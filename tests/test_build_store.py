import importlib.util
import io
import json
import re
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from contextlib import redirect_stdout
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "build_store", ROOT / "scripts" / "build_store.py"
)
assert SPEC and SPEC.loader
build_store = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(build_store)
FILTER_SPEC = importlib.util.spec_from_file_location(
    "filter_extracted_stock_actions",
    ROOT / "scripts" / "filter_extracted_stock_actions.py",
)
assert FILTER_SPEC and FILTER_SPEC.loader
filter_extracted_stock_actions = importlib.util.module_from_spec(FILTER_SPEC)
FILTER_SPEC.loader.exec_module(filter_extracted_stock_actions)


def event(
    code: str,
    event_id: str,
    old_shares: int,
    new_shares: int,
    *,
    eps_adjusted_by_issuer: bool | None,
    effective_date: str = "2026-07-01",
    status: str = "confirmed",
) -> dict:
    return {
        "eventId": event_id,
        "securityCode": code,
        "action": "split",
        "oldShares": old_shares,
        "newShares": new_shares,
        "effectiveDate": effective_date,
        "status": status,
        "epsAdjustedByIssuer": eps_adjusted_by_issuer,
        "source": {
            "url": f"https://example.com/{event_id}.pdf",
            "type": "issuer_ir",
        },
    }


