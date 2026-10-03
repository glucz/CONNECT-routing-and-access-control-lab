"""Create an offline sensitivity plan; the campaign executes the saved plan unchanged."""
import argparse
import hashlib
import json
import math
from pathlib import Path
from .proxy import TOKEN


def traffic_estimate(cells, repetitions):
    payload = sum(c["prediction"]["body_bytes"] for c in cells) * repetitions
    overhead = sum(c["prediction"]["requests"]*1024 + c["prediction"]["tunnels"]*8192
                   for c in cells) * repetitions
    return {"runs": len(cells)*repetitions, "predicted_body_bytes": payload,
            "assumed_tunnel_bytes": payload+overhead,
            "assumed_tunnel_bytes_if_every_run_retried": 2*(payload+overhead),
            "assumptions": "1024 bytes per HTTP request/response and 8192 per TLS tunnel; excludes TCP/IP and background traffic. Not a bound."}


def predict(c):
    """Analytic prediction under stable tunnels, equal session splits and no window expiry."""
    R, M, L, S = (c[k] for k in ("count", "reuse", "limit", "sessions"))
    video = c["workload"] == "video"
    route, defender = c["routing"], c["defender"]
    records = requests = tunnels = addresses = body = 0
    for i in range(S):
        n = R // S + (i < R % S)
        requests += 1; tunnels += 1; addresses += 1
        login_ok = route != "R-dirty-stick"
        body += 2 if login_ok else 0
        if video and not login_ok:
            continue
        rotating = route == "R-clean-rot"
        if video:
            requests += 1; tunnels += 1; addresses += rotating
            if defender == "bind" and rotating or (L and not rotating and L < 2):
                continue
            body += c["manifest_bytes"]
        allowed = login_ok and not (defender == "bind" and rotating) and not (
            route == "R-split" and defender != "login")
        if not allowed:
            good = 0
        elif not L:
            good = n
        elif rotating:
            good = (n // M) * min(M, L) + min(n % M, L)
        else:
            good = min(n, max(0, L - (2 if video and route != "R-split" else 1)))
        attempted = n if not video or good == n else good + 1
        if video and rotating and L and L < M and allowed:
            good = min(n, L)
            attempted = min(n, good + 1)
        records += good
        requests += attempted
        new_tunnels = math.ceil(attempted / M)
        tunnels += new_tunnels
        addresses += new_tunnels if rotating else int(route == "R-split" and attempted > 0)
        body += good * c["body_bytes"]
    return {"records_ok": records, "delivery_fraction": records / R,
            "complete_delivery": records == R, "success_99": records >= (99*R + 99)//100,
            "success": records == R if video else records >= (99*R + 99)//100,
            "requests": requests, "tunnels": tunnels, "addresses": addresses, "body_bytes": body}


def build_plan(block, repetitions=3, seed=20261003):
    if block not in ("capacity", "composition") or not 1 <= repetitions <= 30:
        raise ValueError("invalid block/repetitions")
    cells = []
    def add(section, **kwargs):
        c = dict(block=section, workload="scrape", defender="every", routing="R-clean-stick",
                 limit=0, sessions=1, count=1000, body_bytes=1024, manifest_bytes=10000, reuse=25)
        c.update(kwargs)
        c["cell_id"] = f"sens-{block}-{len(cells):03d}"
        c["prediction"] = predict(c)
        cells.append(c)
    if block == "capacity":
        for M in (1, 10, 25, 100):
            for L in sorted({max(1, M-1), M, M+1}):
                for defender in ("every", "bind"):
                    for route in ("R-clean-rot", "R-clean-stick"):
                        add("reuse_limit", reuse=M, limit=L, defender=defender, routing=route)
        for L in (10, 50, 101, 200, 334):
            full = math.ceil(1000 / (L-1))
            accepted = math.ceil(990 / (L-1))
            for S in sorted({max(1, k+d) for k in (full, accepted) for d in (-1, 0, 1)}):
                for defender in ("every", "bind"):
                    for M in (1, 100):
                        add("session_threshold", limit=L, sessions=S, defender=defender, reuse=M)
    else:
        for count in (10, 100, 1000):
            for manifest in (1000, 10000, 1_000_000):
                for defender in ("login", "every", "bind"):
                    for route in ("R-split", "R-clean-rot", "R-clean-stick"):
                        add("composition", workload="video", count=count, body_bytes=65536,
                            manifest_bytes=manifest, reuse=100, defender=defender, routing=route)
    return {"schema": 1, "mode": "sensitivity", "block": block, "seed": seed,
            "repetitions": repetitions, "byte_budget": 2_000_000_000 if block == "capacity" else 8_000_000_000,
            "window_s": 3600, "cells": cells,
            "estimate": traffic_estimate(cells, repetitions),
            "price_grid": {"clean_usd_per_gb": [.49, 1.45, 5.0],
                           "dirty_usd_per_gb": [.0000276, .01, .12, .38],
                           "held_usd_per_address_month": [.225, 1.25, 4.6]},
            "notes": ["Predictions assume prescribed connection reuse, no early closes and no rate-window expiry.",
                      "99% delivery and complete delivery are separate outcomes.",
                      "Synthetic deterministic mechanism sensitivity; not measurement of real-service thresholds.",
                      "Composition changes byte counts, not paced playback duration; runtime is measured."]}


def validate_plan(p):
    if p.get("schema") != 1 or p.get("mode") != "sensitivity" or not p.get("cells"):
        raise ValueError("invalid sensitivity plan")
    if not isinstance(p["repetitions"], int) or not 1 <= p["repetitions"] <= 30:
        raise ValueError("invalid repetitions")
    if not isinstance(p["byte_budget"], int) or p["byte_budget"] <= 0 or p["window_s"] != 3600:
        raise ValueError("invalid cap/window")
    ids = set()
    for c in p["cells"]:
        if not TOKEN.fullmatch(c["cell_id"]) or c["cell_id"] in ids:
            raise ValueError("duplicate cell id")
        ids.add(c["cell_id"])
        if any(not isinstance(c[k], int) or c[k] < 1 for k in ("count", "reuse", "body_bytes", "manifest_bytes", "sessions")):
            raise ValueError("invalid workload parameters")
        if c["sessions"] > c["count"] or not isinstance(c["limit"], int) or c["limit"] < 0 or c["workload"] not in ("video", "scrape"):
            raise ValueError("invalid cell")
        if c["workload"] == "video" and (c["limit"] or c["sessions"] != 1):
            raise ValueError("video sensitivity currently supports one session and no rate cap")
        if c["routing"] not in ("R-clean-stick", "R-clean-rot", "R-split") or c["defender"] not in ("login", "every", "bind"):
            raise ValueError("invalid route/defender")
        if c["workload"] == "scrape" and c["routing"] == "R-split":
            raise ValueError("scraping sensitivity uses clean routes")
        if c["prediction"] != predict(c):
            raise ValueError("prediction no longer matches cell; regenerate the plan")
    if p["estimate"] != traffic_estimate(p["cells"], p["repetitions"]):
        raise ValueError("traffic estimate no longer matches grid")
    for key in ("clean_usd_per_gb", "dirty_usd_per_gb", "held_usd_per_address_month"):
        values = p["price_grid"][key]
        if not values or any(not isinstance(v, (float, int)) or not math.isfinite(v) or v < 0 for v in values):
            raise ValueError("invalid price grid")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--block", required=True, choices=("capacity", "composition"))
    parser.add_argument("--output", required=True)
    parser.add_argument("--repetitions", type=int, default=3)
    args = parser.parse_args()
    plan = build_plan(args.block, args.repetitions)
    validate_plan(plan)
    raw = json.dumps(plan, indent=2)
    with Path(args.output).open("x", encoding="utf-8") as f:
        f.write(raw)
    print(json.dumps({"cells": len(plan["cells"]), **plan["estimate"],
                      "hard_cap_bytes": plan["byte_budget"], "sha256": hashlib.sha256(raw.encode()).hexdigest()}, indent=2))


if __name__ == "__main__":
    main()
