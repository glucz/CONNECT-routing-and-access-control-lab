"""Timing boundary model and production pacing/reconnect coverage."""
import contextlib
import csv
import io
import json
import unittest

from .campaign import run_campaign
from .temporal import build_plan, ideal_arrivals, replay, report, validate_plan
from . import test_system


class ModelTests(unittest.TestCase):
    def test_default_grids_and_boundary(self):
        for block, count in (("rate-window", 48), ("reconnect", 20)):
            plan = build_plan(block)
            validate_plan(plan)
            self.assertEqual(len(plan["cells"]), count)
            self.assertEqual(plan["repetitions"], 1)
        cell = dict(window_s=2, limit=2, defender="every")
        arrivals = [dict(source_ip="held", arrival_monotonic_s=t,
                         **{"class": "login" if i == 0 else "seg"})
                    for i,t in enumerate((0, .5, 1., 1.99, 2., 2.1, 2.2))]
        expected = replay(cell, arrivals)
        self.assertEqual([x["status"] for x in expected], [200,200,403,403,200,200,403])
        self.assertEqual([x["window_count"] for x in expected], [1,2,3,4,1,2,3])
        self.assertTrue(expected[4]["window_reset"])

    def test_reconnect_predictions_and_plan_tampering(self):
        plan = build_plan("reconnect")
        for cell in plan["cells"]:
            expected = cell["reconnect_every"] if cell["defender"] == "bind" and cell["reconnect_source"] == "rotate" else 61
            self.assertEqual(cell["ideal_records_ok"], expected)
        plan["cells"][0]["ideal_records_ok"] = -1
        with self.assertRaises(ValueError):
            validate_plan(plan)


class TemporalIntegrationTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = test_system.SystemTests.asyncSetUp
    asyncTearDown = test_system.SystemTests.asyncTearDown

    async def test_paced_windows_and_cookie_preserving_reconnects(self):
        plan = build_plan("reconnect")
        base = plan["cells"][0]
        cells = []
        for defender in ("every", "bind"):
            for source in ("hold", "rotate"):
                cells.append(dict(base, cell_id=f"test-{defender}-{source}", defender=defender,
                                  count=6, reconnect_every=2, reconnect_source=source, request_interval_s=.02))
        cells.append(dict(base, cell_id="test-window", block="rate-window", count=8,
                          window_s=1, limit=2, request_interval_s=.3))
        for c in cells:
            c["ideal_records_ok"] = sum(r["status"] == 200 for r in replay(c, ideal_arrivals(c))[1:])
        plan["cells"] = cells
        path = self.root / "plan.json"
        path.write_text(json.dumps(plan))
        with contextlib.redirect_stdout(io.StringIO()):
            state = await run_campaign(self.config, self.root / "campaign", allocator=self.allocator,
                                       sensitivity_plan=path)
        self.assertEqual(state["status"], "complete")
        self.assertEqual(state["attempts"], 5)
        report(self.root / "campaign", self.root / "report")
        with (self.root / "report" / "delivery.csv").open() as f:
            records = {r["cell_id"]: r for r in csv.DictReader(f)}
        for r in records.values():
            self.assertEqual(r["valid"], "1")
            self.assertEqual(r["replay_mismatches"], "0")
            self.assertEqual(r["source_pattern_mismatches"], "0")
            self.assertEqual(r["tunnel_pattern_mismatches"], "0")
        for defender in ("every", "bind"):
            for source in ("hold", "rotate"):
                r = records[f"test-{defender}-{source}"]
                self.assertEqual(int(r["observed_records_ok"]), 2 if defender == "bind" and source == "rotate" else 6)
                self.assertEqual(r["forced_reconnects"], "2")
        self.assertGreaterEqual(int(records["test-window"]["window_resets"]), 2)
        self.assertGreaterEqual(float(records["test-window"]["duration_s"]), 2.4)
        timeline = (self.root / "report" / "timeline.csv").read_text()
        self.assertNotIn("127.0.", timeline)
        self.assertNotIn("run_id", timeline)


if __name__ == "__main__":
    unittest.main()
