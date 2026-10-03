"""Saved pacing/reconnect experiments and timestamp-conditioned mechanism reports."""
import argparse
from collections import defaultdict
import csv
import json
import math
from pathlib import Path

from .analyze import analyze, rows
from .proxy import TOKEN


def replay(cell, arrivals):
    """Independent first-arrival-anchored window model; never reads observed status."""
    counters, login_source = {}, None
    result = []
    for r in arrivals:
        now, source = r["arrival_monotonic_s"], r["source_ip"]
        start, used = counters.get(source, (now, 0))
        reset = now - start >= cell["window_s"]
        if reset:
            start, used = now, 0
        used += 1
        counters[source] = (start, used)
        reason = ""
        if r["class"] != "login":
            if login_source is None:
                reason = "no_session"
            elif cell["defender"] == "bind" and source != login_source:
                reason = "bind_mismatch"
        if not reason and cell["limit"] and used > cell["limit"]:
            reason = "rate"
        if r["class"] == "login" and not reason:
            login_source = source
        result.append(dict(status=403 if reason else 200, reason=reason,
                           window_reset=reset, window_count=used))
    return result


def ideal_arrivals(c):
    arrivals = [dict(source_ip=0, tunnel_ordinal=0, arrival_monotonic_s=0., **{"class": "login"}, index=0)]
    tunnel, used, generation = 0, c["reuse"], 0
    for i in range(c["count"]):
        if c["reconnect_every"] and i and i % c["reconnect_every"] == 0:
            used = c["reuse"]
            generation += c["reconnect_source"] == "rotate"
        if used >= c["reuse"]:
            tunnel += 1
            used = 0
        used += 1
        source = tunnel if c["routing"] == "R-clean-rot" else generation
        arrivals.append(dict(source_ip=source, tunnel_ordinal=tunnel, arrival_monotonic_s=(i+1)*c["request_interval_s"],
                             **{"class": "seg"}, index=i))
    return arrivals


def build_plan(block):
    if block not in ("rate-window", "reconnect"):
        raise ValueError("unknown temporal block")
    cells = []
    def add(**kw):
        c = dict(block=block, workload="scrape", defender="every", routing="R-clean-stick",
                 sessions=1, count=31, body_bytes=1024, manifest_bytes=32, reuse=10000,
                 limit=0, window_s=3600, request_interval_s=.5,
                 reconnect_every=0, reconnect_source="hold")
        c.update(kw)
        c["cell_id"] = f"time-{block}-{len(cells):03d}"
        ideal = replay(c, ideal_arrivals(c))
        c["ideal_records_ok"] = sum(r["status"] == 200 for r in ideal[1:])
        cells.append(c)
    if block == "rate-window":
        for window in (2, 10):
            for load in (.5, 1., 2.):
                for defender in ("every", "bind"):
                    for route in ("R-clean-stick", "R-clean-rot"):
                        for reuse in (1, 10):
                            add(window_s=window, limit=10, request_interval_s=window/(10*load),
                                defender=defender, routing=route, reuse=reuse)
    else:
        for interval in (.5, 2.):
            for defender in ("every", "bind"):
                add(count=61, request_interval_s=interval, defender=defender)
                for every in (5, 20):
                    for source in ("hold", "rotate"):
                        add(count=61, request_interval_s=interval, defender=defender,
                            reconnect_every=every, reconnect_source=source)
    return dict(schema=1, mode="temporal", block=block, seed=20261003, repetitions=1,
                byte_budget=250_000_000, cells=cells,
                scheduled_wait_s=sum(c["count"]*c["request_interval_s"] for c in cells),
                notes=["One run per configuration; no independent repeat batches.",
                       "Serial requests, minimum start-to-start spacing, no catch-up bursts.",
                       "Login is paced relative to subsequent requests and consumes rate quota.",
                       "Windows anchor at each source's first arrival; denied requests also count.",
                       "Ideal predictions omit network latency; replay conditions on recorded arrival times.",
                       "Scrape probe continues after denials to observe subsequent resets/reconnects.",
                       "Reconnects preserve application cookie/session; rotate changes only proxy source identity.",
                       "No cookie expiry or TCP idle-timeout sensitivity is implemented by this experiment."])


def validate_plan(p):
    if p.get("mode") != "temporal" or p.get("schema") != 1 or p.get("repetitions") != 1:
        raise ValueError("invalid temporal plan")
    if not isinstance(p.get("byte_budget"), int) or p["byte_budget"] <= 0 or not p.get("cells"):
        raise ValueError("invalid budget/cells")
    ids = set()
    for c in p["cells"]:
        if not TOKEN.fullmatch(c["cell_id"]) or c["cell_id"] in ids:
            raise ValueError("duplicate/invalid cell id")
        ids.add(c["cell_id"])
        if c["workload"] != "scrape" or c["sessions"] != 1 or c["defender"] not in ("every", "bind"):
            raise ValueError("temporal tests require a single clean scrape session")
        if c["routing"] not in ("R-clean-stick", "R-clean-rot"):
            raise ValueError("invalid temporal route")
        for k in ("count", "reuse", "window_s", "body_bytes", "manifest_bytes"):
            if not isinstance(c[k], int) or c[k] < 1:
                raise ValueError("invalid " + k)
        for k in ("limit", "reconnect_every"):
            if not isinstance(c[k], int) or c[k] < 0:
                raise ValueError("invalid " + k)
        if c["reconnect_source"] not in ("hold", "rotate"):
            raise ValueError("invalid reconnect source")
        interval = c["request_interval_s"]
        if not isinstance(interval, (int, float)) or not math.isfinite(interval) or not 0 < interval <= 10:
            raise ValueError("invalid pacing interval")
        expected = sum(r["status"] == 200 for r in replay(c, ideal_arrivals(c))[1:])
        if c["ideal_records_ok"] != expected:
            raise ValueError("regenerate ideal prediction")


