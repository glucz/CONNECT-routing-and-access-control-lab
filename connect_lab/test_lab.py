"""Local-only integration tests; cryptography is a test-only dependency."""
import asyncio
import base64
import copy
import datetime
import ipaddress
import json
from pathlib import Path
import ssl
import unittest
import uuid

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from .driver import run_job
from .proxy import AddressPool, ExitProxy, SETS


class LoopbackPool:
    """Test substitution only: real TCP source binding on portable 127/8 addresses."""
    def __init__(self):
        self.held, self.labels = {}, {}
        self.serial = 1

    def allocate(self, exit_set, identity):
        key = (exit_set, identity)
        if SETS[exit_set][1] == "stick" and key in self.held:
            return self.held[key]
        self.serial += 1
        address = f"127.0.{self.serial // 250}.{self.serial % 250 + 1}"
        self.held[key] = address
        self.labels[address] = SETS[exit_set][0]
        return address


def certificate(directory):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Local lab test")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .add_extension(x509.SubjectAlternativeName([
                x509.DNSName(f"{kind}.lab.test") for kind in ("login", "manifest", "seg")]), critical=False)
            .sign(key, hashes.SHA256()))
    cert_path, key_path = directory / "cert.pem", directory / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                         serialization.PrivateFormat.PKCS8,
                                         serialization.NoEncryption()))
    return cert_path, key_path


class AllocationTests(unittest.TestCase):
    def test_ipv6_layout_identity_and_rotation(self):
        pool = AddressPool("2001:db8:1234:5678::/64")
        held = pool.allocate("clean-stick", "run.s0")
        self.assertEqual(held, pool.allocate("clean-stick", "run.s0"))
        addresses = [pool.allocate("clean-rot", "run.s0") for _ in range(1000)]
        self.assertEqual(len(set(addresses)), 1000)
        self.assertNotIn(held, addresses)
        for address in [held] + addresses:
            ip = ipaddress.IPv6Address(address)
            self.assertIn(ip, pool.network)
            self.assertEqual((int(ip) >> 48) & 0xffff, 0xc)
        dirty = pool.allocate("dirty-stick", "run.s0")
        self.assertEqual((int(ipaddress.IPv6Address(dirty)) >> 48) & 0xffff, 0xd)
        self.assertNotEqual(held, pool.allocate("clean-stick", "other.s0"))


class IntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Ordinary inherited permissions also work inside Windows restricted-token sandboxes.
        self.temp = Path(__file__).resolve().parent / ("test-work-" + uuid.uuid4().hex)
        self.temp.mkdir()
        cert, key = certificate(self.temp)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        self.allocator = LoopbackPool()
        self.policy, self.limit, self.truncate = "login", 0, False
        self.segment_delay = 0
        self.counts, self.origin_rows, self.proxy_rows = {}, [], []
        self.origin_tasks = set()
        self.origin = await asyncio.start_server(self.origin_request, "127.0.0.1", 0, ssl=context)
        port = self.origin.sockets[0].getsockname()[1]
        self.pool_config = {
            "exit_prefix": "2001:db8::/64", "byte_budget": 10_000_000,
            "idle_timeout_s": 2,
            "listeners": {name: 0 for name in SETS},
            "targets": {f"{kind}.lab.test": {"ip": "127.0.0.1", "port": port}
                        for kind in ("login", "manifest", "seg")},
        }
        self.proxy = ExitProxy(self.pool_config, self.proxy_rows.append, self.allocator)
        ports = await self.proxy.start()
        self.job_config = {
            "workload": "video", "routing": "R-clean-stick", "count": 6,
            "body_bytes": 128, "manifest_bytes": 32, "reuse": 2, "sessions": 1,
            "timeout_s": 2, "ca_file": str(cert), "proxies": ports,
            "targets": {kind: {"host": f"{kind}.lab.test", "port": port,
                               "path": "/seg/{index}" if kind == "seg" else f"/{kind}"}
                        for kind in ("login", "manifest", "seg")},
        }

    async def asyncTearDown(self):
        await self.proxy.close()
        self.origin.close()
        await self.origin.wait_closed()
        for task in list(self.origin_tasks):
            task.cancel()
        if self.origin_tasks:
            await asyncio.gather(*list(self.origin_tasks), return_exceptions=True)
        for name in ("cert.pem", "key.pem"):
            (self.temp / name).unlink()
        self.temp.rmdir()

    async def origin_request(self, reader, writer):
        """Small test fixture, NOT the production defender or its signed-cookie implementation."""
        task = asyncio.current_task()
        self.origin_tasks.add(task)
        source = writer.get_extra_info("peername")[0]
        try:
            while True:
                raw = await reader.readuntil(b"\r\n\r\n")
                lines = raw.decode().split("\r\n")
                path = lines[0].split()[1]
                kind = "seg" if path.startswith("/seg/") else path[1:]
                headers = dict((k.lower(), v.strip()) for k, v in
                               (line.split(":", 1) for line in lines[1:] if line))
                self.counts[source] = self.counts.get(source, 0) + 1
                clean = self.allocator.labels[source] == "clean"
                reason = ""
                if kind == "login" and not clean:
                    reason = "login_denied"
                elif kind != "login":
                    cookie = headers.get("cookie", "")
                    if not cookie.startswith("lab_session="):
                        reason = "no_session"
                    elif self.policy == "bind" and cookie != "lab_session=" + source:
                        reason = "bind_mismatch"
                    elif self.policy == "every" and not clean:
                        reason = "dirty_exit"
                if not reason and self.limit and self.counts[source] > self.limit:
                    reason = "rate"
                size = 0 if reason else {"login": 2, "manifest": 32, "seg": 128}[kind]
                if kind == "seg" and self.segment_delay:
                    await asyncio.sleep(self.segment_delay)
                status = 403 if reason else 200
                response = f"HTTP/1.1 {status} Test\r\nContent-Length: {size}\r\nX-Lab-Reason: {reason}\r\n"
                if kind == "login" and not reason:
                    response += f"Set-Cookie: lab_session={source}; Secure; HttpOnly\r\n"
                writer.write((response + "\r\n").encode())
                writer.write(b"x" * (5 if self.truncate and kind == "seg" else size))
                await writer.drain()
                self.origin_rows.append({"source": source, "class": kind, "status": status,
                                         "tunnel_id": headers["x-lab-tunnel-id"]})
                if self.truncate and kind == "seg":
                    return
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (OSError, asyncio.CancelledError):
                pass
            self.origin_tasks.discard(task)

    async def job(self, **changes):
        config = copy.deepcopy(self.job_config)
        config.update(changes)
        rows = []
        result = await asyncio.to_thread(run_job, config, rows.append)
        return result, rows

    async def test_all_24_route_workload_defender_cells(self):
        for workload in ("video", "scrape"):
            for policy in ("login", "every", "bind"):
                self.policy = policy
                for routing in ("R-dirty-stick", "R-clean-rot", "R-split", "R-clean-stick"):
                    with self.subTest(workload=workload, policy=policy, routing=routing):
                        result, rows = await self.job(workload=workload, routing=routing)
                        expected = routing == "R-clean-stick" or (
                            routing == "R-clean-rot" and policy != "bind") or (
                            routing == "R-split" and policy == "login")
                        self.assertEqual(result["success"], expected, rows)
                        self.assertTrue(result["valid"], rows)
                        if workload == "scrape":
                            self.assertEqual(result["n_requests"], 7)  # no retry or early denial stop

    async def test_real_source_binding_reuse_and_cookie_sharing(self):
        result, rows = await self.job(routing="R-clean-stick")
        self.assertTrue(result["success"])
        self.assertEqual(len({r["source"] for r in self.origin_rows}), 1)
        self.origin_rows.clear()
        result, rows = await self.job(routing="R-clean-rot")
        self.assertTrue(result["success"])
        # Login + manifest + 3 segment tunnels, each a different real TCP source.
        self.assertEqual(len({r["source"] for r in self.origin_rows}), 5)
        self.assertEqual(len({r["tunnel_id"] for r in rows}), 5)
        await asyncio.sleep(.05)
        events = [e for e in self.proxy_rows if e.get("tunnel_id") in {r["tunnel_id"] for r in rows}]
        self.assertEqual(len(events), 5)
        self.assertTrue(all(e["source_ip"] == e["bound_source_ip"] for e in events))
        self.assertGreater(sum(e["bytes_down"] for e in events), result["body_bytes"])

    async def test_rate_limit_rotation_and_multi_session_capacity(self):
        self.policy, self.limit = "every", 3
        rotating, _ = await self.job(workload="scrape", routing="R-clean-rot", count=8)
        held, _ = await self.job(workload="scrape", count=8)
        self.assertTrue(rotating["success"])
        self.assertFalse(held["success"])
        self.policy = "bind"
        rotating, _ = await self.job(workload="scrape", routing="R-clean-rot", count=8)
        held, _ = await self.job(workload="scrape", count=8, sessions=4)
        self.assertFalse(rotating["success"])
        self.assertTrue(held["success"])
        self.assertEqual(held["n_requests"], 12)

    async def test_truncated_200_is_not_delivery(self):
        self.truncate = True
        result, rows = await self.job()
        self.assertFalse(result["success"])
        self.assertFalse(result["valid"])
        self.assertEqual(result["records_ok"], 0)
        self.assertEqual(rows[-1]["body_bytes"], 5)

    async def test_wrong_body_size_is_invalid_not_a_defender_denial(self):
        result, rows = await self.job(body_bytes=127)
        self.assertFalse(result["success"])
        self.assertFalse(result["valid"])
        self.assertEqual(rows[-1]["error"], "BodySizeMismatch")

    async def test_unlisted_destination_never_connects(self):
        port = self.proxy.servers[0].sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"CONNECT external.invalid:443 HTTP/1.1\r\n\r\n")
        await writer.drain()
        self.assertIn(b"403", await reader.readline())
        writer.close()
        await writer.wait_closed()
        self.assertEqual(self.origin_rows, [])

    async def test_shared_hard_budget_stops_delivery(self):
        self.proxy.budget = 100
        result, _ = await self.job()
        self.assertFalse(result["success"])
        self.assertLessEqual(self.proxy.used, 100)
        self.assertTrue(self.proxy.exhausted)

    async def test_concurrent_jobs_share_one_budget(self):
        self.proxy.budget = 12000
        results = await asyncio.gather(self.job(routing="R-clean-rot"), self.job(routing="R-split"))
        self.assertTrue(self.proxy.exhausted)
        self.assertLessEqual(self.proxy.used, self.proxy.budget)
        self.assertTrue(any(not result[0]["success"] for result in results))
        await self.proxy.close()
        self.assertEqual(sum(e.get("bytes_up", 0) + e.get("bytes_down", 0)
                             for e in self.proxy_rows), self.proxy.used)
        self.assertEqual(sum(e.get("event") == "budget_exhausted" for e in self.proxy_rows), 1)

    async def test_certificate_hostname_is_verified(self):
        config = copy.deepcopy(self.job_config)
        port = config["targets"]["login"]["port"]
        self.proxy.targets[("wrong.lab.test", port)] = "127.0.0.1"
        config["targets"]["login"]["host"] = "wrong.lab.test"
        rows = []
        result = await asyncio.to_thread(run_job, config, rows.append)
        self.assertFalse(result["success"])
        self.assertEqual(rows[0]["error"], "SSLCertVerificationError")

    async def test_request_deadline(self):
        self.segment_delay = .5
        result, rows = await self.job(timeout_s=.2)
        self.assertFalse(result["success"])
        self.assertFalse(result["valid"])
        self.assertLess(result["duration_s"], 1.5)


if __name__ == "__main__":
    unittest.main()
