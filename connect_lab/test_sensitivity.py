"""Independent request-level predictions and production integration checks."""
import contextlib
import io
import importlib.util
import json
from pathlib import Path
import unittest

from . import test_system as fixture
from .campaign import run_campaign
from .netstats import delta
from .sensitivity import build_plan, predict, validate_plan, traffic_estimate
from .sensitivity_report import report


def reference_records(cell):
    """Stateful origin/connection model, independent of the closed-form predictor."""
    total = 0
    for session in range(cell["sessions"]):
        count = cell["count"] // cell["sessions"] + (session < cell["count"] % cell["sessions"])
        connections, requests = {}, {}
        cookie = None
        sequence = ["login"] + (["manifest"] if cell["workload"] == "video" else []) + ["seg"]*count
        serial = 0
        for kind in sequence:
            route = cell["routing"]
            label = "dirty" if route == "R-split" and kind == "seg" else "clean"
            key = (kind, label)
            connection = connections.get(key)
            if connection is None or connection[1] == cell["reuse"]:
                serial += 1
                source = (label, serial) if route == "R-clean-rot" else (label, 0)
                connection = [source, 0]
                connections[key] = connection
            source = connection[0]
            connection[1] += 1
            requests[source] = requests.get(source, 0) + 1
            allowed = label == "clean" if kind == "login" else cookie is not None
            if kind != "login" and cell["defender"] == "bind":
                allowed = allowed and source == cookie
            if kind != "login" and cell["defender"] == "every":
                allowed = allowed and label == "clean"
            allowed = allowed and (not cell["limit"] or requests[source] <= cell["limit"])
            if kind == "login" and allowed:
                cookie = source
            if kind == "seg" and allowed:
                total += 1
            if not allowed and cell["workload"] == "video":
                break
    return total


class PredictionTests(unittest.TestCase):
    def test_every_default_cell_against_stateful_reference(self):
        for block, number in (("capacity", 120), ("composition", 81)):
            plan = build_plan(block)
            validate_plan(plan)
            self.assertEqual(len(plan["cells"]), number)
            for cell in plan["cells"]:
                with self.subTest(cell=cell["cell_id"]):
                    self.assertEqual(predict(cell)["records_ok"], reference_records(cell))

    def test_thresholds_and_reuse_boundary(self):
        plan = build_plan("capacity")
        boundary = next(c for c in plan["cells"] if c["block"] == "session_threshold" and c["limit"] == 334 and c["sessions"] == 3)
        self.assertEqual(boundary["prediction"]["records_ok"], 999)
        self.assertTrue(boundary["prediction"]["success_99"])
        self.assertFalse(boundary["prediction"]["complete_delivery"])
        rotation = next(c for c in plan["cells"] if c["routing"] == "R-clean-rot" and c["defender"] == "every" and c["reuse"] == 100 and c["limit"] == 99)
        self.assertEqual(rotation["prediction"]["records_ok"], 990)
        rotation["prediction"]["records_ok"] = 1000
        with self.assertRaises(ValueError):
            validate_plan(plan)

    def test_interface_reset_not_negative_traffic(self):
        result = delta({"interfaces": {"eth0": {"rx_bytes": 10, "tx_bytes": 20}}},
                       {"interfaces": {"eth0": {"rx_bytes": 5, "tx_bytes": 30}}})
        self.assertIsNone(result["interfaces"]["eth0"]["rx_bytes"])
        self.assertEqual(result["interfaces"]["eth0"]["tx_bytes"], 10)


class SensitivityIntegrationTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.SystemTests.asyncSetUp
    asyncTearDown = fixture.SystemTests.asyncTearDown

    async def test_saved_plan_delivery_price_report_and_counters(self):
        plan = build_plan("capacity", repetitions=1)
        # Real socket/TLS tests of partial rotation and the 99%-versus-complete boundary.
        cells = []
        for route, defender, limit, sessions, count, reuse in (
            ("R-clean-rot", "every", 9, 1, 20, 10),
            ("R-clean-rot", "bind", 9, 1, 20, 10),
            ("R-clean-stick", "bind", 34, 3, 100, 25),
            ("R-clean-stick", "bind", 34, 4, 100, 25),
        ):
            c = dict(plan["cells"][0], routing=route, defender=defender, limit=limit,
                     sessions=sessions, count=count, reuse=reuse, body_bytes=128, cell_id=f"test-{len(cells)}")
            if len(cells) >= 2:
                c["block"] = "session_threshold"
            c["prediction"] = predict(c)
            cells.append(c)
        plan["cells"] = cells
        plan["estimate"] = traffic_estimate(cells, 1)
        plan["byte_budget"] = 1_000_000
        path = self.root / "plan.json"
        path.write_text(json.dumps(plan))
        with contextlib.redirect_stdout(io.StringIO()):
            status = await run_campaign(self.config, self.root / "campaign", sensitivity_plan=path, allocator=self.allocator)
        self.assertEqual(status["mode"], "sensitivity")
        self.assertEqual(status["status"], "complete")
        self.assertEqual(status["attempts"], 4)
        self.assertIn("network", status)
        summary = report(self.root / "campaign", self.root / "report", plots=bool(importlib.util.find_spec("matplotlib")))
        self.assertTrue(all(c["prediction_mismatches"] == 0 for c in summary["cells"]))
        self.assertTrue(all(c["address_prediction_mismatches"] == 0 for c in summary["cells"]))
        self.assertTrue(all(c["tunnel_prediction_mismatches"] == 0 for c in summary["cells"]))
        threshold = summary["cells"][2]
        self.assertEqual(threshold["successes_99"], 1)
        self.assertEqual(threshold["complete_deliveries"], 0)
        self.assertEqual(summary["cells"][3]["complete_deliveries"], 1)
        for artifact in ("aggregates.json", "prices.csv", "delivery.csv"):
            raw = (self.root / "report" / artifact).read_text()
            self.assertNotIn("127.0.", raw)
            self.assertNotIn("source_ip", raw)

    async def test_composition_plan_changes_manifest_and_exports_plot(self):
        plan = build_plan("composition", repetitions=1)
        cells = [dict(c) for c in plan["cells"] if c["count"] == 10 and c["manifest_bytes"] == 1000
                 and (c["defender"], c["routing"]) in (("login", "R-split"), ("bind", "R-split"), ("every", "R-clean-rot"))]
        for c in cells:
            c.update(count=4, body_bytes=128, manifest_bytes=4096)
            c["prediction"] = predict(c)
        plan["cells"] = cells
        plan["estimate"] = traffic_estimate(cells, 1)
        path = self.root / "composition.json"
        path.write_text(json.dumps(plan))
        with contextlib.redirect_stdout(io.StringIO()):
            state = await run_campaign(self.config, self.root / "composition", sensitivity_plan=path, allocator=self.allocator)
        self.assertEqual(state["status"], "complete")
        result = report(self.root / "composition", self.root / "figures", plots=bool(importlib.util.find_spec("matplotlib")))
        self.assertTrue(all(c["prediction_mismatches"] == 0 for c in result["cells"]))
        self.assertEqual(sum(c["complete_deliveries"] for c in result["cells"]), 2)


if __name__ == "__main__":
    unittest.main()
