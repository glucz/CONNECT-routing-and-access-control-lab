"""Verify bundled anonymous reports and reproduce the campaign summary offline."""
import argparse
import csv
import hashlib
import json
from pathlib import Path


def reproduce(output):
    published = Path(__file__).resolve().parent / "published"
    manifest = json.loads((published / "manifest.json").read_text(encoding="utf-8"))
    for name, expected in manifest["sha256"].items():
        path = (published / name).resolve()
        if not path.is_relative_to(published.resolve()):
            raise ValueError("manifest path escapes published directory")
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError("Published artifact differs: " + name)
    with (published / "campaigns.csv").open(encoding="utf-8", newline="") as f:
        inventory = list(csv.DictReader(f))
    records = []
    for block in ("capacity", "composition", "rate-window", "reconnect"):
        aggregate = json.loads((published / block / "aggregates.json").read_text(encoding="utf-8"))
        with (published / block / "delivery.csv").open(encoding="utf-8", newline="") as f:
            delivery = list(csv.DictReader(f))
        campaign = next(c for c in inventory if c["campaign"].startswith(block + "-"))
        valid = sum(int(c["valid"]) for c in delivery)
        if valid != int(campaign["final_valid"]) or valid != sum(c["valid"] for c in aggregate["cells"]):
            raise ValueError("Validity counts disagree: " + block)
        records.append(dict(block=block, configurations=len(delivery), planned_runs=int(campaign["planned"]),
            retained_valid_runs=valid, attempts=int(campaign["attempts"]),
            invalid_attempts=int(campaign["invalid_attempts"]), complete_deliveries=sum(c["complete_deliveries"] for c in aggregate["cells"]),
            proxy_bytes=int(campaign["proxy_bytes"]), duration_s=float(campaign["duration_s"])))
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(records[0]))
        writer.writeheader(); writer.writerows(records)
    print(f"Verified {len(manifest['sha256'])} artifacts; wrote {output}")
    print(f"{sum(r['retained_valid_runs'] for r in records)} retained valid runs, "
          f"{sum(r['attempts'] for r in records)} attempts, "
          f"{sum(r['proxy_bytes'] for r in records)} tunnel bytes")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="output/summary.csv")
    args = parser.parse_args()
    reproduce(args.output)
