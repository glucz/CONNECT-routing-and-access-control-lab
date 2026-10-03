"""Generate private two-server configurations and a lab CA; run on the workstation."""
import argparse
import csv
import datetime
import hashlib
import ipaddress
import json
from pathlib import Path
import secrets
import statistics


def prepare(output, origin_ip, exit_prefix, calibration, posted_prices):
    # cryptography is needed only for preparation/testing, not by either server runtime.
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    origin_ip = str(ipaddress.IPv6Address(origin_ip))
    prefix = ipaddress.IPv6Network(exit_prefix)
    if prefix.prefixlen != 64:
        raise ValueError("exit prefix must be a routed /64")
    calibration, posted_prices = Path(calibration), Path(posted_prices)
    inputs = json.loads(calibration.read_text(encoding="utf-8"))
    with posted_prices.open(encoding="utf-8", newline="") as source:
        records = list(csv.DictReader(source))
    exclusions = ("promo", "marginal", "from-price", "annual", "sales-gated", "contact sales", "(site seized)")
    selected = {}
    for row in records:
        if (row["category"] == "datacenter" and row["observed_date"] == "2026-09-29"
                and row["effective_usd_per_gb"] and not any(x in row["plan"].lower() for x in exclusions)):
            provider = row["provider"]
            if provider not in selected or float(row["effective_usd_per_gb"]) < float(selected[provider]["effective_usd_per_gb"]):
                selected[provider] = row
    if not selected:
        raise ValueError("dated datacenter price sample is empty")
    prices = {
        "clean_usd_per_gb": inputs["prices"]["residential_largest_tier_usd_per_gb"]["median"],
        "dirty_usd_per_gb": statistics.median(float(r["effective_usd_per_gb"]) for r in selected.values()),
        "held_usd_per_address_month": inputs["prices"]["isp_usd_per_ip_month_largest_plan"]["median"],
        "own_path_range": inputs["c0_usd_per_gb"]["value"],
        "provenance": {"date": "2026-09-29", "datacenter_rule": "minimum eligible per-GB quote per provider, then median",
                       "datacenter_rows": list(selected.values()),
                       "calibration_sha256": hashlib.sha256(calibration.read_bytes()).hexdigest(),
                       "posted_prices_sha256": hashlib.sha256(posted_prices.read_bytes()).hexdigest(),
                       "calibration_price_inputs": inputs["prices"]},
    }
    root = Path(output)
    root.mkdir(parents=True, exist_ok=False)
    a, b = root / "origin", root / "client"
    a.mkdir(); b.mkdir()
    hosts = {kind: f"{kind}.lab.test" for kind in ("login", "manifest", "seg")}
    now = datetime.datetime.now(datetime.timezone.utc)
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    server_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Private experiment CA")])
    ca = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name)
          .public_key(ca_key.public_key()).serial_number(x509.random_serial_number())
          .not_valid_before(now-datetime.timedelta(days=1)).not_valid_after(now+datetime.timedelta(days=90))
          .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
          .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
          .add_extension(x509.KeyUsage(False, False, False, False, False, True, True, False, False), critical=True)
          .sign(ca_key, hashes.SHA256()))
    leaf = (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Lab origin")]))
            .issuer_name(ca_name).public_key(server_key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now-datetime.timedelta(days=1)).not_valid_after(now+datetime.timedelta(days=90))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(server_key.public_key()), critical=False)
            .add_extension(x509.KeyUsage(True, False, True, False, False, False, False, False, False), critical=True)
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(h) for h in hosts.values()]), critical=False)
            .add_extension(x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .sign(ca_key, hashes.SHA256()))
    (a / "lab-cert.pem").write_bytes(leaf.public_bytes(serialization.Encoding.PEM) + ca.public_bytes(serialization.Encoding.PEM))
    (a / "lab-key.pem").write_bytes(server_key.private_bytes(serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    (a / "lab-key.pem").chmod(0o600)
    (b / "lab-ca.pem").write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    token = secrets.token_urlsafe(48)
    for directory in (a, b):
        (directory / "admin.token").write_text(token, encoding="ascii")
        (directory / "admin.token").chmod(0o600)
    # CA private key is intentionally not persisted. Regenerate this whole bundle on expiry.
    origin = {"listen_ip": origin_ip, "port": 8443, "admin_port": 9080, "source_prefix": str(prefix),
              "hosts": hosts, "cert_file": "lab-cert.pem", "key_file": "lab-key.pem",
              "admin_token_file": "admin.token", "log": "origin-internal.jsonl"}
    pool = {"exit_prefix": str(prefix), "byte_budget": 230_000_000_000, "idle_timeout_s": 35,
            "listeners": {"clean-stick": 3128, "clean-rot": 3129, "dirty-stick": 3130},
            "targets": {h: {"ip": origin_ip, "port": 8443} for h in hosts.values()}}
    campaign = {"seed": 20261002, "control_port": 9081, "admin_token_file": "admin.token", "pool": pool,
                "job": {"ca_file": "lab-ca.pem", "cookie_name": "lab_session", "timeout_s": 30,
                        "targets": {kind: {"host": host, "port": 8443,
                            "path": "/seg/{index}" if kind == "seg" else "/" + kind}
                            for kind, host in hosts.items()}}, "prices": prices}
    for path, data in ((a / "origin.json", origin), (b / "campaign.json", campaign)):
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    (b / "exit-prefix.txt").write_text(str(prefix) + "\n", encoding="ascii")
    return root


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument("--origin-ipv6", required=True)
    parser.add_argument("--exit-prefix", required=True)
    inputs = Path(__file__).resolve().parent.parent / "inputs"
    parser.add_argument("--calibration", default=str(inputs / "calibration_inputs.json"))
    parser.add_argument("--posted-prices", default=str(inputs / "posted_prices.csv"))
    args = parser.parse_args()
    prepare(args.output, args.origin_ipv6, args.exit_prefix, args.calibration, args.posted_prices)
    print("Created private origin/ and client/ bundles. Keep keys, tokens and configs internal.")


if __name__ == "__main__":
    main()
