import importlib.util
import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load(name: str):
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "scripts" / f"{name}.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


build_ir_review_queue = load("build_ir_review_queue")
fetch_forecasts = load("fetch_forecasts")


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as target:
        json.dump(data, target, ensure_ascii=False)


class ConstantsStayInSyncTest(unittest.TestCase):
    """fetch_forecasts.py 側の上限値と乖離したら「近い」の意味が変わるので、
    一致していることをテストで保証する。"""

    def test_pending_limits_match_fetch_forecasts(self) -> None:
        self.assertEqual(
            build_ir_review_queue.EVENT_PENDING_MAX_ATTEMPTS,
            fetch_forecasts.EVENT_PENDING_MAX_ATTEMPTS,
        )
        self.assertEqual(
            build_ir_review_queue.EVENT_PENDING_MAX_DAYS,
            fetch_forecasts.EVENT_PENDING_MAX_DAYS,
        )


class CodeResolverTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.edinet_dir = Path(self.directory.name)
        write_json(
            self.edinet_dir / "4746.json",
            {"code": "4746", "name": "東計電算", "edinetCode": "E05066"},
        )
        self.resolver = build_ir_review_queue.CodeResolver(self.edinet_dir)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_resolves_direct_sec_code(self) -> None:
        self.assertEqual(self.resolver.resolve("4746", ""), "4746")

    def test_resolves_five_digit_trailing_zero(self) -> None:
        self.assertEqual(self.resolver.resolve("47460", ""), "4746")

    def test_falls_back_to_edinet_code_index(self) -> None:
        self.assertEqual(self.resolver.resolve("99999", "E05066"), "4746")

    def test_unresolvable_returns_none(self) -> None:
        self.assertIsNone(self.resolver.resolve("0001", "E99999"))

    def test_name_for_reads_the_feed(self) -> None:
        self.assertEqual(self.resolver.name_for("4746"), "東計電算")

    def test_name_for_missing_code_is_empty(self) -> None:
        self.assertEqual(self.resolver.name_for("9999"), "")


class ScanPendingSplitEventsTest(unittest.TestCase):
    def test_keeps_only_split_and_reverse_split(self) -> None:
        state = {
            "events": {
                "pending": {
                    "a": {"eventType": "stock_split"},
                    "b": {"eventType": "reverse_split"},
                    "c": {"eventType": "dividend_revision"},
                    "d": {"eventType": "earnings_summary"},
                }
            }
        }
        result = build_ir_review_queue.scan_pending_split_events(state)
        self.assertEqual(set(result), {"a", "b"})

    def test_missing_events_block_is_empty(self) -> None:
        self.assertEqual(build_ir_review_queue.scan_pending_split_events({}), {})


class ClassifyOverflowRiskTest(unittest.TestCase):
    def test_safe_when_low_attempts_and_fresh(self) -> None:
        near, reason = build_ir_review_queue.classify_overflow_risk(1, 2)
        self.assertFalse(near)
        self.assertIsNone(reason)

    def test_near_limit_by_attempts(self) -> None:
        near, reason = build_ir_review_queue.classify_overflow_risk(4, 1)
        self.assertTrue(near)
        self.assertEqual(reason, "attempts_near_limit")

    def test_near_limit_by_age(self) -> None:
        near, reason = build_ir_review_queue.classify_overflow_risk(1, 12)
        self.assertTrue(near)
        self.assertEqual(reason, "age_near_limit")


class LookupIrUrlTest(unittest.TestCase):
    def test_prefers_confirmed_ir_sites(self) -> None:
        ir_sites = {"4746": {"irTopUrl": "https://confirmed.example/"}}
        candidates = {"sites": {"4746": {"irTopUrl": "https://candidate.example/"}}}
        url, source = build_ir_review_queue.lookup_ir_url("4746", ir_sites, candidates)
        self.assertEqual(url, "https://confirmed.example/")
        self.assertEqual(source, "ir_sites")

    def test_falls_back_to_candidates(self) -> None:
        candidates = {"sites": {"4746": {"irTopUrl": "https://candidate.example/"}}}
        url, source = build_ir_review_queue.lookup_ir_url("4746", {}, candidates)
        self.assertEqual(url, "https://candidate.example/")
        self.assertEqual(source, "ir_sites_candidates")

    def test_not_found(self) -> None:
        url, source = build_ir_review_queue.lookup_ir_url("4746", {}, {})
        self.assertIsNone(url)
        self.assertEqual(source, "not_found")


class BuildPendingSplitEntriesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.edinet_dir = Path(self.directory.name)
        write_json(
            self.edinet_dir / "4746.json",
            {"code": "4746", "name": "東計電算", "edinetCode": "E05066"},
        )
        self.resolver = build_ir_review_queue.CodeResolver(self.edinet_dir)
        self.ir_sites = {"4746": {"irTopUrl": "https://toukei.example/ir/"}}
        self.today = date(2026, 8, 15)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_safe_pending_event_is_secondary_entrance(self) -> None:
        state = {
            "events": {
                "pending": {
                    "evt-1": {
                        "eventType": "stock_split",
                        "eventDate": "2026-08-10",
                        "secCode": "4746",
                        "edinetCode": "E05066",
                        "firstSeenAt": "2026-08-14",
                        "attempts": 1,
                    }
                },
                "seen": {},
            }
        }
        entries, tracked = build_ir_review_queue.build_pending_split_entries(
            state, self.resolver, self.ir_sites, {}, {}, self.today
        )
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry["trigger"], "edinetdb_event")
        self.assertEqual(entry["code"], "4746")
        self.assertEqual(entry["name"], "東計電算")
        self.assertEqual(entry["detail"]["eventId"], "evt-1")
        self.assertIn("evt-1", tracked)

    def test_near_attempts_limit_is_primary_entrance_with_ir_url(self) -> None:
        state = {
            "events": {
                "pending": {
                    "evt-1": {
                        "eventType": "stock_split",
                        "eventDate": "2026-08-10",
                        "secCode": "4746",
                        "edinetCode": "E05066",
                        "firstSeenAt": "2026-08-14",
                        "attempts": 4,
                    }
                },
                "seen": {},
            }
        }
        entries, _tracked = build_ir_review_queue.build_pending_split_entries(
            state, self.resolver, self.ir_sites, {}, {}, self.today
        )
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry["trigger"], "api_overflow")
        self.assertEqual(entry["detail"]["reason"], "attempts_near_limit")
        self.assertEqual(entry["detail"]["irUrl"], "https://toukei.example/ir/")
        self.assertEqual(entry["detail"]["irUrlSource"], "ir_sites")

    def test_dropped_event_is_detected_from_previous_snapshot(self) -> None:
        previous_pending = {
            "evt-1": {
                "code": "4746",
                "secCode": "4746",
                "edinetCode": "E05066",
                "eventType": "stock_split",
                "eventDate": "2026-08-01",
                "attempts": 5,
                "firstSeenAt": "2026-07-20",
            }
        }
        state = {"events": {"pending": {}, "seen": {}}}
        entries, tracked = build_ir_review_queue.build_pending_split_entries(
            state, self.resolver, self.ir_sites, {}, previous_pending, self.today
        )
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry["trigger"], "api_overflow")
        self.assertEqual(entry["detail"]["reason"], "dropped_from_queue")
        self.assertEqual(entry["code"], "4746")
        # 脱落した分は追跡対象から外れる(seenにもpendingにも無いのでtrackedへ再登録しない)
        self.assertNotIn("evt-1", tracked)

    def test_resolved_event_is_not_treated_as_dropped(self) -> None:
        previous_pending = {
            "evt-1": {
                "code": "4746",
                "secCode": "4746",
                "edinetCode": "E05066",
                "eventType": "stock_split",
                "eventDate": "2026-08-01",
                "attempts": 2,
                "firstSeenAt": "2026-07-20",
            }
        }
        state = {"events": {"pending": {}, "seen": {"evt-1": "2026-08-14"}}}
        entries, _tracked = build_ir_review_queue.build_pending_split_entries(
            state, self.resolver, self.ir_sites, {}, previous_pending, self.today
        )
        self.assertEqual(entries, [])

    def test_unresolvable_code_is_skipped_but_still_tracked(self) -> None:
        state = {
            "events": {
                "pending": {
                    "evt-1": {
                        "eventType": "stock_split",
                        "eventDate": "2026-08-10",
                        "secCode": "0001",
                        "edinetCode": "E99999",
                        "firstSeenAt": "2026-08-14",
                        "attempts": 1,
                    }
                },
                "seen": {},
            }
        }
        entries, tracked = build_ir_review_queue.build_pending_split_entries(
            state, self.resolver, self.ir_sites, {}, {}, self.today
        )
        self.assertEqual(entries, [])
        self.assertIn("evt-1", tracked)
        self.assertIsNone(tracked["evt-1"]["code"])

    def test_non_split_event_types_never_reach_the_queue(self) -> None:
        state = {
            "events": {
                "pending": {
                    "evt-1": {
                        "eventType": "dividend_revision",
                        "eventDate": "2026-08-10",
                        "secCode": "4746",
                        "edinetCode": "E05066",
                        "firstSeenAt": "2026-08-14",
                        "attempts": 5,
                    }
                },
                "seen": {},
            }
        }
        entries, tracked = build_ir_review_queue.build_pending_split_entries(
            state, self.resolver, self.ir_sites, {}, {}, self.today
        )
        self.assertEqual(entries, [])
        self.assertEqual(tracked, {})


class SuspectYearsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.edinet_dir = Path(self.directory.name)
        write_json(self.edinet_dir / "1301.json", {"name": "極洋"})
        self.resolver = build_ir_review_queue.CodeResolver(self.edinet_dir)
        self.today = date(2026, 8, 15)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_extract_suspect_years_only_when_unreliable(self) -> None:
        fiscal_dividends = {
            "1301": {
                "streakBasis": {
                    "reliable": False,
                    "breakYears": [2020, 2021],
                }
            },
            "1302": {
                "streakBasis": {"reliable": True, "breakYears": [2019]},
            },
            "1303": {},
        }
        result = build_ir_review_queue.extract_suspect_years(fiscal_dividends)
        self.assertEqual(result, {"1301": ["2020", "2021"]})

    def test_new_years_only_are_queued(self) -> None:
        current = {"1301": ["2020", "2021"]}
        previous = {"1301": ["2020"]}
        entries = build_ir_review_queue.build_suspect_year_entries(
            current, previous, self.resolver, self.today
        )
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["detail"], {"year": "2021"})
        self.assertEqual(entries[0]["trigger"], "new_suspect_year")
        self.assertEqual(entries[0]["name"], "極洋")

    def test_unchanged_years_are_not_requeued(self) -> None:
        current = {"1301": ["2020"]}
        previous = {"1301": ["2020"]}
        entries = build_ir_review_queue.build_suspect_year_entries(
            current, previous, self.resolver, self.today
        )
        self.assertEqual(entries, [])

    def test_first_time_seen_code_queues_all_its_years(self) -> None:
        current = {"1301": ["2020", "2021"]}
        entries = build_ir_review_queue.build_suspect_year_entries(
            current, {}, self.resolver, self.today
        )
        self.assertEqual({entry["detail"]["year"] for entry in entries}, {"2020", "2021"})


class RunIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.edinet_dir = self.root / "edinet"
        write_json(
            self.edinet_dir / "4746.json",
            {"code": "4746", "name": "東計電算", "edinetCode": "E05066"},
        )
        write_json(self.edinet_dir / "1301.json", {"name": "極洋"})

    def tearDown(self) -> None:
        self.directory.cleanup()

    def _args(self, **overrides):
        defaults = dict(
            state=self.root / "forecasts_state.json",
            fiscal_dividends=self.root / "fiscal_dividends.json",
            edinet_dir=self.edinet_dir,
            ir_sites=self.root / "ir_sites.json",
            ir_sites_candidates=self.root / "ir_sites_candidates.json",
            queue=self.root / "data" / "pending_ir_review.json",
            snapshot=self.root / "data" / "ir_review_snapshot.json",
            today=date(2026, 8, 15),
        )
        defaults.update(overrides)
        return build_ir_review_queue.parse_args(
            [
                "--state", str(defaults["state"]),
                "--fiscal-dividends", str(defaults["fiscal_dividends"]),
                "--edinet-dir", str(defaults["edinet_dir"]),
                "--ir-sites", str(defaults["ir_sites"]),
                "--ir-sites-candidates", str(defaults["ir_sites_candidates"]),
                "--queue", str(defaults["queue"]),
                "--snapshot", str(defaults["snapshot"]),
                "--today", defaults["today"].isoformat(),
            ]
        )

    def test_missing_inputs_do_not_crash_and_exit_zero(self) -> None:
        args = self._args()
        exit_code = build_ir_review_queue.run(args)
        self.assertEqual(exit_code, 0)
        queue = json.loads(args.queue.read_text(encoding="utf-8"))
        self.assertEqual(queue, {"queue": []})

    def test_full_run_populates_queue_and_snapshot(self) -> None:
        write_json(
            self.root / "forecasts_state.json",
            {
                "events": {
                    "pending": {
                        "evt-split": {
                            "eventType": "stock_split",
                            "eventDate": "2026-08-10",
                            "secCode": "4746",
                            "edinetCode": "E05066",
                            "firstSeenAt": "2026-08-14",
                            "attempts": 1,
                        }
                    },
                    "seen": {},
                }
            },
        )
        write_json(
            self.root / "fiscal_dividends.json",
            {
                "1301": {
                    "streakBasis": {"reliable": False, "breakYears": [2025]}
                }
            },
        )
        write_json(
            self.root / "ir_sites.json",
            {"4746": {"irTopUrl": "https://toukei.example/ir/"}},
        )

        args = self._args()
        exit_code = build_ir_review_queue.run(args)
        self.assertEqual(exit_code, 0)

        queue = json.loads(args.queue.read_text(encoding="utf-8"))
        triggers = sorted(item["trigger"] for item in queue["queue"])
        self.assertEqual(triggers, ["edinetdb_event", "new_suspect_year"])

        snapshot = json.loads(args.snapshot.read_text(encoding="utf-8"))
        self.assertEqual(snapshot["suspectYears"], {"1301": ["2025"]})
        self.assertIn("evt-split", snapshot["pendingSplitEvents"])

    def test_second_run_with_unchanged_state_adds_nothing(self) -> None:
        state = {
            "events": {
                "pending": {
                    "evt-split": {
                        "eventType": "stock_split",
                        "eventDate": "2026-08-10",
                        "secCode": "4746",
                        "edinetCode": "E05066",
                        "firstSeenAt": "2026-08-14",
                        "attempts": 1,
                    }
                },
                "seen": {},
            }
        }
        write_json(self.root / "forecasts_state.json", state)
        write_json(
            self.root / "fiscal_dividends.json",
            {"1301": {"streakBasis": {"reliable": False, "breakYears": [2025]}}},
        )

        args = self._args()
        build_ir_review_queue.run(args)
        first_queue = json.loads(args.queue.read_text(encoding="utf-8"))
        self.assertEqual(len(first_queue["queue"]), 2)

        # 2回目: 全く同じ入力で再実行しても、追跡状態がsnapshotへ移った
        # ことで重複が積まれない。
        args2 = self._args(today=date(2026, 8, 16))
        build_ir_review_queue.run(args2)
        second_queue = json.loads(args.queue.read_text(encoding="utf-8"))
        self.assertEqual(len(second_queue["queue"]), 2)

    def test_dropped_event_flows_through_two_runs(self) -> None:
        write_json(
            self.root / "forecasts_state.json",
            {
                "events": {
                    "pending": {
                        "evt-split": {
                            "eventType": "stock_split",
                            "eventDate": "2026-08-01",
                            "secCode": "4746",
                            "edinetCode": "E05066",
                            "firstSeenAt": "2026-07-20",
                            "attempts": 4,
                        }
                    },
                    "seen": {},
                }
            },
        )
        args1 = self._args(today=date(2026, 8, 3))
        build_ir_review_queue.run(args1)
        first_queue = json.loads(args1.queue.read_text(encoding="utf-8"))
        self.assertEqual(len(first_queue["queue"]), 1)
        self.assertEqual(first_queue["queue"][0]["trigger"], "api_overflow")
        self.assertEqual(
            first_queue["queue"][0]["detail"]["reason"], "attempts_near_limit"
        )

        # 翌日以降、fetch_forecasts.py側が上限超過でpendingから消した想定
        # (stateには残っていない・seenにも無い)。
        write_json(
            self.root / "forecasts_state.json",
            {"events": {"pending": {}, "seen": {}}},
        )
        args2 = self._args(today=date(2026, 8, 4))
        build_ir_review_queue.run(args2)
        second_queue = json.loads(args1.queue.read_text(encoding="utf-8"))
        # 重複排除は (code, trigger, eventId) 単位。near_limit時点で既に
        # api_overflow/evt-splitとして積んであるので、後日dropされても
        # 同じイベントの2件目は積まれない(既にリストに載っている以上、
        # 「消えた」という追記情報だけのために別枠を増やす必要はない)。
        self.assertEqual(len(second_queue["queue"]), 1)
        self.assertEqual(
            second_queue["queue"][0]["detail"]["reason"], "attempts_near_limit"
        )


if __name__ == "__main__":
    unittest.main()
