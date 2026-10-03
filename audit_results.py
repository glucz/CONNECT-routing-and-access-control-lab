"""Rebuild reports from preserved lab logs; export a private-data-free evidence inventory."""
import argparse
import csv
import hashlib
import json
from pathlib import Path

from connect_lab.analyze import rows
from connect_lab.sensitivity_report import report as sensitivity_report
from connect_lab.temporal import report as temporal_report


def digest(path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def audit(root, output):
    output.mkdir(parents=True, exist_ok=False)
    inventory, checks, invalid_attempts = [], [], []
    for folder in sorted(root.iterdir()):
        if not (folder / "campaign.json").is_file():
            continue  # Report directories are not independent experiments.
        state = json.loads((folder / "campaign.json").read_text())
        env = json.loads((folder / "env.json").read_text())
        attempts = list(rows(folder / "runs.jsonl"))
        latest = {}
        for r in attempts:
            latest[(r["cell"]["cell_id"], r["repetition"])] = r
            if not r["valid"]:
                invalid_attempts.append(dict(campaign=folder.name, cell=r["cell"]["cell_id"],
                    repetition=r["repetition"], attempt=r["attempt"],
                    first_failure=r.get("first_failure"), records_ok=r["records_ok"],
                    transport_errors=r["transport_errors"], origin_consistent=r["origin_consistent"]))
        mode = state.get("mode", env["mode"])
        entry = dict(campaign=folder.name, mode=mode, status=state["status"],
            planned=state["planned_runs"], finished=state["finished_runs"],
            attempts=len(attempts), invalid_attempts=sum(not r["valid"] for r in attempts),
            final_invalid=state["invalid_runs"],
            final_valid=sum(r["valid"] for r in latest.values()),
            final_success=sum(r["valid"] and r["success"] for r in latest.values()),
            duration_s=state["ended"]-state["started"], proxy_bytes=state["proxy_bytes"])
        inventory.append(entry)
        assert len(attempts) == state["attempts"], folder.name
        if mode not in ("sensitivity", "temporal"):
            continue
        report_dir = output / (folder.name + "-report")
        summary = (temporal_report if mode == "temporal" else sensitivity_report)(folder, report_dir)
        regenerated = (report_dir / "delivery.csv").read_bytes()
        originals = sorted(root.glob(folder.name + "-report*/delivery.csv"))
        check = dict(campaign=folder.name, cells=len(summary["cells"]),
            valid=sum(c["valid"] for c in summary["cells"]),
            accounting_discrepancies=summary["accounting_discrepancies_over_one_percent"],
            original_delivery_reports={str(p.relative_to(root)): p.read_bytes() == regenerated for p in originals},
            raw_sha256={p.name: digest(p) for p in sorted(folder.iterdir()) if p.is_file()})
        checks.append(check)
        assert all(check["original_delivery_reports"].values()), "Report drift: " + folder.name
        assert check["accounting_discrepancies"] == 0, "Body accounting mismatch: " + folder.name
        assert check["valid"] == entry["final_valid"], "Measurement validity differs: " + folder.name
    with (output / "campaign-inventory.csv").open("x", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(inventory[0]))
        writer.writeheader(); writer.writerows(inventory)
    result = dict(inventory=inventory, checks=checks, invalid_attempts=invalid_attempts,
                  note="Reports rebuilt from raw logs with current code; source code hashes for collection remain in each env.json. Raw configs/addresses/IDs are not exported here.")
    (output / "audit.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(f"Audited {len(inventory)} campaigns; rebuilt {len(checks)} sweep reports; original delivery CSVs match.")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", type=Path, default=Path("results/client"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    audit(args.results, args.output)
