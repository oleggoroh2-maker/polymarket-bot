import unittest
from paper_checkpoint_quality import audit_checkpoint_rows


class PaperCheckpointQualityTests(unittest.TestCase):
    def test_delayed_measurement_is_not_on_time(self):
        rows = [(1, "2026-01-01T00:00:00+00:00")]
        outcomes = [(1, 180, "2026-01-02T00:00:00+00:00")]
        result = audit_checkpoint_rows(rows, outcomes, "2026-01-03T00:00:00+00:00", (180,))
        self.assertEqual(result["180"]["delayed"], 1)
        self.assertEqual(result["180"]["delay_minutes_max"], 1260.0)

    def test_missing_and_not_yet_due_are_distinct(self):
        rows = [(1, "2026-01-01T00:00:00+00:00"), (2, "2026-01-02T23:00:00+00:00")]
        result = audit_checkpoint_rows(rows, [], "2026-01-03T00:00:00+00:00", (180,))
        self.assertEqual(result["180"]["eligible"], 1)
        self.assertEqual(result["180"]["missing"], 1)

    def test_utc_and_naive_timestamps(self):
        rows = [(7, "2026-01-01T00:00:00")]
        outcomes = [(7, 180, "2026-01-01T03:00:00Z")]
        result = audit_checkpoint_rows(rows, outcomes, "2026-01-01T04:00:00Z", (180,))
        self.assertEqual(result["180"]["on_time"], 1)


if __name__ == "__main__":
    unittest.main()
