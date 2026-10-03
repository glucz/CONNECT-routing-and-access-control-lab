"""Aggregate a completed or interrupted campaign without exporting addresses or run IDs."""
import argparse
from collections import defaultdict
import json
import math
from pathlib import Path
import statistics


def rows(path):
    with Path(path).open(encoding="utf-8") as source:
        for line in source:
            if line.strip():
                yield json.loads(line)


def describe(values):
    return {"n": len(values), "mean": statistics.mean(values), "median": statistics.median(values),
            "min": min(values), "max": max(values)} if values else None


def wilson(success, total):
    if not total:
        return None
    z = 1.959963984540054
    p = success / total
    center = (p + z*z/(2*total)) / (1 + z*z/total)
    half = z * math.sqrt(p*(1-p)/total + z*z/(4*total*total)) / (1 + z*z/total)
    return [max(0, center-half), min(1, center+half)]


def fisher_greater(a, n, b, m):
    """One-sided exact test: first cell has greater success probability."""
    successes = a+b
    denominator = math.comb(n+m, n)
    return sum(math.comb(successes, x)*math.comb(n+m-successes, n-x)/denominator
               for x in range(a, min(n, successes)+1) if 0 <= n-x <= n+m-successes)


def analyze(directory):
    root = Path(directory)
    env = json.loads((root / "env.json").read_text())
    campaign = json.loads((root / "campaign.json").read_text())
    attempts = list(rows(root / "runs.jsonl"))
    latest = {}
    for run in attempts:
        latest[(run["cell"]["cell_id"], run["repetition"])] = run
    tunnels = {r["tunnel_id"]: r for r in rows(root / "proxy.jsonl") if r.get("event") == "tunnel"
               and "tunnel_id" in r}
    run_tunnels, client_bytes = defaultdict(set), defaultdict(int)
    for row in rows(root / "requests.jsonl"):
        if "tunnel_id" in row:
            run_tunnels[row["run_id"]].add(row["tunnel_id"])
        client_bytes[row["run_id"]] += row["body_bytes"]
    observed, origin_bytes = defaultdict(lambda: defaultdict(set)), defaultdict(int)
    for row in rows(root / "origin.jsonl"):
        observed[row["run_id"]][row["session_id"]].add(row["source_ip"])
        origin_bytes[row["run_id"]] += row["body_bytes"]
    prices = env["config"]["prices"]
    grouped = defaultdict(list)
    accounting_bad = 0
    for run in latest.values():
        rid = run["run_id"]
        ids = run_tunnels[rid]
        complete = all(t in tunnels for t in ids)
        entries = [tunnels[t] for t in ids if t in tunnels]
        byte_by_class = {label: sum(e["bytes_up"] + e["bytes_down"] for e in entries
                                    if e["class"] == label) for label in ("clean", "dirty")}
        total = sum(byte_by_class.values())
        sources = {e["bound_source_ip"] for e in entries if "bound_source_ip" in e}
        clean_sources = {e["bound_source_ip"] for e in entries if "bound_source_ip" in e and e["class"] == "clean"}
        relative = abs(client_bytes[rid]-origin_bytes[rid])/max(1, origin_bytes[rid])
        accounting_bad += relative > .01
        run["measurement_valid"] = run["valid"] and complete and not run["budget_exhausted"] and relative <= .01
        run["measure"] = {
            "clean_bytes": byte_by_class["clean"], "dirty_bytes": byte_by_class["dirty"],
            "clean_addresses": len(clean_sources), "tunnels": len(entries),
            "clean_share": byte_by_class["clean"] / total if total else None,
            "addresses_used": len(sources), "addresses_per_session": [len(v) for v in observed[rid].values()],
            "tunnel_bytes": total, "body_discrepancy_fraction": relative,
            "transfer_usd": (byte_by_class["clean"]*prices["clean_usd_per_gb"] +
                             byte_by_class["dirty"]*prices["dirty_usd_per_gb"]) / 1e9,
            "own_path_low_usd": (byte_by_class["clean"]*prices["clean_usd_per_gb"] +
                                  byte_by_class["dirty"]*prices["own_path_range"][0]) / 1e9,
            "own_path_high_usd": (byte_by_class["clean"]*prices["clean_usd_per_gb"] +
                                   byte_by_class["dirty"]*prices["own_path_range"][1]) / 1e9,
            "dedicated_address_month_usd": len(clean_sources)*prices["held_usd_per_address_month"]
                if run["routing"] == "R-clean-stick" else None,
        }
        grouped[run["cell"]["cell_id"]].append(run)
    results = []
    for cell in sorted(env["cells"], key=lambda c: c["cell_id"]):
        values = grouped[cell["cell_id"]]
        valid = [r for r in values if r["measurement_valid"]]
        passed = [r for r in valid if r["success"]]
        fully_delivered = [r for r in valid if r.get("complete_delivery", False)]
        result = dict(cell, planned=env["repetitions"], completed=len(values), valid=len(valid),
                      successes=len(passed), success_rate=len(passed)/len(valid) if valid else None,
                      success_wilson95=wilson(len(passed), len(valid)),
                      invalid=len(values)-len(valid), missing=env["repetitions"]-len(values),
                      max_body_discrepancy=max((r["measure"]["body_discrepancy_fraction"] for r in values), default=None))
        result["complete_deliveries"] = len(fully_delivered)
        result["successes_99"] = sum(r.get("success_99", r["success"]) for r in valid)
        result["delivery_fraction_all_valid_runs"] = describe([r["delivery_fraction"] for r in valid if "delivery_fraction" in r])
        result["duration_s_all_valid_runs"] = describe([r["duration_s"] for r in valid if "duration_s" in r])
        if "prediction" in cell:
            result["prediction_mismatches"] = sum(r.get("records_ok") != cell["prediction"]["records_ok"] for r in valid)
            result["address_prediction_mismatches"] = sum(r["measure"]["addresses_used"] != cell["prediction"]["addresses"] for r in valid)
            result["tunnel_prediction_mismatches"] = sum(r["measure"]["tunnels"] != cell["prediction"]["tunnels"] for r in valid)
        for label, cohort in (("complete", fully_delivered), ("successes", passed)):
            for key in ("clean_bytes", "dirty_bytes", "clean_addresses"):
                result[key + "_among_" + label] = describe([r["measure"][key] for r in cohort])
        for key in ("clean_share", "addresses_used", "tunnel_bytes", "transfer_usd", "own_path_low_usd",
                    "own_path_high_usd", "dedicated_address_month_usd"):
            result[key + "_among_successes"] = describe([r["measure"][key] for r in passed if r["measure"][key] is not None])
        result["addresses_per_session_among_successes"] = describe([
            n for r in passed for n in r["measure"]["addresses_per_session"]])
        for key in ("addresses_used", "tunnel_bytes", "transfer_usd"):
            result[key + "_all_valid_runs"] = describe([r["measure"][key] for r in valid])
        sessions = [n for r in valid for n in r["measure"]["addresses_per_session"]]
        result["observed_sessions_all_valid_runs"] = len(sessions)
        result["mixed_source_sessions_all_valid_runs"] = sum(n > 1 for n in sessions)
        results.append(result)
    comparisons = []
    for workload in ("video", "scrape"):
        for defender, challenger in (("every", "R-split"), ("bind", "R-split"), ("bind", "R-clean-rot")):
            a = next((c for c in results if c["block"] == 1 and c["workload"] == workload
                     and c["defender"] == defender and c["routing"] == "R-clean-stick"), None)
            b = next((c for c in results if c["block"] == 1 and c["workload"] == workload
                     and c["defender"] == defender and c["routing"] == challenger), None)
            comparisons.append({"workload": workload, "defender": defender, "challenger": challenger,
                                "one_sided_fisher_p": fisher_greater(a["successes"], a["valid"], b["successes"], b["valid"])
                                if a and b and a["valid"] and b["valid"] else None})
    return {"mode": env["mode"], "campaign_status": campaign["status"], "attempts": len(attempts),
            "network": campaign.get("network", {}),
            "proxy_bytes_all_attempts": campaign["proxy_bytes"], "accounting_discrepancies_over_one_percent": accounting_bad,
            "prices": {k: prices[k] for k in ("clean_usd_per_gb", "dirty_usd_per_gb", "held_usd_per_address_month", "own_path_range")},
            "notes": ["Costs are listed-price scenarios, not provider bills or total operating costs.",
                      "Dedicated-address cost reserves each observed clean address for a whole month; no pooling or amortization assumed.",
                      "Intervals and unadjusted directional tests describe repeated deterministic lab runs, not real-service prevalence.",
                      "Proxy bytes include TLS/framing in both directions; body reconciliation compares client with origin only.",
                      "Interrupted campaigns and invalid runs are not silently counted as defender failures."],
            "cells": results, "h1_comparisons": comparisons}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("campaign")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    result = analyze(args.campaign)
    with Path(args.output).open("x", encoding="utf-8") as out:
        json.dump(result, out, indent=2)
    print(f"Wrote {len(result['cells'])} aggregate cells ({result['mode']}, {result['campaign_status']}).")


if __name__ == "__main__":
    main()
