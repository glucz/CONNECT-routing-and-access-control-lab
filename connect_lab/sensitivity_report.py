"""Produce aggregate tables, offline price comparisons and optional scientific plots."""
import argparse
from collections import defaultdict
import csv
import itertools
import json
from pathlib import Path

from .analyze import analyze


def price_sweep(cells, grid):
    grouped = defaultdict(list)
    fields = ("block", "workload", "defender", "limit", "count", "body_bytes", "manifest_bytes", "reuse")
    for cell in cells:
        grouped[tuple(cell[k] for k in fields)].append(cell)
    result = []
    axes = (grid["clean_usd_per_gb"], grid["dirty_usd_per_gb"], grid["held_usd_per_address_month"])
    for key, candidates in grouped.items():
        for criterion, counter, suffix in (("complete", "complete_deliveries", "complete"),
                                            ("workload_success", "successes", "successes")):
            eligible = [c for c in candidates if c["valid"] == c["planned"] and c[counter] == c["planned"]]
            for clean, dirty, rent in itertools.product(*axes):
                choices = []
                for c in eligible:
                    cb, db, ips = (c[k + "_among_" + suffix]["mean"]
                                   for k in ("clean_bytes", "dirty_bytes", "clean_addresses"))
                    choices.append(((cb*clean+db*dirty)/1e9, c["cell_id"], "transfer"))
                    if c["routing"] == "R-clean-stick":
                        choices.append((ips*rent + db*dirty/1e9, c["cell_id"], "dedicated_address_month"))
                winner = min(choices) if choices else None
                tied = [x for x in choices if winner and abs(x[0]-winner[0]) <= 1e-12]
                result.append({**dict(zip(fields, key)), "criterion": criterion,
                    "clean_usd_per_gb": clean, "dirty_usd_per_gb": dirty,
                    "held_usd_per_address_month": rent,
                    "sampled_group_complete": all(c["valid"] == c["planned"] for c in candidates),
                    "eligible_routes": len(eligible), "minimum_usd": winner[0] if winner else None,
                    "winners": ";".join(f"{x[1]}:{x[2]}" for x in tied)})
    return result


def write_csv(path, records):
    if not records:
        return
    with path.open("x", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(records[0]))
        writer.writeheader(); writer.writerows(records)


def figures(cells, output):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import math
    def save(fig, name):
        fig.savefig(output / (name + ".svg"), bbox_inches="tight")
        fig.savefig(output / (name + ".png"), dpi=180, bbox_inches="tight")
        plt.close(fig)
    reuse = [c for c in cells if c["block"] == "reuse_limit" and c["delivery_fraction_all_valid_runs"]]
    if reuse:
        fig, axes = plt.subplots(2, 2, figsize=(10, 8), constrained_layout=True)
        for i, defender in enumerate(("every", "bind")):
            for j, route in enumerate(("R-clean-rot", "R-clean-stick")):
                subset = [c for c in reuse if c["defender"] == defender and c["routing"] == route]
                ax = axes[i, j]
                scatter = ax.scatter([c["reuse"] for c in subset], [c["limit"] for c in subset],
                    c=[c["delivery_fraction_all_valid_runs"]["mean"] for c in subset],
                    vmin=0, vmax=1, cmap="viridis", s=90)
                ax.plot([1, 100], [1, 100], "k--", alpha=.4)
                ax.set(xscale="log", yscale="log", xlabel="Requests per tunnel M", ylabel="Address limit L",
                       title=f"{defender} / {route}")
        fig.colorbar(scatter, ax=axes.ravel().tolist(), label="Observed delivered fraction")
        save(fig, "reuse-limit-delivery")
    threshold = [c for c in cells if c["block"] == "session_threshold" and c["delivery_fraction_all_valid_runs"]]
    if threshold:
        fig, axes = plt.subplots(2, 2, figsize=(11, 8), constrained_layout=True)
        for i, defender in enumerate(("every", "bind")):
            for j, M in enumerate((1, 100)):
                ax = axes[i, j]
                for L in sorted({c["limit"] for c in threshold}):
                    subset = sorted((c for c in threshold if c["defender"] == defender and c["reuse"] == M and c["limit"] == L), key=lambda c:c["sessions"])
                    if not subset:
                        continue
                    x = [c["sessions"] / math.ceil(c["count"]/(L-1)) for c in subset]
                    line, = ax.plot(x, [c["delivery_fraction_all_valid_runs"]["mean"] for c in subset], "o-", label=f"L={L}")
                    ax.plot(x, [c["prediction"]["delivery_fraction"] for c in subset], "x", color=line.get_color())
                ax.axhline(.99, color="gray", linestyle=":")
                ax.axhline(1, color="gray", linestyle="--")
                ax.set(xlabel="Sessions / full-delivery threshold", ylabel="Delivered fraction",
                       title=f"{defender}, M={M} (o observed; x predicted)")
                ax.legend(fontsize=7)
        save(fig, "held-session-threshold")
    composition = [c for c in cells if c["block"] == "composition"]
    if composition:
        fig, axes = plt.subplots(1, 3, figsize=(13, 4), constrained_layout=True)
        fractions = [(c["manifest_bytes"]+2)/(c["manifest_bytes"]+2+c["count"]*c["body_bytes"]) for c in composition]
        for ax, defender in zip(axes, ("login", "every", "bind")):
            for route in ("R-split", "R-clean-rot", "R-clean-stick"):
                subset = [c for c in composition if c["defender"] == defender and c["routing"] == route
                          and c["clean_share_among_successes"]]
                ax.scatter([(c["manifest_bytes"]+2)/(c["manifest_bytes"]+2+c["count"]*c["body_bytes"]) for c in subset],
                           [c["clean_share_among_successes"]["mean"] for c in subset], label=route, alpha=.7)
            ax.set_xlim(min(fractions) / 2, min(1, max(fractions) * 2))
            ax.set(xscale="log", xlabel="Configured control-body fraction", ylabel="Measured clean tunnel-byte share",
                   title=defender)
            ax.legend(fontsize=7)
        save(fig, "composition-clean-share")


