"""End-to-end tests using the production origin, orchestrator and analyzer on loopback."""
import asyncio
import contextlib
import io
import json
from pathlib import Path
import shutil
import ssl
import unittest
from unittest.mock import patch
import uuid

from .analyze import analyze, fisher_greater, wilson
from .campaign import Control, matrix, run_campaign
from .driver import run_job
from .origin import Origin
from .prepare import prepare
from .test_lab import certificate, LoopbackPool


class SystemTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.root = Path(__file__).resolve().parent / ("test-work-" + uuid.uuid4().hex)
        self.root.mkdir()
        cert, key = certificate(self.root)
        token = self.root / "admin.token"
        token.write_text("test-only-" + "a" * 40)
        self.allocator = LoopbackPool()
        self.origin_log = []
        config = {"source_prefix": "2001:db8:1::/64", "admin_token_file": str(token),
                  "cert_file": str(cert), "key_file": str(key), "listen_ip": "127.0.0.1",
                  "port": 0, "admin_port": 0,
                  "hosts": {k: f"{k}.lab.test" for k in ("login", "manifest", "seg")}}
        self.origin = Origin(config, self.origin_log.append, self.allocator.labels.get)
        ports = await self.origin.start()
        self.control = Control(ports["admin"], str(token))
        self.config = {"seed": 42, "control_port": ports["admin"], "admin_token_file": str(token),
            "pool": {"exit_prefix": "2001:db8:1::/64", "byte_budget": 10_000_000,
                     "listeners": {k: 0 for k in ("clean-stick", "clean-rot", "dirty-stick")},
                     "targets": {h: {"ip": "127.0.0.1", "port": ports["https"]} for h in config["hosts"].values()}},
            "job": {"ca_file": str(cert), "timeout_s": 3,
                    "targets": {k: {"host": h, "port": ports["https"],
                                      "path": "/seg/{index}" if k == "seg" else "/" + k}
                                for k, h in config["hosts"].items()}},
            "prices": {"clean_usd_per_gb": 1.45, "dirty_usd_per_gb": .38,
                       "held_usd_per_address_month": 1.25, "own_path_range": [.0000276, .12]}}

    async def asyncTearDown(self):
        await self.origin.close()
        # Cleanup is confined to this test's freshly created workspace directory.
        if self.root.parent != Path(__file__).resolve().parent or not self.root.name.startswith("test-work-"):
            raise RuntimeError("unsafe test cleanup path")
        shutil.rmtree(self.root)

    async def test_complete_smoke_matrix_and_anonymous_analysis(self):
        output = self.root / "campaign"
        with contextlib.redirect_stdout(io.StringIO()):
            status = await run_campaign(self.config, output, smoke=True, allocator=self.allocator)
        self.assertEqual(status["status"], "complete")
        self.assertEqual(status["attempts"], 34)
        self.assertEqual(len(self.origin.rows), 0)  # acknowledged rows do not accumulate in memory
        summary = analyze(output)
        self.assertEqual(len(summary["cells"]), 34)
        self.assertEqual(summary["accounting_discrepancies_over_one_percent"], 0)
        for cell in summary["cells"]:
            with self.subTest(cell=cell["cell_id"]):
                if cell["block"] == 1:
                    expected = cell["routing"] == "R-clean-stick" or (
                        cell["routing"] == "R-clean-rot" and cell["defender"] != "bind") or (
                        cell["routing"] == "R-split" and cell["defender"] == "login")
                else:
                    expected = cell["sessions"] > 1 or (
                        cell["routing"] == "R-clean-rot" and cell["defender"] == "every")
                self.assertEqual(cell["valid"], 1)
                self.assertEqual(cell["successes"], int(expected))
        serialized = json.dumps(summary)
        for forbidden in ("127.0.", "source_ip", "run_id", "session_id", str(self.root)):
            self.assertNotIn(forbidden, serialized)
        full = matrix(False)
        self.assertEqual(len(full)*30, 1020)
        self.assertEqual({c["sessions"] for c in full if c["sessions"] > 1}, {6, 21})

    async def test_hmac_cookie_tampering_session_binding_and_epoch(self):
        self.origin.set_policy(dict(defender="bind", cell_id="test", limit=0,
                                    body_bytes=128, manifest_bytes=32, window_s=3600))
        cookie = self.origin.sign({"epoch": self.origin.epoch, "run": "r", "session": "s", "source": "::1"})
        self.assertIsNotNone(self.origin.verify("lab_session=" + cookie, "r", "s"))
        self.assertIsNone(self.origin.verify("lab_session=" + cookie, "r", "other"))
        self.assertIsNone(self.origin.verify("lab_session=" + cookie + "x", "r", "s"))
        self.origin.set_policy(dict(self.origin.policy))
        self.assertIsNone(self.origin.verify("lab_session=" + cookie, "r", "s"))

    async def test_control_requires_token(self):
        self.control.token = "incorrect"
        with self.assertRaises(RuntimeError):
            await asyncio.to_thread(self.control.call, "GET", "/health")

    async def test_pilot_planning_cap_and_partial_matrix_analysis(self):
        self.config["pool"]["byte_budget"] = 230_000_000_000
        jobs = []
        def record_job(job, log):
            # Verify full-size planning without transferring two GB in the unit test.
            jobs.append(job)
            return dict(run_id=job["run_id"], routing=job["routing"], valid=True, success=False)
        output = self.root / "pilot"
        with patch("connect_lab.campaign.run_job", side_effect=record_job), contextlib.redirect_stdout(io.StringIO()):
            result = await run_campaign(self.config, output, pilot=True, allocator=self.allocator)
        self.assertEqual(result["mode"], "pilot")
        self.assertEqual(result["byte_budget"], 3_000_000_000)
        self.assertEqual(result["planned_runs"], 4)
        self.assertEqual(len(jobs), 4)
        self.assertTrue(all(j["count"] == 1000 and j["body_bytes"] == 1_000_000 and j["reuse"] == 100 for j in jobs))
        summary = analyze(output)
        self.assertEqual(summary["mode"], "pilot")
        self.assertEqual(len(summary["cells"]), 4)
        self.assertTrue(any(c["one_sided_fisher_p"] is None for c in summary["h1_comparisons"]))

    async def test_infrastructure_retry_is_preserved_and_denial_not_retried(self):
        calls = 0
        def fail_once(job, log):
            nonlocal calls
            calls += 1
            job = json.loads(json.dumps(job))
            if calls == 1:
                job["targets"]["login"]["host"] = "unlisted.lab.test"
            return run_job(job, log)
        output = self.root / "retry"
        # One dirty-held video cell: failed CONNECT then a valid login denial.
        cell = matrix(True)[0]
        with patch("connect_lab.campaign.matrix", return_value=[cell]), \
                patch("connect_lab.campaign.run_job", side_effect=fail_once), \
                contextlib.redirect_stdout(io.StringIO()):
            result = await run_campaign(self.config, output, smoke=True, allocator=self.allocator)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["attempts"], 2)
        attempts = [json.loads(line) for line in (output / "runs.jsonl").read_text().splitlines()]
        self.assertFalse(attempts[0]["valid"])
        self.assertTrue(attempts[1]["valid"])
        self.assertFalse(attempts[1]["success"])
        self.assertNotEqual(attempts[0]["run_id"], attempts[1]["run_id"])

    async def test_budget_exhaustion_remains_incomplete(self):
        self.config["pool"]["byte_budget"] = 100
        output = self.root / "interrupted"
        with contextlib.redirect_stdout(io.StringIO()):
            status = await run_campaign(self.config, output, smoke=True, allocator=self.allocator)
        self.assertEqual(status["status"], "budget_exhausted")
        summary = analyze(output)
        self.assertEqual(sum(c["valid"] for c in summary["cells"]), 0)
        self.assertGreater(sum(c["missing"] for c in summary["cells"]), 0)

    async def test_prepare_bundle_and_price_provenance(self):
        inputs = Path(__file__).resolve().parents[1] / "inputs"
        calibration = inputs / "calibration_inputs.json"
        posted_prices = inputs / "posted_prices.csv"
        if not calibration.is_file() or not posted_prices.is_file():
            self.skipTest("Preparation test needs the repository inputs/ directory; runtime servers may omit it.")
        out = self.root / "bundle"
        prepare(out, "2001:db8:2::10", "2001:db8:1::/64",
                calibration, posted_prices)
        config = json.loads((out / "client/campaign.json").read_text())
        self.assertEqual(config["prices"]["dirty_usd_per_gb"], .38)
        self.assertEqual(len(config["prices"]["provenance"]["datacenter_rows"]), 5)
        self.assertFalse((out / "client/lab-key.pem").exists())
        self.assertEqual((out / "client/admin.token").read_bytes(), (out / "origin/admin.token").read_bytes())
        origin_config = json.loads((out / "origin/origin.json").read_text())
        for name in ("cert_file", "key_file", "admin_token_file"):
            origin_config[name] = str(out / "origin" / origin_config[name])
        origin_config.update(listen_ip="127.0.0.1", port=0, admin_port=0)
        prepared = Origin(origin_config, lambda row: None, lambda address: "clean")
        try:
            ports = await prepared.start()
            context = ssl.create_default_context(cafile=str(out / "client/lab-ca.pem"))
            reader, writer = await asyncio.open_connection("127.0.0.1", ports["https"],
                ssl=context, server_hostname="login.lab.test")
            writer.close()
            await writer.wait_closed()
        finally:
            await prepared.close()


class StatisticsTests(unittest.TestCase):
    def test_known_intervals_and_exact_test(self):
        self.assertAlmostEqual(wilson(0, 30)[1], .1135133931739688)
        self.assertAlmostEqual(fisher_greater(3, 3, 0, 3), .05)
        self.assertIsNone(wilson(0, 0))


if __name__ == "__main__":
    unittest.main()
