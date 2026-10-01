import importlib.util
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "daily_store_artifact", ROOT / "scripts" / "daily_store_artifact.py"
)
assert SPEC and SPEC.loader
daily_store_artifact = importlib.util.module_from_spec(SPEC)
sys.modules["daily_store_artifact"] = daily_store_artifact
SPEC.loader.exec_module(daily_store_artifact)


class SelectRecentSuccessfulRunTest(unittest.TestCase):
    def test_selects_previous_utc_date_run_after_midnight(self) -> None:
        now = datetime(2026, 9, 24, 0, 10, tzinfo=timezone.utc)
        runs = [
            {"databaseId": 100, "createdAt": "2026-09-23T22:00:00Z"},
            {"databaseId": 101, "createdAt": "2026-09-23T23:20:00Z"},
        ]

        self.assertEqual(
            daily_store_artifact.select_recent_successful_run(runs, now=now), "101"
        )

    def test_rejects_run_older_than_36_hours(self) -> None:
        now = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
        runs = [{"databaseId": 99, "createdAt": "2026-09-22T23:59:59Z"}]

        self.assertIsNone(
            daily_store_artifact.select_recent_successful_run(runs, now=now)
        )

    def test_returns_none_when_there_are_no_successful_runs(self) -> None:
        now = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)

        self.assertIsNone(daily_store_artifact.select_recent_successful_run([], now=now))


class SplitEventArtifactTest(unittest.TestCase):
    def test_missing_artifact_is_normal_when_daily_store_has_no_events(self) -> None:
        self.assertFalse(daily_store_artifact.has_split_event_artifact({"artifacts": []}))

    def test_detects_listed_split_event_artifact(self) -> None:
        response = {"artifacts": [{"name": "other"}, {"name": "split-event-feed"}]}

        self.assertTrue(daily_store_artifact.has_split_event_artifact(response))

    def test_detects_artifact_on_a_later_api_page(self) -> None:
        response = [
            {"artifacts": [{"name": "other"}]},
            {"artifacts": [{"name": "split-event-feed"}]},
        ]

        self.assertTrue(daily_store_artifact.has_split_event_artifact(response))


if __name__ == "__main__":
    unittest.main()