def report(campaign, output, plots=False):
    root, output = Path(campaign), Path(output)
    summary = analyze(root)
    if summary["mode"] != "sensitivity":
        raise ValueError("use a sensitivity campaign directory")
    plan = json.loads((root / "sensitivity-plan.json").read_text(encoding="utf-8"))
    output.mkdir(parents=True, exist_ok=False)
    (output / "aggregates.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    table = []
    for c in summary["cells"]:
        row = {k: c[k] for k in ("cell_id", "block", "workload", "defender", "routing", "count", "reuse", "limit", "sessions", "body_bytes", "manifest_bytes", "planned", "valid", "missing", "invalid", "successes_99", "complete_deliveries", "prediction_mismatches", "address_prediction_mismatches", "tunnel_prediction_mismatches")}
        row["predicted_fraction"] = c["prediction"]["delivery_fraction"]
        observed = c["delivery_fraction_all_valid_runs"]
        row["observed_fraction"] = observed["mean"] if observed else None
        table.append(row)
    write_csv(output / "delivery.csv", table)
    write_csv(output / "prices.csv", price_sweep(summary["cells"], plan["price_grid"]))
    mismatches = sum(c["prediction_mismatches"] for c in summary["cells"])
    (output / "README.md").write_text(
        f"# Sensitivity results\n\nStatus: {summary['campaign_status']}. Cells: {len(table)}. "
        f"Valid-run delivery prediction mismatches: {mismatches}.\n\n"
        "delivery.csv separates 99% and complete delivery; missing/invalid runs remain explicit. "
        "A prediction mismatch is a finding, not an instruction to overwrite observations.\n\n"
        "prices.csv is an offline scenario sweep. Candidates must deliver the criterion in every planned repetition. "
        "Winners are only among sampled candidates, not an exhaustive market minimum. Empty winners are not proof of impossibility, "
        "especially when sampled_group_complete is false. Dedicated-address costs reserve a whole address-month per used clean address; "
        "there is no assumed sharing, profitability, or provider billing measurement.\n\n"
        "Interface counters include background/control traffic; choose the physical interface and do not sum virtual duplicates. "
        "These measurements are synthetic mechanism sensitivity, not discovered service policies.\n", encoding="utf-8")
    if plots:
        figures(summary["cells"], output)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("campaign")
    parser.add_argument("--output", required=True)
    parser.add_argument("--plots", action="store_true", help="Requires matplotlib; writes SVG and PNG")
    args = parser.parse_args()
    result = report(args.campaign, args.output, args.plots)
    print(f"Wrote sensitivity report: {len(result['cells'])} cells, {result['campaign_status']}")


if __name__ == "__main__":
    main()
