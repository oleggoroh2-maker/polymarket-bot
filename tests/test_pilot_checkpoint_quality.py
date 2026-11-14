import sqlite3
import tempfile
import unittest
from pathlib import Path

from pilot_checkpoint_quality import audit_pilot_database


class PilotAuditTests(unittest.TestCase):
    def test_v2_v4_are_separated_and_only_trade_counted(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Path(folder) / "audit.sqlite"
            with sqlite3.connect(db) as conn:
                conn.executescript("""
                CREATE TABLE entry_discovery_candidates(id INTEGER PRIMARY KEY, opened_at TEXT);
                CREATE TABLE entry_discovery_outcomes(candidate_id INTEGER, checkpoint_minutes INTEGER, measured_at TEXT);
                CREATE TABLE pilot_engine_v2_decisions(candidate_id INTEGER, action TEXT);
                CREATE TABLE pilot_engine_v4_decisions(candidate_id INTEGER, action TEXT);
                INSERT INTO entry_discovery_candidates VALUES (1,'2026-01-01T00:00:00+00:00');
                INSERT INTO entry_discovery_candidates VALUES (2,'2026-01-01T00:00:00+00:00');
                INSERT INTO pilot_engine_v2_decisions VALUES (1,'TRADE');
                INSERT INTO pilot_engine_v2_decisions VALUES (2,'SKIP');
                INSERT INTO pilot_engine_v4_decisions VALUES (2,'TRADE');
                INSERT INTO entry_discovery_outcomes VALUES (1,180,'2026-01-01T03:30:00+00:00');
                """)
            result = audit_pilot_database(db, now="2026-01-03T00:00:00+00:00")
            self.assertEqual(result["v2"]["trades"], 1)
            self.assertEqual(result["v2"]["checkpoint_quality"]["180"]["delayed"], 1)
            self.assertEqual(result["v4"]["checkpoint_quality"]["180"]["missing"], 1)


if __name__ == "__main__":
    unittest.main()
