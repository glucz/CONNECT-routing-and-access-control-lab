"""Run the full matrix or a small smoke matrix; owns the exit-pool process lifetime."""
import argparse
import asyncio
import copy
import hashlib
import http.client
import json
import math
from pathlib import Path
import platform
import random
import time
import uuid

from .driver import ROUTES, run_job
from .proxy import ExitProxy, JsonLog
from .netstats import snapshot, delta


class Control:
    def __init__(self, port, token_file):
        self.port = port
        self.token = Path(token_file).read_text().strip()

    def call(self, method, path, value=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        try:
            data = json.dumps(value).encode() if value is not None else None
            conn.request(method, path, body=data, headers={"Authorization": "Bearer " + self.token,
                                                         "Content-Type": "application/json"})
            response = conn.getresponse()
            body = response.read()
            if response.status != 200:
                raise RuntimeError(f"origin control returned {response.status}")
            return json.loads(body)
        finally:
            conn.close()


def matrix(smoke=False):
    cells = []
    for workload in ("video", "scrape"):
        for defender in ("login", "every", "bind"):
            for routing in ROUTES:
                cells.append(dict(block=1, workload=workload, defender=defender, routing=routing,
                                  limit=0, sessions=1))
    count = 8 if smoke else 1000
    for limit in ((3, 5) if smoke else (50, 200)):
        for defender, routing, sessions in (
            ("every", "R-clean-rot", 1), ("every", "R-clean-stick", 1),
            ("every", "R-clean-stick", math.ceil(count / (limit - 1))),
            ("bind", "R-clean-rot", 1),
            ("bind", "R-clean-stick", math.ceil(count / (limit - 1))),
        ):
            cells.append(dict(block=2, workload="scrape", defender=defender, routing=routing,
                              limit=limit, sessions=sessions))
    for i, cell in enumerate(cells):
        cell["cell_id"] = f"cell-{i:02d}"
    return cells


async def collect(control, cursor, log):
    rows = []
    while True:
        result = await asyncio.to_thread(control.call, "GET", f"/rows?cursor={cursor}")
        rows.extend(result["rows"])
        for row in result["rows"]:
            log(row)
        cursor = result["cursor"]
        if cursor == result["total"]:
            break
    await asyncio.to_thread(control.call, "POST", "/ack", {"cursor": cursor})
    return cursor, rows


async def run_campaign(config, output, smoke=False, preregistration=None, allocator=None, pilot=False,
                       sensitivity_plan=None):
    plan = None
    if sensitivity_plan:
        if smoke or pilot or preregistration:
            raise ValueError("sensitivity must run separately")
        plan = json.loads(Path(sensitivity_plan).read_text(encoding="utf-8"))
        if plan.get("mode") == "temporal":
            from .temporal import validate_plan
        else:
            from .sensitivity import validate_plan
        validate_plan(plan)
    if pilot and (smoke or preregistration):
        raise ValueError("pilot is separate from smoke and registered campaigns")
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    repetitions = 1 if smoke or pilot else 30
    cells = matrix(smoke)
    if pilot:
        pairs = {("login", "R-split"), ("every", "R-split"),
                 ("bind", "R-clean-rot"), ("bind", "R-clean-stick")}
        cells = [c for c in cells if c["block"] == 1 and c["workload"] == "video"
                 and (c["defender"], c["routing"]) in pairs]
    if plan:
        repetitions, cells = plan["repetitions"], copy.deepcopy(plan["cells"])
    seed = plan["seed"] if plan else config.get("seed", 20261002)
    random.Random(seed).shuffle(cells)
    environment = {"mode": plan["mode"] if plan else ("pilot" if pilot else ("smoke" if smoke else "registered")), "python": platform.python_version(),
                   "platform": platform.platform(), "seed": seed,
                   "repetitions": repetitions, "cells": cells, "config": config,
                   "source_sha256": {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                                     for p in Path(__file__).parent.glob("*.py")},
                   "preregistration_sha256": hashlib.sha256(Path(preregistration).read_bytes()).hexdigest()
                       if preregistration else None}
    if plan:
        raw_plan = Path(sensitivity_plan).read_bytes()
        environment["sensitivity_plan_sha256"] = hashlib.sha256(raw_plan).hexdigest()
        (output / "sensitivity-plan.json").write_bytes(raw_plan)
    (output / "env.json").write_text(json.dumps(environment, indent=2), encoding="utf-8")
    if preregistration:
        (output / "approved-preregistration.md").write_bytes(Path(preregistration).read_bytes())
    logs = {name: JsonLog(output / (name + ".jsonl"))
            for name in ("proxy", "requests", "origin", "runs")}
    pool_config = copy.deepcopy(config["pool"])
    pool_config["byte_budget"] = min(pool_config["byte_budget"], 10_000_000) if smoke else pool_config["byte_budget"]
    if pilot:
        pool_config["byte_budget"] = min(pool_config["byte_budget"], 3_000_000_000)
    if plan:
        pool_config["byte_budget"] = min(pool_config["byte_budget"], plan["byte_budget"])
    pool = ExitProxy(pool_config, logs["proxy"], allocator)
    control = Control(config["control_port"], config["admin_token_file"])
    state = {"status": "running", "attempts": 0, "planned_runs": len(cells) * repetitions,
             "finished_runs": 0, "started": time.time(), "invalid_runs": 0,
             "mode": environment["mode"], "byte_budget": pool_config["byte_budget"]}
    before_b, before_a = snapshot(), {}
    try:
        ports = await pool.start()
        logs["proxy"]({"event": "start", "time": time.time(), "config": pool_config, "ports": ports})
        health = await asyncio.to_thread(control.call, "GET", "/health")
        if plan and plan["mode"] == "temporal" and health.get("arrival_clock") != "monotonic-v1":
            raise RuntimeError("Update and restart origin on A: temporal runs require arrival timestamps")
        before_a = health.get("network", {})
        cursor = health["cursor"]
        for cell in cells:
            count = cell.get("count", 8 if smoke else 1000)
            body_bytes = cell.get("body_bytes", 128 if smoke else (1_000_000 if cell["workload"] == "video" else 26227))
            manifest_bytes = cell.get("manifest_bytes", 32 if smoke else 10000)
            policy = {"defender": cell["defender"], "cell_id": cell["cell_id"],
                      "body_bytes": body_bytes, "manifest_bytes": manifest_bytes,
                      "limit": cell["limit"], "window_s": cell.get("window_s", 3600)}
            configured = await asyncio.to_thread(control.call, "POST", "/policy", policy)
            if configured["cursor"] != cursor:
                raise RuntimeError("unexpected origin traffic between cells")
            for repetition in range(repetitions):
                for attempt in (1, 2):
                    if pool.exhausted:
                        state["status"] = "budget_exhausted"
                        return state
                    job = dict(config["job"], workload=cell["workload"], routing=cell["routing"],
                               count=count, body_bytes=body_bytes, manifest_bytes=manifest_bytes,
                               reuse=cell.get("reuse", 2 if smoke else (100 if cell["workload"] == "video" else 25)),
                               sessions=cell["sessions"], proxies=ports, run_id=uuid.uuid4().hex)
                    for key in ("request_interval_s", "reconnect_every", "reconnect_source"):
                        if key in cell:
                            job[key] = cell[key]
                    client_rows = []
                    def request_log(row):
                        client_rows.append(row)
                        logs["requests"](row)
                    result = await asyncio.to_thread(run_job, job, request_log)
                    deadline = time.monotonic() + 45
                    while pool.tasks and time.monotonic() < deadline:
                        await asyncio.sleep(.02)
                    if pool.tasks:
                        raise RuntimeError("proxy tunnels did not finish; campaign incomplete")
                    cursor, origin_rows = await collect(control, cursor, logs["origin"])
                    # All successful client requests must have a matching complete origin row.
                    keyed = {(r["tunnel_id"], r["class"], r["index"]): r for r in origin_rows
                             if r["run_id"] == result["run_id"]}
                    consistent = all((r["tunnel_id"], r["class"], r["index"]) in keyed and
                        keyed[(r["tunnel_id"], r["class"], r["index"])]["complete"] and
                        keyed[(r["tunnel_id"], r["class"], r["index"])]["status"] == r["status"] and
                        keyed[(r["tunnel_id"], r["class"], r["index"])]["body_bytes"] == r["body_bytes"]
                        for r in client_rows if r["complete"])
                    window_valid = not cell["limit"] or result.get("duration_s", 0) < policy["window_s"]
                    temporal = plan is not None and plan["mode"] == "temporal"
                    result["valid"] = result["valid"] and consistent and (window_valid or temporal)
                    result["rate_window_unexpired"] = window_valid
                    result.update(cell=cell, repetition=repetition, attempt=attempt,
                                  origin_consistent=consistent, policy_epoch=configured["epoch"],
                                  job=job, budget_exhausted=pool.exhausted)
                    logs["runs"](result)
                    state["attempts"] += 1
                    print(f"{cell['cell_id']} {repetition + 1}/{repetitions} attempt={attempt} "
                          f"success={result['success']} valid={result['valid']} "
                          f"traffic_MB={pool.used/1e6:.2f}", flush=True)
                    if pool.exhausted:
                        state["status"] = "budget_exhausted"
                        return state
                    if result["valid"] or attempt == 2:
                        state["finished_runs"] += 1
                        state["invalid_runs"] += not result["valid"]
                        break  # A defender denial is a result, never grounds for rerunning.
        state["status"] = "complete" if not state["invalid_runs"] else "complete_with_invalid_runs"
        return state
    except BaseException:
        state["status"] = "interrupted"
        raise
    finally:
        await pool.close()
        try:
            after_a = (await asyncio.to_thread(control.call, "GET", "/health")).get("network", {})
        except Exception:
            after_a = {}
        after_b = snapshot()
        state["network"] = {"origin": delta(before_a, after_a), "client": delta(before_b, after_b)}
        (output / "network-counters.json").write_text(json.dumps(
            {"origin_before": before_a, "origin_after": after_a, "client_before": before_b,
             "client_after": after_b}, indent=2), encoding="utf-8")
        state.update(ended=time.time(), proxy_bytes=pool.used)
        logs["proxy"]({"event": "stop", "time": time.time(), "bytes": pool.used,
                       "budget_exhausted": pool.exhausted})
        for log in logs.values():
            log.close()
        (output / "campaign.json").write_text(json.dumps(state, indent=2), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--output", required=True)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--smoke", action="store_true")
    group.add_argument("--pilot", action="store_true", help="Four full-size video cells, one repetition, at most 3 GB")
    group.add_argument("--sensitivity-plan", help="Saved sensitivity grid JSON (separate from registered baseline)")
    group.add_argument("--approved-preregistration", help="Frozen author-approved protocol file")
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    if args.approved_preregistration and not Path(args.approved_preregistration).is_file():
        parser.error("approved preregistration file is missing")
    asyncio.run(run_campaign(config, args.output, args.smoke, args.approved_preregistration,
                             pilot=args.pilot, sensitivity_plan=args.sensitivity_plan))


if __name__ == "__main__":
    main()