def report(directory, output):
    root, out = Path(directory), Path(output)
    plan = json.loads((root / "sensitivity-plan.json").read_text())
    validate_plan(plan)
    summary = analyze(root)
    latest = {}
    for r in rows(root / "runs.jsonl"):
        latest[r["cell"]["cell_id"]] = r
    origin, requests = defaultdict(list), defaultdict(list)
    for r in rows(root / "origin.jsonl"):
        origin[r["run_id"]].append(r)
    for r in rows(root / "requests.jsonl"):
        requests[r["run_id"]].append(r)
    table, timeline = [], []
    for cell in summary["cells"]:
        run = latest.get(cell["cell_id"])
        record = {k: cell[k] for k in ("cell_id", "block", "defender", "routing", "count", "reuse", "limit",
                    "window_s", "request_interval_s", "reconnect_every", "reconnect_source", "ideal_records_ok",
                    "valid", "invalid", "missing")}
        record.update(observed_records_ok=None, replay_records_ok=None, replay_mismatches=None,
                      source_pattern_mismatches=None, tunnel_pattern_mismatches=None,
                      duration_s=None, measured_arrival_rate=None,
                      window_resets=None, forced_reconnects=None)
        if run:
            arrivals = origin[run["run_id"]]
            predicted = replay(cell, arrivals)
            ideal = ideal_arrivals(cell)
            sources, tunnels = {}, {}
            span = arrivals[-1]["arrival_monotonic_s"] - arrivals[0]["arrival_monotonic_s"] if arrivals else 0
            patterns = tunnel_patterns = 0
            for n, (r, pred) in enumerate(zip(arrivals, predicted)):
                ordinal = sources.setdefault(r["source_ip"], len(sources))
                expected = ideal[n]["source_ip"] if n < len(ideal) else None
                patterns += ordinal != expected
                tunnel = tunnels.setdefault(r["tunnel_id"], len(tunnels))
                tunnel_patterns += tunnel != (ideal[n]["tunnel_ordinal"] if n < len(ideal) else None)
                timeline.append(dict(cell_id=cell["cell_id"], sequence=n, request_class=r["class"],
                    index=r["index"], elapsed_s=r["arrival_monotonic_s"]-arrivals[0]["arrival_monotonic_s"],
                    source_ordinal=ordinal, tunnel_ordinal=tunnel, status=r["status"], reason=r["reason"],
                    replay_status=pred["status"], replay_reason=pred["reason"],
                    window_count=pred["window_count"], window_reset=pred["window_reset"]))
            record.update(observed_records_ok=run["records_ok"],
                replay_records_ok=sum(p["status"] == 200 for r,p in zip(arrivals,predicted) if r["class"] == "seg"),
                replay_mismatches=sum((r["status"],r["reason"]) != (p["status"],p["reason"]) for r,p in zip(arrivals,predicted)),
                source_pattern_mismatches=patterns + abs(len(ideal)-len(arrivals)), duration_s=run["duration_s"],
                tunnel_pattern_mismatches=tunnel_patterns + abs(len(ideal)-len(arrivals)),
                measured_arrival_rate=(len(arrivals)-1)/span if span else None,
                window_resets=sum(p["window_reset"] for p in predicted),
                forced_reconnects=sum(r.get("forced_reconnect", False) for r in requests[run["run_id"]]))
        table.append(record)
    out.mkdir(parents=True, exist_ok=False)
    (out / "aggregates.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    for name, data in (("delivery.csv", table), ("timeline.csv", timeline)):
        if data:
            with (out / name).open("x", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=list(data[0]))
                writer.writeheader(); writer.writerows(data)
    (out / "README.md").write_text(
        "# Temporal mechanism results\n\nCompare ideal_records_ok with observations as timing sensitivity, "
        "not as an infrastructure validity test. Replay uses measured arrival times and source identities, "
        "but does not read observed status to predict it. Zero replay mismatches establish consistency with "
        "the implemented policy, not an independent empirical discovery. Inspect source_pattern_mismatches "
        "separately. Missing/invalid runs are not evidence of policy rejection. timeline.csv uses anonymous "
        "source ordinals within each run. Intervals describe serial offered load, not concurrent saturation.\n",
        encoding="utf-8")
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--block", choices=("rate-window", "reconnect"))
    group.add_argument("--report", metavar="CAMPAIGN")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    if args.report:
        result = report(args.report, args.output)
        print(f"Wrote temporal report: {len(result['cells'])} cells, {result['campaign_status']}")
    else:
        plan = build_plan(args.block)
        validate_plan(plan)
        with Path(args.output).open("x", encoding="utf-8") as f:
            json.dump(plan, f, indent=2)
        print(f"{len(plan['cells'])} runs; scheduled pacing {plan['scheduled_wait_s']/60:.1f} min "
              f"plus request overhead; cap {plan['byte_budget']/1e6:.0f} MB")


if __name__ == "__main__":
    main()
