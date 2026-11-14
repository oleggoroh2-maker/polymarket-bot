"""Synthetic SQLite tests for the read-only v2/v4 aggregate report."""
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pilot_live_readonly_report import analyze


class LiveReportTests(unittest.TestCase):
    def test_counts_timing_and_no_mutation(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "paper.db"
            conn = sqlite3.connect(path)
            conn.execute("CREATE TABLE entry_discovery_candidates(id INTEGER PRIMARY KEY, opened_at TEXT)")
            conn.execute("CREATE TABLE entry_discovery_outcomes(candidate_id INTEGER, checkpoint_minutes INTEGER, measured_at TEXT, roi REAL)")
            for table in ("pilot_engine_v2_decisions", "pilot_engine_v4_decisions"):
                conn.execute(f"CREATE TABLE {table}(candidate_id INTEGER, version TEXT, action TEXT, stake REAL)")
            now = datetime.now(timezone.utc)
            opened = now - timedelta(hours=5)
            conn.executemany("INSERT INTO entry_discovery_candidates VALUES (?,?)",
                             [(1, opened.isoformat()), (2, opened.isoformat()), (3, opened.isoformat())])
            conn.executemany("INSERT INTO pilot_engine_v2_decisions VALUES (?,?,?,?)",
                             [(1, "v2-pilot-engine", "TRADE", 20),
                              (2, "wrong-version", "TRADE", 20),
                              (3, "v2-pilot-engine", "SKIP", 0)])
            conn.execute("INSERT INTO pilot_engine_v4_decisions VALUES (?,?,?,?)",
                         (2, "v4-category-filter-future-only", "TRADE", 20))
            conn.executemany("INSERT INTO entry_discovery_outcomes VALUES (?,?,?,?)",
                             [(1, 180, (opened + timedelta(minutes=190)).isoformat(), 10),
                              (2, 180, (opened + timedelta(minutes=220)).isoformat(), -5)])
            conn.commit()
            before = conn.total_changes
            result = analyze(path)
            self.assertEqual(result["strategies"]["v2"]["trades"], 1)
            self.assertEqual(result["strategies"]["v4"]["trades"], 1)
            v2 = result["strategies"]["v2"]["horizons"]["180"]
            v4 = result["strategies"]["v4"]["horizons"]["180"]
            self.assertEqual(v2["due"], 1)
            self.assertEqual(v2["recorded"], 1)
            self.assertEqual(v2["on_time_0_to_15m"], 1)
            self.assertEqual(v2["pnl_usd"], 2.0)
            self.assertEqual(v4["late_over_15m"], 1)
            self.assertEqual(v4["pnl_usd"], -1.0)
            self.assertEqual(v2["on_time_only"]["n"], 1)
            self.assertEqual(v2["on_time_only"]["pnl_usd"], 2.0)
            self.assertEqual(v2["on_time_only"]["mean_roi_pct"], 10.0)
            self.assertEqual(v2["on_time_only"]["win_rate_pct"], 100.0)
            self.assertEqual(v4["on_time_only"]["n"], 0)
            self.assertEqual(v4["on_time_only"]["pnl_usd"], 0.0)
            self.assertEqual(conn.total_changes, before)
            conn.close()


if __name__ == "__main__":
    unittest.main()