class SplitAdjustmentTest(unittest.TestCase):
    def build(self, path: Path, actions: dict[str, list[dict]]) -> None:
        financials = [
            {
                "code": "7236",
                "name": "ティラド",
                "eps": {"2026": 1500.0},
                "bps": {"2026": 9000.0},
                "per": {"2026": 5.2},
                "dividendPerShare": {"2026": 560.0},
            },
            {
                "code": "2220",
                "name": "亀田製菓",
                "eps": {"2026": 390.0},
                "bps": {"2026": 1620.0},
                "per": {"2026": 3.7},
                "dividendPerShare": {"2026": 66.0},
            },
            {
                "code": "9999",
                "name": "イベントなし",
                "eps": {"2026": 100.0},
                "bps": {"2026": 500.0},
                "per": {"2026": 10.0},
                "dividendPerShare": {"2026": 20.0},
            },
        ]
        prices = {"7236": 1483.0, "2220": 1272.0, "9999": 1000.0}
        daily_dividends = {
            "7236": 560.0,
            "2220": 66.0,
            "9999": 20.0,
        }
        with mock.patch.object(
            build_store,
            "load_daily_prices",
            return_value=(prices, daily_dividends, "2026-08-03"),
        ):
            build_store.create_database(
                path,
                financials,
                {},
                {},
                {},
                [
                    Path("financials"),
                    Path("sectors"),
                    Path("tickers"),
                    Path("forecasts"),
                ],
                "fixture.csv",
                actions,
                Path("stock_actions.json"),
            )

    @staticmethod
    def rows(path: Path) -> dict[str, tuple]:
        with sqlite3.connect(path) as connection:
            return {
                row[0]: (row[1], row[2], json.loads(row[3]))
                for row in connection.execute(
                    "SELECT code, yield, price, payload FROM stocks ORDER BY code"
                )
            }

    def test_eps_adjusted_branch_and_eventless_stock(self) -> None:
        actions = {
            "7236": [
                event("7236", "ten-for-one", 1, 10, eps_adjusted_by_issuer=False)
            ],
            "2220": [
                event("2220", "three-for-one", 1, 3, eps_adjusted_by_issuer=True)
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            baseline = Path(directory) / "baseline.sqlite"
            adjusted = Path(directory) / "adjusted.sqlite"
            self.build(baseline, {})
            self.build(adjusted, actions)
            before = self.rows(baseline)
            after = self.rows(adjusted)

        tirad = after["7236"][2]
        self.assertEqual(after["7236"][0], 3.78)
        self.assertEqual(tirad["eps"]["2026"], 150.0)
        self.assertEqual(tirad["bps"]["2026"], 900.0)
        self.assertEqual(tirad["dividendPerShare"]["2026"], 56.0)
        self.assertEqual(tirad["per"]["2026"], 5.2)
        self.assertEqual(
            tirad["splitAdjustment"]["events"][0]["sourceUrl"],
            "https://example.com/ten-for-one.pdf",
        )
        self.assertEqual(
            tirad["splitAdjustment"]["events"][0]["status"], "confirmed"
        )
        self.assertTrue(
            tirad["splitAdjustment"]["events"][0]["applyDividendAdjustment"]
        )
        self.assertFalse(
            tirad["splitAdjustment"]["events"][0]["epsAdjustedByIssuer"]
        )
        self.assertFalse(tirad["splitAdjustment"]["hasProvisional"])

        kameda = after["2220"][2]
        self.assertEqual(after["2220"][0], 1.73)
        self.assertEqual(kameda["eps"], before["2220"][2]["eps"])
        self.assertEqual(kameda["bps"], before["2220"][2]["bps"])
        self.assertEqual(kameda["per"], before["2220"][2]["per"])
        self.assertEqual(kameda["dividendPerShare"]["2026"], 22.0)

        self.assertEqual(after["9999"], before["9999"])
        self.assertLess(
            tirad["dividendYield"], before["7236"][2]["dividendYield"]
        )
        self.assertLess(
            kameda["dividendYield"], before["2220"][2]["dividendYield"]
        )

    def test_provisional_adjusts_dividend_but_not_eps_or_bps(self) -> None:
        actions = {
            "7236": [
                event(
                    "7236",
                    "provisional-ten-for-one",
                    1,
                    10,
                    eps_adjusted_by_issuer=None,
                    status="provisional",
                )
            ]
        }
        with tempfile.TemporaryDirectory() as directory:
            baseline = Path(directory) / "baseline.sqlite"
            provisional = Path(directory) / "provisional.sqlite"
            self.build(baseline, {})
            self.build(provisional, actions)
            before = self.rows(baseline)
            after = self.rows(provisional)

        tirad = after["7236"][2]
        self.assertEqual(after["7236"][0], 3.78)
        self.assertEqual(tirad["dividendPerShare"]["2026"], 56.0)
        self.assertEqual(tirad["eps"], before["7236"][2]["eps"])
        self.assertEqual(tirad["bps"], before["7236"][2]["bps"])
        self.assertEqual(tirad["per"], before["7236"][2]["per"])
        self.assertEqual(tirad["splitAdjustment"]["epsBpsFactor"], 1.0)
        self.assertTrue(tirad["splitAdjustment"]["hasProvisional"])
        self.assertEqual(
            tirad["splitAdjustment"]["events"][0]["status"], "provisional"
        )
        self.assertTrue(
            tirad["splitAdjustment"]["events"][0]["applyDividendAdjustment"]
        )
        self.assertIsNone(
            tirad["splitAdjustment"]["events"][0]["epsAdjustedByIssuer"]
        )

    def test_dps_adjusted_has_three_states(self) -> None:
        omitted = event(
            "1234", "dps-omitted", 1, 2, eps_adjusted_by_issuer=True
        )
        explicit_false = event(
            "1234", "dps-false", 1, 2, eps_adjusted_by_issuer=True
        )
        explicit_false["applyDividendAdjustment"] = False
        explicit_null = event(
            "1234", "dps-null", 1, 2, eps_adjusted_by_issuer=True
        )
        explicit_null["applyDividendAdjustment"] = None
        fallbacks: list[dict] = []
        adjustment = build_store.split_adjustment(
            [omitted, explicit_false, explicit_null],
            fallback_events=fallbacks,
        )
        assert adjustment is not None
        self.assertEqual(adjustment["dividendFactor"], 0.5)
        self.assertEqual(adjustment["epsBpsFactor"], 1.0)
        self.assertEqual(
            [item["applyDividendAdjustment"] for item in adjustment["events"]],
            [True, False, None],
        )
        self.assertEqual(len(fallbacks), 1)
        self.assertEqual(fallbacks[0]["eventId"], "dps-null")

    def test_confirmed_with_unconfirmed_eps_adjustment_falls_back_safely(self) -> None:
        actions = {
            "7236": [
                event(
                    "7236",
                    "confirmed-without-eps-check",
                    1,
                    10,
                    eps_adjusted_by_issuer=None,
                )
            ]
        }
        output = io.StringIO()
        with redirect_stdout(output):
            adjustment = build_store.split_adjustment(actions["7236"])
        assert adjustment is not None
        self.assertEqual(adjustment["dividendFactor"], 0.1)
        self.assertEqual(adjustment["epsBpsFactor"], 1.0)
        self.assertIn(
            "confirmedなのにepsAdjustedByIssuerがnull", output.getvalue()
        )

        with tempfile.TemporaryDirectory() as directory:
            baseline = Path(directory) / "baseline.sqlite"
            fallback = Path(directory) / "fallback.sqlite"
            self.build(baseline, {})
            self.build(fallback, actions)
            before = self.rows(baseline)
            after = self.rows(fallback)
            path = Path(directory) / "actions.json"
            path.write_text(
                json.dumps({"events": actions["7236"]}), encoding="utf-8"
            )
            fallbacks: list[dict] = []
            loaded = build_store.load_stock_actions(
                path, as_of=date(2026, 8, 3), fallback_events=fallbacks
            )
            with sqlite3.connect(fallback) as connection:
                metadata = dict(connection.execute("SELECT key, value FROM meta"))

        self.assertEqual(len(fallbacks), 1)
        self.assertIsNone(loaded["7236"][0]["epsAdjustedByIssuer"])
        self.assertEqual(after["7236"][0], 3.78)
        self.assertEqual(after["7236"][2]["eps"], before["7236"][2]["eps"])
        self.assertEqual(after["7236"][2]["bps"], before["7236"][2]["bps"])
        self.assertEqual(
            after["7236"][2]["splitAdjustment"]["events"][0]["status"],
            "confirmed",
        )
        self.assertEqual(metadata["split_adjustment_fallback_count"], "1")
        self.assertIn(
            "confirmedなのにepsAdjustedByIssuerがnull",
            metadata["split_adjustment_fallbacks"],
        )

    def test_eps_adjusted_unexpected_values_fall_back_to_null(self) -> None:
        document = {
            "events": [
                event(
                    "7236",
                    "string-eps-flag",
                    1,
                    10,
                    eps_adjusted_by_issuer="false",
                    status="provisional",
                )
            ]
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "actions.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            fallbacks: list[dict] = []
            loaded = build_store.load_stock_actions(
                path, as_of=date(2026, 8, 3), fallback_events=fallbacks
            )
        self.assertIsNone(loaded["7236"][0]["epsAdjustedByIssuer"])
        self.assertEqual(fallbacks[0]["field"], "epsAdjustedByIssuer")

    def test_legacy_field_names_are_rejected_with_migration_guidance(self) -> None:
        legacy_eps_name = "".join(("eps", "Adjusted"))
        legacy_dps_name = "".join(("dps", "Adjusted"))
        legacy_fields = (
            (legacy_eps_name, "epsAdjustedByIssuer"),
            (legacy_dps_name, "applyDividendAdjustment"),
        )
        for index, (legacy_field, replacement) in enumerate(legacy_fields):
            with self.subTest(legacy_field=legacy_field):
                action = event(
                    "7236",
                    f"legacy-field-{index}",
                    1,
                    10,
                    eps_adjusted_by_issuer=False,
                )
                if legacy_field == legacy_eps_name:
                    action[legacy_field] = action.pop(replacement)
                else:
                    action[legacy_field] = True
                message = re.escape(f"新しいフィールド名 '{replacement}'")
                with self.assertRaisesRegex(ValueError, message):
                    build_store.split_adjustment([action])

                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "actions.json"
                    path.write_text(
                        json.dumps({"events": [action]}), encoding="utf-8"
                    )
                    with self.assertRaisesRegex(ValueError, message):
                        build_store.load_stock_actions(
                            path, as_of=date(2026, 8, 3)
                        )

    def test_unknown_status_is_ignored(self) -> None:
        actions = {
            "7236": [
                event(
                    "7236",
                    "unknown-status",
                    1,
                    10,
                    eps_adjusted_by_issuer=False,
                    status="unknown",
                )
            ]
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "actions.json"
            path.write_text(json.dumps({"events": actions["7236"]}), encoding="utf-8")
            loaded = build_store.load_stock_actions(path, as_of=date(2026, 8, 3))
            self.assertEqual(loaded, {})

            baseline = Path(directory) / "baseline.sqlite"
            ignored = Path(directory) / "ignored.sqlite"
            self.build(baseline, {})
            self.build(ignored, actions)
            self.assertEqual(self.rows(ignored), self.rows(baseline))

    def test_missing_status_is_ignored_by_split_adjustment(self) -> None:
        action = event(
            "7236",
            "missing-status",
            1,
            10,
            eps_adjusted_by_issuer=False,
        )
        del action["status"]
        self.assertIsNone(build_store.split_adjustment([action]))

    def test_multiple_events_multiply_factors(self) -> None:
        adjustment = build_store.split_adjustment(
            [
                event("1234", "one-to-two", 1, 2, eps_adjusted_by_issuer=False),
                event("1234", "one-to-five", 1, 5, eps_adjusted_by_issuer=True),
            ]
        )
        assert adjustment is not None
        self.assertAlmostEqual(adjustment["dividendFactor"], 0.1)
        self.assertAlmostEqual(adjustment["epsBpsFactor"], 0.5)
        self.assertEqual(len(adjustment["events"]), 2)

    def test_multiple_events_adjust_each_fiscal_period_only_after_its_split(self) -> None:
        adjustment = build_store.split_adjustment(
            [
                event(
                    "7466",
                    "spk-2020",
                    1,
                    2,
                    eps_adjusted_by_issuer=True,
                    effective_date="2020-04-01",
                ),
                event(
                    "7466",
                    "spk-2026",
                    1,
                    2,
                    eps_adjusted_by_issuer=True,
                    effective_date="2026-04-01",
                ),
            ]
        )
        assert adjustment is not None
        adjusted = build_store.adjust_per_share_series(
            {"2020": 72, "2021": 37, "2026": 73},
            adjustment,
            fiscal_month=3,
        )
        self.assertEqual(adjusted["2020"], 18.0)
        self.assertEqual(adjusted["2021"], 18.5)
        self.assertEqual(adjusted["2026"], 36.5)

    def test_loader_excludes_future_events(self) -> None:
        document = {
            "events": [
                event(
                    "1234",
                    "future",
                    1,
                    2,
                    eps_adjusted_by_issuer=False,
                    effective_date="2026-09-01",
                ),
                event(
                    "1234",
                    "effective",
                    1,
                    2,
                    eps_adjusted_by_issuer=False,
                    effective_date="2026-07-01",
                ),
            ]
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "actions.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            loaded = build_store.load_stock_actions(
                path, as_of=date(2026, 8, 3)
            )
        self.assertEqual(
            [item["eventId"] for item in loaded["1234"]], ["effective"]
        )

    def test_manual_actions_have_expected_stocks_and_kameda_eps_flag(self) -> None:
        loaded = build_store.load_stock_actions(
            ROOT / "data" / "stock_actions_manual.json",
            as_of=date(2026, 8, 3),
        )
        self.assertEqual(len(loaded), 30)
        self.assertEqual(sum(map(len, loaded.values())), 30)
        self.assertTrue(loaded["2220"][0]["epsAdjustedByIssuer"])
        self.assertTrue(
            all(
                item["epsAdjustedByIssuer"] in (True, False, None)
                for events in loaded.values()
                for item in events
            )
        )
        self.assertTrue(loaded["2897"][0]["epsAdjustedByIssuer"])
        self.assertTrue(loaded["6516"][0]["epsAdjustedByIssuer"])

    def test_manual_actions_include_toukei_as_provisional_after_effective_date(
        self,
    ) -> None:
        loaded = build_store.load_stock_actions(
            ROOT / "data" / "stock_actions_manual.json",
            as_of=date(2026, 10, 1),
        )
        self.assertIn("4746", loaded)
        toukei = loaded["4746"][0]
        self.assertEqual(toukei["oldShares"], 1)
        self.assertEqual(toukei["newShares"], 4)
        self.assertEqual(toukei["effectiveDate"], "2026-10-01")
        self.assertEqual(toukei["status"], "provisional")
        self.assertTrue(toukei["applyDividendAdjustment"])
        self.assertIsNone(toukei["epsAdjustedByIssuer"])


class StockActionIntegrationTest(unittest.TestCase):
    MANUAL = ROOT / "data" / "stock_actions_manual.json"
    EXTRACTED = ROOT / "data" / "stock_actions_extracted.json"
    FISCAL = Path("/Users/yusuke/workspace/edinet-direct/data/fiscal_dividends.json")

    def test_extracted_file_is_the_filtered_audited_split_set(self) -> None:
        document = json.loads(self.EXTRACTED.read_text(encoding="utf-8"))
        events = document["events"]
        # 件数は台帳とfiscal_dividendsの進化で変わるため直値にしない。
        # 「採用+除外=入力全体」という保存則と、下の性質検査で担保する。
        self.assertGreater(len(events), 0)
        self.assertEqual(
            len(events) + len(document.get("excluded", [])) - len(
                [i for i in document["excluded"] if "eventId" not in i]
            ),
            1135,
        )
        issuer_excluded = {
            (str(item["securityCode"]), item["effectiveDate"])
            for item in document["excluded"]
            if item.get("reasonCode") == "issuer_mismatch"
        }
        self.assertEqual(
            issuer_excluded,
            {("9432", "2022-10-01"), ("2345", "2022-03-02"), ("5711", "2026-10-01")},
        )
        present = {
            (str(event["securityCode"]), event["effectiveDate"]) for event in events
        }
        excluded = {
            (str(item["securityCode"]), item["effectiveDate"])
            for item in document["excluded"]
        }
        self.assertTrue(excluded.isdisjoint(present))
        self.assertTrue(all(event["action"] == "split" for event in events))
        self.assertTrue(
            all(event["status"] == "confirmed" for event in events)
        )
        self.assertFalse(
            any(event["action"] == "consolidation" for event in events)
        )
        self.assertTrue(
            all(
                event["source"]["audit"]["decision"] == "合格"
                for event in events
            )
        )
        self.assertEqual(
            {event["eventId"] for event in events},
            {
                event["eventId"]
                for event in document["events"]
                if event["source"]["audit"]["decision"] == "合格"
            },
        )
        self.assertTrue(
            all(item.get("reasonCode") for item in document["excluded"])
        )
        self.assertFalse(
            any(
                event["oldShares"] > 0
                and event["newShares"] / event["oldShares"] >= 50
                for event in events
            )
        )

    def test_extracted_file_matches_reproducible_selection_script(self) -> None:
        if not self.FISCAL.exists():
            self.skipTest("外部由来のfiscal_dividends.jsonがありません")
        document = json.loads(self.EXTRACTED.read_text(encoding="utf-8"))
        fiscal = filter_extracted_stock_actions.load_fiscal_series(self.FISCAL)
        tickers = filter_extracted_stock_actions.load_ticker_codes(
            ROOT / "data" / "tickers.json"
        )
        refiltered, counts = filter_extracted_stock_actions.filter_document(
            document,
            fiscal,
            tickers,
        )
        # 選別済みファイルを入力にした再実行でも、採用・除外集合は変わらない。
        self.assertEqual(
            {event["eventId"] for event in refiltered["events"]},
            {event["eventId"] for event in document["events"]},
        )
        self.assertEqual(
            {event.get("eventId") for event in refiltered["excluded"]},
            {event.get("eventId") for event in document["excluded"]},
        )
        self.assertEqual(counts["input"], 1135)
        # 採用件数はfiscal_dividendsの進化で変わるため、ファイルとの一致だけを見る
        self.assertEqual(counts["selected"], len(document["events"]))
        # 新規除外の件数も選定結果に連動する（除外合計との整合だけを見る）
        self.assertEqual(
            counts["newlyExcluded"],
            len(document["excluded"])
            - len([i for i in document["excluded"] if "eventId" not in i]),
        )

    def test_spk_2026_dividend_is_adjusted_from_73_to_36_5(self) -> None:
        if not self.FISCAL.exists():
            self.skipTest("外部由来のfiscal_dividends.jsonがありません")
        fiscal = build_store.load_fiscal_dividends(self.FISCAL)["7466"]
        loaded = build_store.load_stock_actions(
            self.EXTRACTED, as_of=date(2026, 8, 10)
        )
        adjustment = build_store.split_adjustment(loaded["7466"])
        assert adjustment is not None
        series_adjustment = build_store.adjustment_for_unadjusted_series(
            adjustment,
            fiscal["series"],
            fiscal_month=fiscal["fiscalMonth"],
        )
        adjusted = build_store.adjust_per_share_series(
            fiscal["series"],
            series_adjustment,
            fiscal_month=fiscal["fiscalMonth"],
        )
        self.assertEqual(fiscal["series"][2026], 73.0)
        self.assertEqual(adjusted[2026], 36.5)

    def test_october_first_events_are_pending_until_effective_date(self) -> None:
        before = build_store.load_stock_actions(
            self.EXTRACTED, as_of=date(2026, 9, 30)
        )
        after = build_store.load_stock_actions(
            self.EXTRACTED, as_of=date(2026, 10, 1)
        )
        for code in ("1925", "8035", "8316"):
            self.assertNotIn(code, before)
            self.assertIn(code, after)
            self.assertEqual(after[code][0]["effectiveDate"], "2026-10-01")

    def test_manual_event_wins_when_event_id_is_duplicated(self) -> None:
        loaded = build_store.load_stock_actions(
            [self.MANUAL, self.EXTRACTED], as_of=date(2026, 8, 3)
        )
        event = loaded["8053"][0]
        self.assertEqual(
            event["source"]["url"],
            "https://www.sumitomocorp.com/-/media/Files/hq/ir/report/summary/2025/2603Tanshin.pdf?sc_lang=ja",
        )
        self.assertNotEqual(event["source"].get("type"), "edinet")

    def test_loader_maps_eps_flag_and_keeps_audit_provenance(self) -> None:
        loaded = build_store.load_stock_actions(
            [self.MANUAL, self.EXTRACTED], as_of=date(2026, 8, 9)
        )
        event = loaded["8309"][0]
        self.assertFalse(event["epsAdjustedByIssuer"])
        self.assertEqual(event["source"]["type"], "edinet")
        self.assertEqual(event["source"]["audit"]["decision"], "合格")
        self.assertTrue(event["source"]["docID"].startswith("S"))


class SplitFallbackNotificationTest(unittest.TestCase):
    SCRIPT = ROOT / "scripts" / "check_split_fallback.py"

    def run_check(
        self, count: int, fallbacks: list[dict[str, str]]
    ) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "store.sqlite"
            with sqlite3.connect(path) as connection:
                connection.execute(
                    "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
                )
                connection.executemany(
                    "INSERT INTO meta(key, value) VALUES (?, ?)",
                    [
                        ("split_adjustment_fallback_count", str(count)),
                        (
                            "split_adjustment_fallbacks",
                            json.dumps(fallbacks, ensure_ascii=False),
                        ),
                    ],
                )
                connection.commit()
            return subprocess.run(
                [sys.executable, str(self.SCRIPT), str(path)],
                capture_output=True,
                text=True,
                check=False,
            )

    def test_no_fallback_keeps_the_job_successful(self) -> None:
        result = self.run_check(0, [])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("株式分割補正フォールバック: 0件", result.stdout)

    def test_fallback_fails_after_delivery_with_actionable_error(self) -> None:
        result = self.run_check(
            1,
            [
                {
                    "eventId": "confirmed-without-eps-check",
                    "securityCode": "7236",
                    "field": "epsAdjustedByIssuer",
                    "reason": "confirmedなのにepsAdjustedByIssuerがnull",
                }
            ],
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("::error::株式分割補正にフォールバックが発生しました", result.stdout)


if __name__ == "__main__":
    unittest.main()


class FiscalDividendStatsTest(unittest.TestCase):
    def test_counts_consecutive_increases_and_detects_ceiling(self) -> None:
        stats = build_store.fiscal_dividend_stats(
            {2022: 10.0, 2023: 11.0, 2024: 12.0, 2025: 13.0}
        )
        # 系列の一番古い年まで増配が続く＝それより前は確認できない
        self.assertEqual(stats["streakIncrease"], 3)
        self.assertTrue(stats["streakIncreaseCapped"])

    def test_streak_that_stops_inside_the_series_is_not_capped(self) -> None:
        stats = build_store.fiscal_dividend_stats(
            {2021: 10.0, 2022: 9.0, 2023: 11.0, 2024: 12.0}
        )
        self.assertEqual(stats["streakIncrease"], 2)
        self.assertFalse(stats["streakIncreaseCapped"])

    def test_flat_year_stops_increase_but_not_non_decrease(self) -> None:
        stats = build_store.fiscal_dividend_stats(
            {2022: 10.0, 2023: 10.0, 2024: 11.0}
        )
        self.assertEqual(stats["streakIncrease"], 1)
        self.assertEqual(stats["streakNonDecrease"], 2)
        self.assertTrue(stats["streakNonDecreaseCapped"])

    def test_unpaid_year_stops_the_streak(self) -> None:
        # 無配(0円)から復配した年は「連続増配」に数えない
        stats = build_store.fiscal_dividend_stats(
            {2021: 8.0, 2022: 0.0, 2023: 5.0, 2024: 6.0, 2025: 7.0}
        )
        self.assertEqual(stats["streakIncrease"], 2)
        self.assertFalse(stats["streakIncreaseCapped"])

    def test_missing_year_stops_the_streak(self) -> None:
        # 欠測年をまたいで比較すると連続していたか確認できない
        stats = build_store.fiscal_dividend_stats(
            {2020: 5.0, 2021: 6.0, 2023: 7.0, 2024: 8.0}
        )
        self.assertEqual(stats["streakIncrease"], 1)

    def test_cagr_uses_year_distance_not_entry_count(self) -> None:
        stats = build_store.fiscal_dividend_stats(
            {2020: 100.0, 2023: 133.1, 2024: 150.0}
        )
        # 2021年が無いので「3年前」は2021年ではなく存在しない -> None
        self.assertIsNone(stats["cagr3"])
        self.assertIsNone(stats["cagr5"])

    def test_cagr_is_computed_from_the_year_n_years_back(self) -> None:
        series = {year: 100.0 * (1.1 ** (year - 2020)) for year in range(2020, 2026)}
        stats = build_store.fiscal_dividend_stats(series)
        self.assertAlmostEqual(stats["cagr3"], 10.0, places=1)
        self.assertAlmostEqual(stats["cagr5"], 10.0, places=1)

    def test_empty_series_is_all_zero(self) -> None:
        stats = build_store.fiscal_dividend_stats({})
        self.assertEqual(stats["streakIncrease"], 0)
        self.assertFalse(stats["streakIncreaseCapped"])
        self.assertIsNone(stats["cagr3"])


class FiscalDividendLoaderTest(unittest.TestCase):
    def load(self, document: dict) -> dict:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fiscal_dividends.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            return build_store.load_fiscal_dividends(path)

    def test_skips_stocks_without_a_series(self) -> None:
        loaded = self.load(
            {
                "1301": {"fiscalMonth": 3, "series": {}, "connection": {}},
                "9433": {
                    "fiscalMonth": 3,
                    "series": {"2025": 72.5, "2026": 80.0},
                    "connection": {"status": "scaled", "reason": "テスト"},
                    "externalSource": "haitoukin-checker",
                    "externalYears": [2025],
                },
            }
        )
        self.assertEqual(set(loaded), {"9433"})
        self.assertEqual(loaded["9433"]["series"], {2025: 72.5, 2026: 80.0})
        self.assertEqual(loaded["9433"]["connectionStatus"], "scaled")
        self.assertEqual(loaded["9433"]["externalYears"], [2025])

    def test_drops_external_years_that_are_not_in_the_series(self) -> None:
        loaded = self.load(
            {
                "1301": {
                    "fiscalMonth": 3,
                    "series": {"2025": 130.0},
                    "connection": {"status": "connected"},
                    "externalYears": [1990, 2025],
                }
            }
        )
        self.assertEqual(loaded["1301"]["externalYears"], [2025])

    def test_real_file_covers_most_of_the_universe(self) -> None:
        path = ROOT / "data" / "fiscal_dividends.json"
        if not path.exists():
            self.skipTest(
                "data/fiscal_dividends.json は外部由来のためリポジトリに含めない。"
                "ConoHaから取得するか edinet-direct からコピーして実行する。"
            )
        loaded = build_store.load_fiscal_dividends(path)
        self.assertGreater(len(loaded), 3000)
        kddi = loaded["9433"]
        self.assertEqual(kddi["fiscalMonth"], 3)
        self.assertEqual(
            build_store.fiscal_dividend_stats(kddi["series"])["streakIncrease"],
            24,
        )


class FiscalSeriesInStoreTest(unittest.TestCase):
    def build(
        self,
        path: Path,
        fiscal: dict,
        actions: dict,
        *,
        forecasts: dict | None = None,
        frozen: dict | None = None,
        financials: list | None = None,
    ) -> None:
        financials = financials if financials is not None else [
            {"code": "9433", "name": "ＫＤＤＩ", "dividendPerShare": {"2026": 80.0}},
            {"code": "9999", "name": "系列なし", "dividendPerShare": {"2026": 20.0}},
        ]
        tickers = {
            "9433": {
                "code": "9433",
                "name": "ＫＤＤＩ",
                "market": "プライム（内国株式）",
                "sector": "情報・通信業",
            },
            "9999": {"code": "9999", "name": "系列なし"},
        }
        prices = {"9433": 2877.0, "9999": 1000.0}
        with mock.patch.object(
            build_store,
            "load_daily_prices",
            return_value=(prices, {"9433": 80.0, "9999": 20.0}, "2026-08-04"),
        ):
            build_store.create_database(
                path,
                financials,
                {},
                tickers,
                forecasts or {},
                [Path("f"), Path("s"), Path("t"), Path("fc")],
                "fixture.csv",
                actions,
                Path("stock_actions.json"),
                fiscal,
                Path("fiscal_dividends.json"),
                frozen or {},
                Path("calendar_dividends_frozen.json"),
                date(2026, 8, 5),
            )

    def test_fiscal_series_replaces_calendar_series(self) -> None:
        fiscal = {
            "9433": {
                "series": {2023: 67.5, 2024: 70.0, 2025: 72.5, 2026: 80.0},
                "fiscalMonth": 3,
                "connectionStatus": "scaled",
                "connectionReason": "テスト",
                "externalSource": "haitoukin-checker",
                "externalYears": [2023],
            }
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "store.sqlite"
            self.build(path, fiscal, {})
            with sqlite3.connect(path) as connection:
                rows = {
                    row[0]: row
                    for row in connection.execute(
                        "SELECT code, streak, streak_nd, streak_capped,"
                        " streak_nd_capped, cagr3, payload FROM stocks"
                    )
                }

        kddi = json.loads(rows["9433"][6])
        self.assertEqual(
            kddi["annual"],
            {"2023": 67.5, "2024": 70.0, "2025": 72.5, "2026": 80.0},
        )
        self.assertEqual(kddi["streakIncrease"], 3)
        self.assertTrue(kddi["streakIncreaseCapped"])
        self.assertEqual(rows["9433"][1], 3)
        self.assertEqual(rows["9433"][3], 1)
        self.assertEqual(rows["9433"][5], kddi["cagr3"])
        # Yahooの「集計中」は無くなった。会社発表が無ければバーは出ない。
        self.assertEqual(kddi["annualPartial"], {})
        self.assertEqual(kddi["annualPending"], {})
        self.assertNotIn("annualPartialCalendar", kddi)
        self.assertEqual(kddi["dividendSeries"]["basis"], "fiscal")
        self.assertEqual(kddi["dividendSeries"]["externalYears"], [2023])
        # 銘柄マスタ（JPX由来）から市場・業種が入る
        self.assertEqual(kddi["market"], "プライム（内国株式）")
        self.assertEqual(kddi["sector"], "情報・通信業")

        # 系列が無く凍結スナップショットにも無い銘柄は、配当の年数がNULLになる
        other = json.loads(rows["9999"][6])
        self.assertNotIn("annual", other)
        self.assertIsNone(other["streakIncrease"])
        self.assertEqual(other["annualPartial"], {})
        self.assertEqual(other["dividendSeries"]["basis"], "calendar")
        self.assertFalse(other["dividendSeries"]["frozen"])
        self.assertEqual(rows["9999"][3], 0)

    def test_frozen_calendar_snapshot_fills_the_stocks_without_a_series(
        self,
    ) -> None:
        frozen = {
            "9999": {
                "name": "系列なし",
                "series": {2018: 5.0, 2019: 6.0, 2020: 7.0},
            }
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "store.sqlite"
            self.build(path, {}, {}, frozen=frozen)
            with sqlite3.connect(path) as connection:
                rows = {
                    row[0]: row
                    for row in connection.execute(
                        "SELECT code, streak, payload FROM stocks"
                    )
                }
        payload = json.loads(rows["9999"][2])
        self.assertEqual(payload["annual"], {"2018": 5.0, "2019": 6.0, "2020": 7.0})
        # 年数は凍結系列から数え直す（Yahooが付けていた値は使わない）
        self.assertEqual(payload["streakIncrease"], 2)
        self.assertEqual(rows["9999"][1], 2)
        self.assertTrue(payload["dividendSeries"]["frozen"])
        self.assertEqual(payload["dividendSeries"]["basis"], "calendar")

    def test_pending_bar_comes_from_the_company_announcement(self) -> None:
        fiscal = {
            "9433": {
                "series": {2024: 70.0, 2025: 72.5},
                "fiscalMonth": 3,
                "connectionStatus": "connected",
                "connectionReason": "",
                "externalSource": None,
                "externalYears": [],
            }
        }
        forecasts = {
            "9433": {
                "forecastDividend": 84.0,
                "forecastPeriod": "2027年3月期(予)",
                "forecastFiscalYear": 2027,
                "forecastQuarter": 2,
                "forecastQuarterLabel": "Q2",
                "forecastPeriodType": "current",
                "forecastRevenue": 5_400_000,
                "forecastRevenueChange": 4.5,
                "forecastOperatingIncome": 560_000,
                "forecastOperatingIncomeChange": 7.2,
                "forecastOrdinaryIncome": 550_000,
                "forecastOrdinaryIncomeChange": 6.8,
                "forecastNetIncome": 380_000,
                "forecastNetIncomeChange": 8.1,
                "forecastEps": 123.4,
                "forecastEpsChange": 9.0,
                "confirmedDividend": 80.0,
                "confirmedFiscalYearEnd": "2026-03-31",
                "lastFetchedAt": "2026-08-05",
            }
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "store.sqlite"
            self.build(path, fiscal, {}, forecasts=forecasts)
            with sqlite3.connect(path) as connection:
                payload = json.loads(
                    connection.execute(
                        "SELECT payload FROM stocks WHERE code='9433'"
                    ).fetchone()[0]
                )
        self.assertEqual(
            payload["annualPartial"], {"2026": 80.0, "2027": 84.0}
        )
        self.assertEqual(payload["annualPending"]["2026"]["kind"], "confirmed")
        self.assertEqual(payload["annualPending"]["2026"]["label"], "確定")
        self.assertEqual(payload["annualPending"]["2027"]["kind"], "forecast")
        self.assertEqual(payload["annualPending"]["2027"]["label"], "予想")
        self.assertEqual(payload["forecastRevenue"], 5_400_000)
        self.assertEqual(payload["forecastRevenueChange"], 4.5)
        self.assertEqual(payload["forecastOperatingIncome"], 560_000)
        self.assertEqual(payload["forecastOperatingIncomeChange"], 7.2)
        self.assertEqual(payload["forecastEps"], 123.4)
        self.assertEqual(payload["forecastEpsChange"], 9.0)
        self.assertEqual(payload["forecastQuarter"], 2)
        self.assertEqual(payload["forecastPeriodType"], "current")
        self.assertEqual(payload["earnings"]["forecast"]["kind"], "forecast")
        self.assertEqual(
            payload["earnings"]["forecast"]["period"], "2027年3月期(予)"
        )
        self.assertEqual(payload["earnings"]["forecast"]["sourceQuarter"], 2)
        self.assertIsNone(payload["earnings"]["actual"])

    def test_earnings_payload_exposes_actual_when_the_annual_report_arrives(
        self,
    ) -> None:
        fiscal = {
            "9433": {
                "series": {2024: 70.0, 2025: 72.5},
                "fiscalMonth": 3,
                "connectionStatus": "connected",
                "connectionReason": "",
                "externalSource": None,
                "externalYears": [],
            }
        }
        forecasts = {
            "9433": {
                "forecastPeriod": "2027年3月期(予)",
                "forecastFiscalYear": 2027,
                "forecastQuarter": 2,
                "forecastPeriodType": "current",
                "forecastRevenue": 5_400_000,
                "forecastRevenueChange": 4.5,
                "forecastNetIncome": 380_000,
                "forecastNetIncomeChange": 8.1,
            }
        }
        financials = [
            {
                "code": "9433",
                "name": "ＫＤＤＩ",
                "revenue": {"2027": 5_500_000},
                "operatingIncome": {"2027": 570_000},
                "ordinaryIncome": {"2027": 565_000},
                "netIncome": {"2027": 390_000},
                "eps": {"2027": 126.0},
            },
            {"code": "9999", "name": "系列なし"},
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "store.sqlite"
            self.build(path, fiscal, {}, forecasts=forecasts, financials=financials)
            with sqlite3.connect(path) as connection:
                payload = json.loads(
                    connection.execute(
                        "SELECT payload FROM stocks WHERE code='9433'"
                    ).fetchone()[0]
                )

        actual = payload["earnings"]["actual"]
        self.assertEqual(actual["kind"], "actual")
        self.assertEqual(actual["period"], "2027年3月期")
        self.assertEqual(actual["fiscalYear"], 2027)
        self.assertEqual(actual["metrics"]["revenue"]["value"], 5_500_000)
        self.assertEqual(actual["metrics"]["eps"]["value"], 126.0)

    def test_payout_ratio_line_comes_from_edinet(self) -> None:
        financials = [
            {
                "code": "9433",
                "name": "ＫＤＤＩ",
                "payoutRatioTotalBased": {"2024": 49.57, "2025": 43.77},
            },
            {"code": "9999", "name": "系列なし"},
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "store.sqlite"
            self.build(path, {}, {}, financials=financials)
            with sqlite3.connect(path) as connection:
                rows = {
                    row[0]: (row[1], json.loads(row[2]))
                    for row in connection.execute(
                        "SELECT code, payout, payload FROM stocks"
                    )
                }
        self.assertEqual(
            rows["9433"][1]["payoutRatio"],
            {"2024": 49.57, "2025": 43.77},
        )
        self.assertEqual(rows["9433"][0], 43.77)
        # EDINET側に無ければ折れ線も出さない（Yahooの暦年値には戻さない）
        self.assertEqual(rows["9999"][1]["payoutRatio"], {})
        self.assertIsNone(rows["9999"][0])

    def test_split_factor_is_applied_to_the_fiscal_series(self) -> None:
        fiscal = {
            "9433": {
                "series": {2025: 72.5, 2026: 80.0},
                "fiscalMonth": 3,
                "connectionStatus": "edinet_only",
                "connectionReason": "",
                "externalSource": None,
                "externalYears": [],
            }
        }
        actions = {
            "9433": [
                event("9433", "one-to-two", 1, 2, eps_adjusted_by_issuer=False)
            ]
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "store.sqlite"
            self.build(path, fiscal, actions)
            with sqlite3.connect(path) as connection:
                payload = json.loads(
                    connection.execute(
                        "SELECT payload FROM stocks WHERE code = '9433'"
                    ).fetchone()[0]
                )
        self.assertEqual(payload["annual"], {"2025": 36.25, "2026": 40.0})
        # 比なので連続増配・増配率は分割で変わらない
        self.assertEqual(payload["streakIncrease"], 1)

    def test_unreliable_stock_hides_years_but_keeps_the_series(self) -> None:
        """株式分割の基準ズレで数えられない銘柄は、年数だけ空欄にする。

        イエローハット(9882)を模した形。2025年度100円→2026年度62円と並ぶが、
        2025年4月1日の1→2分割が入っているので62円は分割後の基準。
        0年ではなくNULLにし、配当系列（グラフ）はそのまま残す。
        """
        fiscal = {
            "9433": {
                "series": {2023: 90.0, 2024: 95.0, 2025: 100.0, 2026: 62.0},
                "fiscalMonth": 3,
                "connectionStatus": "edinet_only",
                "connectionReason": "",
                "externalSource": None,
                "externalYears": [],
                "streakReliable": False,
                "streakUnreliableReason": "split_basis",
                "streakUnreliableNote": "株式分割の基準がそろっていない",
                "streakBreakYears": [2025, 2026],
            }
        }
        frozen = {"9999": {"name": "系列なし", "series": {2024: 19.0, 2025: 20.0}}}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "store.sqlite"
            self.build(path, fiscal, {}, frozen=frozen)
            with sqlite3.connect(path) as connection:
                row = connection.execute(
                    "SELECT streak, streak_nd, streak_capped,"
                    " streak_nd_capped, streak_unreliable, cagr3, payload"
                    " FROM stocks WHERE code = '9433'"
                ).fetchone()
                # NULLは比較条件に一致しないので、絞り込みから自動的に外れる
                filtered = connection.execute(
                    "SELECT COUNT(*) FROM stocks WHERE streak >= 0"
                ).fetchone()[0]
                unreliable_count = connection.execute(
                    "SELECT COUNT(*) FROM stocks WHERE streak_unreliable = 1"
                ).fetchone()[0]

        streak, streak_nd, capped, nd_capped, unreliable, cagr3, raw = row
        self.assertIsNone(streak)
        self.assertIsNone(streak_nd)
        self.assertEqual(capped, 0)
        self.assertEqual(nd_capped, 0)
        self.assertEqual(unreliable, 1)
        # 3年増配率は2023→2026なので基準の切れ目をまたぐ
        self.assertIsNone(cagr3)

        payload = json.loads(raw)
        self.assertIsNone(payload["streakIncrease"])
        self.assertIsNone(payload["streakNonDecrease"])
        self.assertIsNone(payload["cagr3"])
        self.assertEqual(payload["streakUnreliable"]["reason"], "split_basis")
        self.assertEqual(
            payload["streakUnreliable"]["breakYears"], [2025, 2026]
        )
        # 配当系列は消さない（グラフは出せる）
        self.assertEqual(
            payload["annual"],
            {"2023": 90.0, "2024": 95.0, "2025": 100.0, "2026": 62.0},
        )
        # 系列が無い9999だけが残り、伏せた銘柄は絞り込みに出てこない
        self.assertEqual(filtered, 1)
        self.assertEqual(unreliable_count, 1)

    def test_reliable_stock_is_not_flagged(self) -> None:
        fiscal = {
            "9433": {
                "series": {2025: 72.5, 2026: 80.0},
                "fiscalMonth": 3,
                "connectionStatus": "edinet_only",
                "connectionReason": "",
                "externalSource": None,
                "externalYears": [],
                "streakReliable": True,
                "streakUnreliableReason": None,
                "streakUnreliableNote": None,
                "streakBreakYears": [],
            }
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "store.sqlite"
            self.build(path, fiscal, {})
            with sqlite3.connect(path) as connection:
                streak, unreliable, raw = connection.execute(
                    "SELECT streak, streak_unreliable, payload FROM stocks"
                    " WHERE code = '9433'"
                ).fetchone()
        self.assertEqual(streak, 1)
        self.assertEqual(unreliable, 0)
        self.assertIsNone(json.loads(raw)["streakUnreliable"])


class StreakBasisFlagTest(unittest.TestCase):
    """印の読み取りと、増配率を落とす区間の判定。"""

    def load(self, document: dict) -> dict:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fiscal_dividends.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            return build_store.load_fiscal_dividends(path)

    def test_reads_the_flag_written_by_edinet_direct(self) -> None:
        loaded = self.load(
            {
                "9882": {
                    "fiscalMonth": 3,
                    "series": {"2025": 100.0, "2026": 62.0},
                    "connection": {},
                    "streakBasis": {
                        "reliable": False,
                        "reason": "split_basis",
                        "note": "テスト",
                        "breakYears": [2025, 2026],
                    },
                },
                "9433": {
                    "fiscalMonth": 3,
                    "series": {"2025": 72.5, "2026": 80.0},
                    "connection": {},
                    "streakBasis": {"reliable": True},
                },
            }
        )
        self.assertFalse(loaded["9882"]["streakReliable"])
        self.assertEqual(loaded["9882"]["streakUnreliableReason"], "split_basis")
        self.assertEqual(loaded["9882"]["streakBreakYears"], [2025, 2026])
        self.assertTrue(loaded["9433"]["streakReliable"])
        self.assertEqual(loaded["9433"]["streakBreakYears"], [])

    def test_missing_flag_is_treated_as_countable(self) -> None:
        """印が無い（印を付ける前の版の）ファイルでも壊れずに動く。"""
        loaded = self.load(
            {"9433": {"fiscalMonth": 3, "series": {"2026": 80.0}, "connection": {}}}
        )
        self.assertTrue(loaded["9433"]["streakReliable"])

    def test_only_the_spans_that_cross_the_break_are_dropped(self) -> None:
        series = {year: float(year - 2000) for year in range(2015, 2027)}
        stats = build_store.fiscal_dividend_stats(
            series, streak_reliable=False, break_years=[2021, 2022]
        )
        self.assertIsNone(stats["streakIncrease"])
        self.assertIsNone(stats["streakNonDecrease"])
        # 2023〜2026はまたがないので残る、2016〜2026・2021〜2026はまたぐ
        self.assertIsNotNone(stats["cagr3"])
        self.assertIsNone(stats["cagr5"])
        self.assertIsNone(stats["cagr10"])

    def test_unknown_break_years_drop_every_span(self) -> None:
        series = {year: float(year - 2000) for year in range(2015, 2027)}
        stats = build_store.fiscal_dividend_stats(
            series, streak_reliable=False, break_years=[]
        )
        self.assertIsNone(stats["cagr3"])
        self.assertIsNone(stats["cagr5"])
        self.assertIsNone(stats["cagr10"])

    def test_real_file_marks_the_reviewed_stocks(self) -> None:
        path = ROOT / "data" / "fiscal_dividends.json"
        if not path.exists():
            self.skipTest(
                "data/fiscal_dividends.json は外部由来のためリポジトリに含めない。"
                "ConoHaから取得するか edinet-direct からコピーして実行する。"
            )
        loaded = build_store.load_fiscal_dividends(path)
        unreliable = [
            code for code, record in loaded.items() if not record["streakReliable"]
        ]
        self.assertGreater(len(unreliable), 50)
        self.assertIn("9882", unreliable)
        self.assertNotIn("9433", unreliable)


class PendingDividendsTest(unittest.TestCase):
    TODAY = date(2026, 8, 5)

    def pending(self, record: dict, years: set[int], **kwargs) -> dict:
        return build_store.pending_dividends(
            record, years, today=self.TODAY, **kwargs
        )

    def test_forecast_year_is_read_from_the_explicit_field(self) -> None:
        result = self.pending(
            {"forecastDividend": 84.0, "forecastFiscalYear": 2027},
            {2025, 2026},
        )
        self.assertEqual(list(result), ["2027"])
        self.assertEqual(result["2027"]["kind"], "forecast")
        self.assertEqual(result["2027"]["value"], 84.0)

    def test_forecast_year_falls_back_to_the_display_string(self) -> None:
        for period, expected in (
            ("2027年3月期(予)", "2027"),
            ("FY2027", "2027"),
        ):
            with self.subTest(period=period):
                result = self.pending(
                    {"forecastDividend": 84.0, "forecastPeriod": period},
                    {2026},
                )
                self.assertEqual(list(result), [expected])

    def test_year_already_in_the_series_is_not_repeated(self) -> None:
        # EDINETの有報から実績が取れている年は、そちらが正
        result = self.pending(
            {
                "confirmedDividend": 80.0,
                "confirmedFiscalYearEnd": "2026-03-31",
                "forecastDividend": 84.0,
                "forecastFiscalYear": 2027,
            },
            {2025, 2026},
        )
        self.assertEqual(list(result), ["2027"])

    def test_confirmed_and_forecast_can_both_appear(self) -> None:
        result = self.pending(
            {
                "confirmedDividend": 80.0,
                "confirmedFiscalYearEnd": "2026-03-31",
                "forecastDividend": 84.0,
                "forecastFiscalYear": 2027,
                "lastFetchedAt": "2026-08-05",
            },
            {2024, 2025},
        )
        self.assertEqual(result["2026"]["kind"], "confirmed")
        self.assertEqual(result["2027"]["kind"], "forecast")
        self.assertEqual(result["2026"]["fetchedAt"], "2026-08-05")

    def test_stale_year_behind_the_series_is_dropped(self) -> None:
        result = self.pending(
            {"confirmedDividend": 60.0, "confirmedFiscalYearEnd": "2024-03-31"},
            {2025, 2026},
        )
        self.assertEqual(result, {})

    def test_absurd_future_year_is_dropped(self) -> None:
        result = self.pending(
            {"forecastDividend": 84.0, "forecastFiscalYear": 2031}, {2026}
        )
        self.assertEqual(result, {})

    def test_zero_and_missing_values_produce_no_bar(self) -> None:
        self.assertEqual(
            self.pending({"forecastDividend": 0, "forecastFiscalYear": 2027}, {2026}),
            {},
        )
        self.assertEqual(self.pending({}, {2026}), {})
        self.assertEqual(self.pending(None, {2026}), {})

    def test_split_factor_is_applied(self) -> None:
        # 対象年度(2027年3月期、period end 2027-03-31)より後に効力が発生する
        # 分割なので、まだ古い株数基準の値を現在の基準へ揃える必要がある。
        adjustment = {
            "events": [
                {
                    "adjustmentFactor": 0.5,
                    "effectiveDate": "2027-04-01",
                    "applyDividendAdjustment": True,
                }
            ]
        }
        result = self.pending(
            {"forecastDividend": 100.0, "forecastFiscalYear": 2027},
            {2026},
            adjustment=adjustment,
            fiscal_month=3,
        )
        self.assertEqual(result["2027"]["value"], 50.0)

    def test_split_factor_is_not_applied_after_the_effective_date(self) -> None:
        """SPK(7466)で直したかった症状そのもの。

        分割(2026-04-01)は対象年度(2027年3月期、period end 2027-03-31)より前に
        発効済み。edinetdb.jpの予想はその期の実際の株数で発表された値なので、
        台帳の係数をさらに掛けると二重補正になる(41円が20.5円に潰れる)。
        """
        adjustment = {
            "events": [
                {
                    "adjustmentFactor": 0.5,
                    "effectiveDate": "2026-04-01",
                    "applyDividendAdjustment": True,
                }
            ]
        }
        result = self.pending(
            {"forecastDividend": 41.0, "forecastFiscalYear": 2027},
            {2025, 2026},
            adjustment=adjustment,
            fiscal_month=3,
        )
        self.assertEqual(result["2027"]["value"], 41.0)

    def test_split_factor_applies_to_a_confirmed_year_before_the_split(
        self,
    ) -> None:
        """確定額（会社発表の確定年度配当）も予想と同じ判定にする。"""
        adjustment = {
            "events": [
                {
                    "adjustmentFactor": 0.5,
                    "effectiveDate": "2026-04-01",
                    "applyDividendAdjustment": True,
                }
            ]
        }
        result = self.pending(
            {
                "confirmedDividend": 80.0,
                "confirmedFiscalYearEnd": "2025-03-31",
            },
            {2023, 2024},
            adjustment=adjustment,
            fiscal_month=3,
        )
        self.assertEqual(result["2025"]["value"], 40.0)

    def test_split_factor_does_not_apply_to_a_confirmed_year_after_the_split(
        self,
    ) -> None:
        adjustment = {
            "events": [
                {
                    "adjustmentFactor": 0.5,
                    "effectiveDate": "2026-04-01",
                    "applyDividendAdjustment": True,
                }
            ]
        }
        result = self.pending(
            {
                "confirmedDividend": 41.0,
                "confirmedFiscalYearEnd": "2027-03-31",
            },
            {2025, 2026},
            adjustment=adjustment,
            fiscal_month=3,
        )
        self.assertEqual(result["2027"]["value"], 41.0)


# 東計電算(4746 / E05066 / 12月決算)の実データ。2026-08-03開示のQ2。
# 2026-10-01に1株→4株の分割があり、中間86.5円は分割前の株数に、
# 期末97.5円は分割後の株数に対して払われる（足した184円はどの株数の
# 話でもない）。fetch_forecasts.py が state に書く形。
TOUKEI_Q2 = {
    "forecastDividend": 97.5,
    "forecastInterimDividend": 86.5,
    "forecastFinalDividend": 97.5,
    "forecastDividendAdjusted": 119.125,
    "forecastSplitFactor": 4,
    "forecastSplitEffectiveDate": "2026-10-01",
    "forecastShareBasis": "indeterminate",
    "forecastPeriod": "2026年12月期(予)",
    "forecastFiscalYear": 2026,
    "confirmedDividend": None,
    "confirmedFiscalYearEnd": "2026-12-31",
    "lastFetchedAt": "2026-08-06",
}
# 併合(5株→1株)を控えた銘柄の想定データ。実在の未来イベントが台帳に0件のため
# 合成データで検証する。forecastSplitFactor は「1株→N株のN」なので併合は0.2。
# 中間10円は併合前の1株に、期末60円は併合後の1株に対して支払われる想定。
# 中間+期末=70 ≠ 年間60 なので混在期と判定される（東計電算Q2と同じ構図）。
GAPPEI_Q2 = {
    "forecastDividend": 60.0,
    "forecastInterimDividend": 10.0,
    "forecastFinalDividend": 60.0,
    "forecastDividendAdjusted": 110.0,
    "forecastSplitFactor": 0.2,
    "forecastSplitEffectiveDate": "2026-10-01",
    "forecastShareBasis": "indeterminate",
    "forecastPeriod": "2026年12月期(予)",
    "forecastFiscalYear": 2026,
    "confirmedDividend": None,
    "confirmedFiscalYearEnd": "2026-12-31",
    "lastFetchedAt": "2026-08-06",
}
# 同じ銘柄の2026-05-07開示(Q1)。年間予想173円が分割前の株数で揃っていると
# API側が申告している（forecast_share_basis = pre_split）。
TOUKEI_Q1 = {
    "forecastDividend": 173,
    "forecastInterimDividend": 62.5,
    "forecastFinalDividend": 110.5,
    "forecastDividendAdjusted": 43.25,
    "forecastSplitFactor": 4,
    "forecastSplitEffectiveDate": "2026-10-01",
    "forecastShareBasis": "pre_split",
    "forecastPeriod": "2026年12月期(予)",
    "forecastFiscalYear": 2026,
    "confirmedDividend": 173,
    "confirmedFiscalYearEnd": "2026-12-31",
    "lastFetchedAt": "2026-05-08",
}
# 分割前後の株価。市場が分割日に株価を1/4にするので、配当も同じ日に
# 基準を切り替えれば利回りが連続する。
TOUKEI_PRICE_BEFORE = 5630.0
TOUKEI_PRICE_AFTER = 1407.5


class ForecastSplitBasisTest(unittest.TestCase):
    """分割日をまたぐ期の予想配当を、株価と同じ株数基準に揃える。"""

    BEFORE = date(2026, 9, 30)
    ON_DAY = date(2026, 10, 1)
    AFTER = date(2026, 11, 4)

    def test_before_the_split_the_yearend_is_scaled_up(self) -> None:
        resolved = build_store.forecast_on_price_basis(
            TOUKEI_Q2, today=self.BEFORE
        )
        # 86.5 + 97.5×4
        self.assertEqual(resolved["value"], 476.5)
        self.assertEqual(resolved["basis"], "pre_split_composed")
        # 内訳も同じ基準に揃える（足して表示値になる）
        self.assertEqual(resolved["interim"], 86.5)
        self.assertEqual(resolved["final"], 390.0)

    def test_after_the_split_the_adjusted_value_is_used(self) -> None:
        for today in (self.ON_DAY, self.AFTER):
            with self.subTest(today=today):
                resolved = build_store.forecast_on_price_basis(
                    TOUKEI_Q2, today=today
                )
                # 86.5÷4 + 97.5
                self.assertEqual(resolved["value"], 119.125)
                self.assertEqual(resolved["basis"], "post_split_adjusted")
                self.assertEqual(resolved["interim"], 21.625)
                self.assertEqual(resolved["final"], 97.5)

    def test_the_yield_is_continuous_across_the_split(self) -> None:
        """分割の前後で利回りが同じになる（ここが壊れると表示が1/4になる）。"""
        before = build_store.forecast_values(
            TOUKEI_Q2, TOUKEI_PRICE_BEFORE, today=self.BEFORE
        )
        after = build_store.forecast_values(
            TOUKEI_Q2, TOUKEI_PRICE_AFTER, today=self.AFTER
        )
        self.assertEqual(before[0], 476.5)
        self.assertEqual(after[0], 119.125)
        self.assertEqual(before[1], 8.46)
        self.assertEqual(after[1], 8.46)
        self.assertEqual(before[4], "pre_split_composed")
        self.assertEqual(after[4], "post_split_adjusted")

    def test_api_adjusted_value_alone_would_quarter_the_yield(self) -> None:
        """直したかった症状そのもの。分割前の株価に分割後の配当を当てると1/4。"""
        wrong = round(119.125 / TOUKEI_PRICE_BEFORE * 100, 2)
        self.assertEqual(wrong, 2.12)
        right = build_store.forecast_values(
            TOUKEI_Q2, TOUKEI_PRICE_BEFORE, today=self.BEFORE
        )[1]
        self.assertEqual(right, 8.46)

    def test_a_reverse_split_composes_before_the_effective_date(self) -> None:
        """併合(係数0.2)でも同じ式が成立する。併合前は期末を併合前基準へ縮める。"""
        resolved = build_store.forecast_on_price_basis(
            GAPPEI_Q2, today=self.BEFORE
        )
        # 10 + 60×0.2 = 22.0（併合前の1株基準）
        self.assertEqual(resolved["value"], 22.0)
        self.assertEqual(resolved["basis"], "pre_split_composed")
        self.assertEqual(resolved["interim"], 10.0)
        self.assertEqual(resolved["final"], 12.0)

    def test_a_reverse_split_switches_to_adjusted_after_the_effective_date(
        self,
    ) -> None:
        for today in (self.ON_DAY, self.AFTER):
            with self.subTest(today=today):
                resolved = build_store.forecast_on_price_basis(
                    GAPPEI_Q2, today=today
                )
                # 10÷0.2 + 60 = 110.0（併合後の1株基準）
                self.assertEqual(resolved["value"], 110.0)
                self.assertEqual(resolved["basis"], "post_split_adjusted")
                self.assertEqual(resolved["interim"], 50.0)
                self.assertEqual(resolved["final"], 60.0)

    def test_the_yield_is_continuous_across_a_reverse_split(self) -> None:
        """併合の前後で利回りが連続する（株価は併合日に市場が5倍にする）。"""
        before = build_store.forecast_values(GAPPEI_Q2, 100.0, today=self.BEFORE)
        after = build_store.forecast_values(GAPPEI_Q2, 500.0, today=self.AFTER)
        self.assertEqual(before[0], 22.0)
        self.assertEqual(after[0], 110.0)
        self.assertEqual(before[1], 22.0)
        self.assertEqual(after[1], 22.0)

    def test_a_yearly_forecast_already_on_one_basis_is_not_recomposed(
        self,
    ) -> None:
        """内訳の合計が年間値と一致する期は、組み立てると係数が二重に掛かる。"""
        resolved = build_store.forecast_on_price_basis(
            TOUKEI_Q1, today=self.BEFORE
        )
        self.assertEqual(resolved["value"], 173)
        self.assertEqual(resolved["basis"], "single_basis_as_reported")
        # 62.5 + 110.5×4 = 504.5 は誤り（API側の 43.25×4 = 173 とも合わない）
        self.assertNotEqual(resolved["value"], 504.5)
        after = build_store.forecast_values(
            TOUKEI_Q1, TOUKEI_PRICE_AFTER, today=self.AFTER
        )
        self.assertEqual(after[0], 43.25)
        self.assertEqual(
            build_store.forecast_values(
                TOUKEI_Q1, TOUKEI_PRICE_BEFORE, today=self.BEFORE
            )[1],
            after[1],
        )



    def test_composes_after_the_split_when_the_api_value_is_missing(self) -> None:
        record = dict(TOUKEI_Q2)
        record["forecastDividendAdjusted"] = None
        resolved = build_store.forecast_on_price_basis(record, today=self.AFTER)
        self.assertEqual(resolved["value"], 119.125)
        self.assertEqual(resolved["basis"], "post_split_composed")

    def test_falls_back_to_the_raw_value_when_a_leg_is_missing(self) -> None:
        for missing in ("forecastInterimDividend", "forecastFinalDividend"):
            with self.subTest(missing=missing):
                record = dict(TOUKEI_Q2)
                record[missing] = None
                record["forecastDividendAdjusted"] = None
                for today in (self.BEFORE, self.AFTER):
                    resolved = build_store.forecast_on_price_basis(
                        record, today=today
                    )
                    self.assertEqual(resolved["value"], 97.5)
                    self.assertEqual(resolved["basis"], "raw")

    def test_unusable_factors_never_multiply_or_divide(self) -> None:
        for factor in (0, -4, None, "4", float("nan")):
            with self.subTest(factor=factor):
                record = dict(TOUKEI_Q2)
                record["forecastSplitFactor"] = factor
                for today in (self.BEFORE, self.AFTER):
                    resolved = build_store.forecast_on_price_basis(
                        record, today=today
                    )
                    self.assertEqual(resolved["value"], 97.5)
                    self.assertEqual(resolved["basis"], "raw")

    def test_a_broken_effective_date_is_ignored(self) -> None:
        for raw in ("", "2026-13-01", "近日", None, 20261001):
            with self.subTest(raw=raw):
                record = dict(TOUKEI_Q2)
                record["forecastSplitEffectiveDate"] = raw
                resolved = build_store.forecast_on_price_basis(
                    record, today=self.BEFORE
                )
                self.assertEqual(resolved["value"], 97.5)
                self.assertEqual(resolved["basis"], "raw")

    def test_stocks_without_a_split_keep_the_previous_behaviour(self) -> None:
        """大多数の銘柄。今までどおり forecastDividend がそのまま出る。"""
        plain = {
            "forecastDividend": 84.0,
            "forecastInterimDividend": 40.0,
            "forecastFinalDividend": 44.0,
            "forecastFiscalYear": 2027,
        }
        resolved = build_store.forecast_on_price_basis(plain, today=self.BEFORE)
        self.assertEqual(resolved["value"], 84.0)
        self.assertEqual(resolved["basis"], "raw")
        self.assertEqual(resolved["interim"], 40.0)
        self.assertEqual(resolved["final"], 44.0)
        values = build_store.forecast_values(plain, 2000.0, today=self.BEFORE)
        self.assertEqual(values[0], 84.0)
        self.assertEqual(values[1], 4.2)
        self.assertEqual(values[4], "raw")

    def test_missing_record_is_still_handled(self) -> None:
        for record in (None, {}, "文字列"):
            with self.subTest(record=record):
                resolved = build_store.forecast_on_price_basis(
                    record, today=self.BEFORE
                )
                self.assertIsNone(resolved["value"])
                self.assertEqual(resolved["basis"], "raw")
        self.assertEqual(
            build_store.forecast_values(None, 100.0, today=self.BEFORE),
            (None, None, None, None, None),
        )

    def test_pending_bar_switches_basis_on_the_split_day(self) -> None:
        before = build_store.pending_dividends(
            TOUKEI_Q2, {2024, 2025}, today=self.BEFORE
        )
        after = build_store.pending_dividends(
            TOUKEI_Q2, {2024, 2025}, today=self.AFTER
        )
        self.assertEqual(before["2026"]["value"], 476.5)
        self.assertEqual(before["2026"]["basis"], "pre_split_composed")
        self.assertEqual(before["2026"]["interim"], 86.5)
        self.assertEqual(before["2026"]["final"], 390.0)
        self.assertEqual(after["2026"]["value"], 119.125)
        self.assertEqual(after["2026"]["basis"], "post_split_adjusted")
        # 内訳の合計が表示値と合う
        self.assertAlmostEqual(
            after["2026"]["interim"] + after["2026"]["final"],
            after["2026"]["value"],
        )

    def test_pending_bar_is_not_double_adjusted_when_the_ledger_also_knows_the_split(
        self,
    ) -> None:
        """東計電算型: 台帳にも同じ分割イベントが載っていても二重に掛からない。

        分割(2026-10-01)は対象年度(2026年12月期、period end 2026-12-31)の
        期中に発効する。period end は効力発生日より後なので、台帳の係数は
        (adjustment_factor_for_period の判定どおり)効かない。分割日をまたぐ
        期の組み立ては forecast_on_price_basis 側がすでに担っている。
        """
        ledger_adjustment = {
            "events": [
                {
                    # 東計電算(1→4分割)の old/new。
                    "adjustmentFactor": 0.25,
                    "effectiveDate": "2026-10-01",
                    "applyDividendAdjustment": True,
                }
            ]
        }
        before = build_store.pending_dividends(
            TOUKEI_Q2,
            {2024, 2025},
            today=self.BEFORE,
            adjustment=ledger_adjustment,
            fiscal_month=12,
        )
        after = build_store.pending_dividends(
            TOUKEI_Q2,
            {2024, 2025},
            today=self.AFTER,
            adjustment=ledger_adjustment,
            fiscal_month=12,
        )
        # adjustment を渡さない場合(既存テスト)とまったく同じ値になる。
        self.assertEqual(before["2026"]["value"], 476.5)
        self.assertEqual(after["2026"]["value"], 119.125)

    def test_pending_bar_without_a_split_has_no_basis_key(self) -> None:
        """分割の無い銘柄のバーは今までと同じ形（余計なキーを足さない）。"""
        result = build_store.pending_dividends(
            {
                "forecastDividend": 84.0,
                "forecastInterimDividend": 40.0,
                "forecastFinalDividend": 44.0,
                "forecastFiscalYear": 2027,
            },
            {2026},
            today=self.BEFORE,
        )
        self.assertEqual(result["2027"]["value"], 84.0)
        self.assertNotIn("basis", result["2027"])
        self.assertEqual(result["2027"]["interim"], 40.0)
        self.assertEqual(result["2027"]["final"], 44.0)


class ForecastLegsSelfCheckTest(unittest.TestCase):
    """組み立てるかどうかは、申告ではなくデータ自身の検算で決める。

    中間＋期末が会社発表の年間値と一致すれば、その期の予想はどこかの
    一つの株数基準で書かれている。一致しなければ、中間と期末が別々の
    株数に対する金額（＝期中分割）だと分かる。
    """

    BEFORE = date(2026, 9, 30)

    def resolve(self, **overrides) -> dict:
        record = {
            "forecastSplitFactor": 4,
            "forecastSplitEffectiveDate": "2026-10-01",
            **overrides,
        }
        return build_store.forecast_on_price_basis(record, today=self.BEFORE)

    def test_the_check_separates_the_two_real_disclosures(self) -> None:
        self.assertFalse(build_store.legs_match_annual(86.5, 97.5, 97.5))
        self.assertTrue(build_store.legs_match_annual(62.5, 110.5, 173))

    def test_the_route_does_not_depend_on_the_reported_basis(self) -> None:
        """forecast_share_basis が何であっても、また無くても同じ結果になる。"""
        for reported in ("indeterminate", "pre_split", "post_split", "", None):
            with self.subTest(reported=reported, legs="混在"):
                mixed = dict(TOUKEI_Q2)
                mixed["forecastShareBasis"] = reported
                resolved = build_store.forecast_on_price_basis(
                    mixed, today=self.BEFORE
                )
                self.assertEqual(resolved["value"], 476.5)
                self.assertEqual(resolved["basis"], "pre_split_composed")
            with self.subTest(reported=reported, legs="単一"):
                single = dict(TOUKEI_Q1)
                single["forecastShareBasis"] = reported
                resolved = build_store.forecast_on_price_basis(
                    single, today=self.BEFORE
                )
                self.assertEqual(resolved["value"], 173)
                self.assertEqual(resolved["basis"], "single_basis_as_reported")

    def test_a_missing_share_basis_key_is_fine(self) -> None:
        for source, expected, basis in (
            (TOUKEI_Q2, 476.5, "pre_split_composed"),
            (TOUKEI_Q1, 173, "single_basis_as_reported"),
        ):
            with self.subTest(expected=expected):
                record = {
                    key: value
                    for key, value in source.items()
                    if key != "forecastShareBasis"
                }
                resolved = build_store.forecast_on_price_basis(
                    record, today=self.BEFORE
                )
                self.assertEqual(resolved["value"], expected)
                self.assertEqual(resolved["basis"], basis)

    def test_post_split_with_matching_legs_is_left_alone(self) -> None:
        """中間も期末も分割後基準なら合計が年間値と合う。組み立てない。"""
        resolved = self.resolve(
            forecastDividend=119.125,
            forecastInterimDividend=21.625,
            forecastFinalDividend=97.5,
            forecastShareBasis="post_split",
        )
        self.assertEqual(resolved["value"], 119.125)
        self.assertEqual(resolved["basis"], "single_basis_as_reported")

    def test_post_split_with_mismatched_legs_is_still_composed(self) -> None:
        """申告と検算が食い違うときは検算を採る（判断の理由はコードのコメント）。"""
        resolved = self.resolve(
            forecastDividend=97.5,
            forecastInterimDividend=86.5,
            forecastFinalDividend=97.5,
            forecastShareBasis="post_split",
        )
        self.assertEqual(resolved["value"], 476.5)
        self.assertEqual(resolved["basis"], "pre_split_composed")

    def test_small_rounding_gaps_still_count_as_a_match(self) -> None:
        # 年間値100円なら許容は0.5円（0.01円と0.5%の大きいほう）
        for legs_total, matches in ((100.4, True), (100.5, True), (100.6, False)):
            with self.subTest(total=legs_total):
                resolved = self.resolve(
                    forecastDividend=100.0,
                    forecastInterimDividend=50.0,
                    forecastFinalDividend=legs_total - 50.0,
                )
                self.assertEqual(
                    resolved["basis"],
                    "single_basis_as_reported" if matches else "pre_split_composed",
                )

    def test_tiny_dividends_use_the_absolute_tolerance(self) -> None:
        # 年間1円なら0.5%は0.005円。0.01円まで同じとみなす
        self.assertTrue(build_store.legs_match_annual(0.5, 0.505, 1.0))
        self.assertFalse(build_store.legs_match_annual(0.5, 0.52, 1.0))

    def test_a_missing_leg_cannot_be_checked(self) -> None:
        self.assertFalse(build_store.legs_match_annual(None, 97.5, 97.5))
        self.assertFalse(build_store.legs_match_annual(86.5, None, 97.5))
        self.assertFalse(build_store.legs_match_annual(86.5, 97.5, None))

    def test_without_legs_a_pre_split_claim_is_still_used(self) -> None:
        """検算できないときだけ、申告を判断材料にする。"""
        resolved = self.resolve(
            forecastDividend=173,
            forecastInterimDividend=None,
            forecastFinalDividend=None,
            forecastShareBasis="pre_split",
        )
        self.assertEqual(resolved["value"], 173)
        self.assertEqual(resolved["basis"], "pre_split_reported")

    def test_without_legs_and_without_a_claim_the_raw_value_is_kept(self) -> None:
        resolved = self.resolve(
            forecastDividend=173,
            forecastInterimDividend=None,
            forecastFinalDividend=None,
            forecastShareBasis="indeterminate",
        )
        self.assertEqual(resolved["value"], 173)
        self.assertEqual(resolved["basis"], "raw")

class ForecastSplitBasisInStoreTest(unittest.TestCase):
    """DBに書き出すところまで通して確かめる。"""

    def build(self, path: Path, today: date, price: float) -> dict:
        financials = [
            {
                "code": "4746",
                "name": "東計電算",
                "dividendPerShare": {"2025": 173.0},
            }
        ]
        fiscal = {
            "4746": {
                "series": {2024: 160.0, 2025: 173.0},
                "fiscalMonth": 12,
                "connectionStatus": "direct",
                "connectionReason": "テスト",
                "externalSource": None,
                "externalYears": [],
            }
        }
        with mock.patch.object(
            build_store,
            "load_daily_prices",
            return_value=({"4746": price}, {"4746": 173.0}, "2026-09-30"),
        ):
            build_store.create_database(
                path,
                financials,
                {},
                {"4746": {"code": "4746", "name": "東計電算"}},
                {"4746": dict(TOUKEI_Q2)},
                [Path("f"), Path("s"), Path("t"), Path("fc")],
                "fixture.csv",
                {},
                Path("stock_actions.json"),
                fiscal,
                Path("fiscal_dividends.json"),
                {},
                Path("calendar_dividends_frozen.json"),
                today,
            )
        with sqlite3.connect(path) as connection:
            code, forecast_yield, payload = connection.execute(
                "SELECT code, forecast_yield, payload FROM stocks"
            ).fetchone()
        return {"forecastYieldColumn": forecast_yield, **json.loads(payload)}

    def test_the_store_switches_basis_on_the_split_day(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            before = self.build(
                Path(directory) / "before.sqlite",
                date(2026, 9, 30),
                TOUKEI_PRICE_BEFORE,
            )
            after = self.build(
                Path(directory) / "after.sqlite",
                date(2026, 10, 1),
                TOUKEI_PRICE_AFTER,
            )
        self.assertEqual(before["forecastDividend"], 476.5)
        self.assertEqual(before["forecastYield"], 8.46)
        self.assertEqual(before["forecastBasis"], "pre_split_composed")
        self.assertEqual(before["forecastYieldColumn"], 8.46)
        self.assertEqual(before["annualPending"]["2026"]["value"], 476.5)
        self.assertEqual(before["annualPartial"]["2026"], 476.5)

        self.assertEqual(after["forecastDividend"], 119.125)
        self.assertEqual(after["forecastYield"], 8.46)
        self.assertEqual(after["forecastBasis"], "post_split_adjusted")
        self.assertEqual(after["forecastYieldColumn"], 8.46)
        self.assertEqual(after["annualPending"]["2026"]["value"], 119.125)
        # 系列に入っている実績年は今までどおり
        self.assertEqual(before["annual"], {"2024": 160.0, "2025": 173.0})
        self.assertEqual(after["annual"], {"2024": 160.0, "2025": 173.0})


class PayoutSeriesTest(unittest.TestCase):
    def test_total_based_is_preferred(self) -> None:
        series = build_store.payout_series(
            {
                "payoutRatioTotalBased": {"2025": 43.77},
                "payoutRatioConsolidated": {"2025": 99.0},
            }
        )
        self.assertEqual(series, {"2025": 43.77})

    def test_falls_back_to_consolidated(self) -> None:
        series = build_store.payout_series(
            {"payoutRatioConsolidated": {"2025": 99.0}}
        )
        self.assertEqual(series, {"2025": 99.0})

    def test_outlier_years_are_dropped_from_the_line(self) -> None:
        # 利益がほぼゼロの年に数千%が入る。1年で縦軸が伸びて他が読めなくなる
        series = build_store.payout_series(
            {"payoutRatioTotalBased": {"2024": 40.0, "2025": 58375.81}}
        )
        self.assertEqual(series, {"2024": 40.0})

    def test_missing_source_returns_an_empty_line(self) -> None:
        self.assertEqual(build_store.payout_series({}), {})


class CalendarSnapshotLoaderTest(unittest.TestCase):
    def load(self, document: dict) -> dict:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "calendar_dividends_frozen.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            return build_store.load_calendar_dividends(path)

    def test_missing_file_is_not_an_error(self) -> None:
        self.assertEqual(
            build_store.load_calendar_dividends(Path("/does/not/exist.json")), {}
        )
        self.assertEqual(build_store.load_calendar_dividends(None), {})

    def test_reads_the_series(self) -> None:
        loaded = self.load(
            {"stocks": {"9501": {"name": "東京電力", "annual": {"2010": 60.0}}}}
        )
        self.assertEqual(loaded["9501"]["series"], {2010: 60.0})

    def test_skips_broken_entries(self) -> None:
        loaded = self.load(
            {
                "stocks": {
                    "9501": {"annual": {}},
                    "ながすぎるコード": {"annual": {"2010": 60.0}},
                    "9502": {"annual": {"2010": None, "2011": -5}},
                }
            }
        )
        self.assertEqual(loaded, {})

    def test_real_file_covers_the_stocks_without_a_fiscal_series(self) -> None:
        path = ROOT / "data" / "calendar_dividends_frozen.json"
        if not path.exists():
            self.skipTest(
                "data/calendar_dividends_frozen.json は外部由来のため"
                "リポジトリに含めない。ConoHaから取得して実行する。"
            )
        loaded = build_store.load_calendar_dividends(path)
        self.assertIn("9501", loaded)
        self.assertIn("5981", loaded)
        self.assertLess(len(loaded), 50)


class NoYahooDependencyTest(unittest.TestCase):
    """dividends.json を読むコードが残っていないことを確かめる。"""

    #  fiscal_dividends.json / calendar_dividends_frozen.json は別物なので、
    #  前に「_」や英数字が付かない dividends.json だけを探す。
    PATTERN = re.compile(r"(?<![\w_])dividends\.json")

    def test_scripts_do_not_read_the_retired_file(self) -> None:
        for name in ("build_store.py", "fetch_forecasts.py"):
            source = (ROOT / "scripts" / name).read_text(encoding="utf-8")
            code_lines = [
                line
                for line in source.splitlines()
                # 経緯を書いたコメント・docstringに名前が出るのは残してよい
                if self.PATTERN.search(line)
                and not line.lstrip().startswith("#")
                and "以前" not in line
                and "あちら" not in line
            ]
            self.assertEqual(code_lines, [], f"{name} に残っている行: {code_lines}")

    def test_build_runs_without_the_retired_file(self) -> None:
        """--dividends という引数自体が無くなっていること。"""
        for name in ("build_store.py", "fetch_forecasts.py"):
            source = (ROOT / "scripts" / name).read_text(encoding="utf-8")
            self.assertNotIn('"--dividends"', source, name)
