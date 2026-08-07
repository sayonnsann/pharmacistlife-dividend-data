import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


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


fetch_forecasts = load("fetch_forecasts")
refresh_one = load("refresh_one")


class ParseCodesTest(unittest.TestCase):
    def test_reads_a_comma_separated_list(self) -> None:
        self.assertEqual(
            refresh_one.parse_codes(" 4746, 9433 ,391A "),
            ["4746", "9433", "391A"],
        )

    def test_duplicates_are_collapsed(self) -> None:
        self.assertEqual(refresh_one.parse_codes("4746,4746"), ["4746"])

    def test_an_unreadable_code_is_rejected(self) -> None:
        for raw in ("47466", "47", "47-6", "東計電算"):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    refresh_one.parse_codes(raw)

    def test_an_empty_list_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            refresh_one.parse_codes("  , ,")

    def test_too_many_at_once_is_rejected(self) -> None:
        """押し間違いで1日の枠を使い切らないための歯止め。"""
        codes = ",".join(f"{1000 + index}" for index in range(21))
        with self.assertRaises(ValueError):
            refresh_one.parse_codes(codes)


class ParseOverridesTest(unittest.TestCase):
    def test_reads_the_three_part_form(self) -> None:
        self.assertEqual(
            refresh_one.parse_overrides("4746:E05066:12"),
            {"4746": ("E05066", 12)},
        )

    def test_an_empty_string_is_no_override(self) -> None:
        self.assertEqual(refresh_one.parse_overrides(""), {})

    def test_broken_forms_are_rejected(self) -> None:
        for raw in (
            "4746:E05066",
            "4746:E05066:12:13",
            "4746:05066:12",
            "4746:E05066:0",
            "4746:E05066:13",
            "4746:E05066:さんがつ",
        ):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    refresh_one.parse_overrides(raw)


class ResolveTargetTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.edinet_dir = Path(self.directory.name)
        (self.edinet_dir / "4746.json").write_text(
            json.dumps(
                {"code": "4746", "edinetCode": "E05066", "fiscalMonth": 12}
            ),
            encoding="utf-8",
        )
        self.addCleanup(self.directory.cleanup)

    def test_the_feed_supplies_the_edinet_code_and_fiscal_month(self) -> None:
        """code_map.json はこのリポジトリに無いが、配信用feedから引ける。"""
        target = refresh_one.resolve_target("4746", self.edinet_dir, {})
        self.assertEqual(target.edinet_code, "E05066")
        self.assertEqual(target.fiscal_month, 12)

    def test_the_real_repository_feed_has_what_we_need(self) -> None:
        target = refresh_one.resolve_target("4746", ROOT / "edinet", {})
        self.assertEqual(target.edinet_code, "E05066")
        self.assertEqual(target.fiscal_month, 12)

    def test_an_override_wins_over_the_feed(self) -> None:
        target = refresh_one.resolve_target(
            "4746", self.edinet_dir, {"4746": ("E99999", 3)}
        )
        self.assertEqual(target.edinet_code, "E99999")
        self.assertEqual(target.fiscal_month, 3)

    def test_a_missing_feed_says_how_to_fix_it(self) -> None:
        with self.assertRaises(ValueError) as caught:
            refresh_one.resolve_target("9999", self.edinet_dir, {})
        self.assertIn("--overrides", str(caught.exception))

    def test_a_broken_feed_is_rejected(self) -> None:
        for broken in (
            {"edinetCode": "05066", "fiscalMonth": 12},
            {"edinetCode": "E05066", "fiscalMonth": 0},
            {"edinetCode": "E05066"},
            {"edinetCode": "E05066", "fiscalMonth": True},
        ):
            with self.subTest(broken=broken):
                (self.edinet_dir / "1234.json").write_text(
                    json.dumps(broken), encoding="utf-8"
                )
                with self.assertRaises(ValueError):
                    refresh_one.resolve_target("1234", self.edinet_dir, {})


class RefreshOneMainTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        base = Path(self.directory.name)
        self.edinet_dir = base / "edinet"
        self.edinet_dir.mkdir()
        for code, edinet_code in (("4746", "E05066"), ("9433", "E04425")):
            (self.edinet_dir / f"{code}.json").write_text(
                json.dumps({"edinetCode": edinet_code, "fiscalMonth": 12}),
                encoding="utf-8",
            )
        self.state_path = base / "forecasts_state.json"
        self.state_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "queuePosition": 7,
                    "stocks": {
                        "4746": {
                            "forecastDividend": 173.0,
                            "lastFetchedAt": "2026-07-28",
                        },
                        "8058": {
                            "forecastDividend": 60.0,
                            "lastFetchedAt": "2026-07-30",
                        },
                    },
                }
            ),
            encoding="utf-8",
        )
        self.addCleanup(self.directory.cleanup)

    def state(self) -> dict:
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def run_main(self, codes: str, fetch: mock.Mock, dry_run: bool = False) -> str:
        argv = [
            "refresh_one.py",
            "--state",
            str(self.state_path),
            "--edinet-dir",
            str(self.edinet_dir),
            "--codes",
            codes,
            "--today",
            "2026-08-06",
        ]
        if dry_run:
            argv.append("--dry-run")
        buffer = io.StringIO()
        with mock.patch.dict(
            os.environ, {"EDINETDB_API_KEY": "test-key"}
        ), mock.patch.object(sys, "argv", argv), mock.patch.object(
            fetch_forecasts, "fetch_one", fetch
        ), contextlib.redirect_stdout(buffer):
            refresh_one.main()
        return buffer.getvalue()

    @staticmethod
    def succeeding() -> mock.Mock:
        return mock.Mock(
            return_value=(
                {
                    "forecastDividend": 97.5,
                    "forecastDividendAdjusted": 119.125,
                    "forecastSplitFactor": 4,
                    "forecastSplitEffectiveDate": "2026-10-01",
                },
                62,
            )
        )

    def test_only_the_named_stock_changes(self) -> None:
        self.run_main("4746", self.succeeding())
        stocks = self.state()["stocks"]
        self.assertEqual(stocks["4746"]["forecastDividend"], 97.5)
        self.assertEqual(stocks["4746"]["lastFetchedAt"], "2026-08-06")
        # 他の銘柄も巡回位置も触らない
        self.assertEqual(stocks["8058"]["forecastDividend"], 60.0)
        self.assertEqual(stocks["8058"]["lastFetchedAt"], "2026-07-30")
        self.assertEqual(self.state()["queuePosition"], 7)

    def test_several_stocks_at_once(self) -> None:
        fetch = self.succeeding()
        self.run_main("4746,9433", fetch)
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(
            sorted(self.state()["stocks"]), ["4746", "8058", "9433"]
        )

    def test_the_before_and_after_are_printed(self) -> None:
        output = self.run_main("4746", self.succeeding())
        self.assertIn("173.0", output)
        self.assertIn("97.5", output)
        self.assertIn("E05066", output)

    def test_a_low_remaining_quota_is_warned_about(self) -> None:
        fetch = mock.Mock(
            return_value=({"forecastDividend": 97.5}, 5)
        )
        output = self.run_main("4746", fetch)
        self.assertIn("edinetdb日次残量=5", output)
        self.assertIn("警告: edinetdbの本日残量が少ない", output)

    def test_a_dry_run_writes_nothing(self) -> None:
        before = self.state_path.read_text(encoding="utf-8")
        output = self.run_main("4746", self.succeeding(), dry_run=True)
        self.assertEqual(self.state_path.read_text(encoding="utf-8"), before)
        self.assertIn("97.5", output)
        self.assertIn("dry-run", output)

    def test_a_failure_keeps_the_saved_value_and_exits_non_zero(self) -> None:
        failing = mock.Mock(
            side_effect=fetch_forecasts.FetchError(
                "4746: edinetdb HTTP 503", kind="http", status=503
            )
        )
        with self.assertRaises(SystemExit) as caught:
            self.run_main("4746", failing)
        self.assertNotIn(caught.exception.code, (0, None))
        # 取れなかったときに前の値を消すと、画面から予想が消える
        self.assertEqual(self.state()["stocks"]["4746"]["forecastDividend"], 173.0)

    def test_one_failure_does_not_block_the_other_stock(self) -> None:
        def side_effect(candidate, api_key):
            if candidate.code == "4746":
                raise fetch_forecasts.FetchError(
                    "4746: edinetdb HTTP 500", kind="http", status=500
                )
            return {"forecastDividend": 210.0}, 61

        with self.assertRaises(SystemExit):
            self.run_main("4746,9433", mock.Mock(side_effect=side_effect))
        self.assertEqual(self.state()["stocks"]["9433"]["forecastDividend"], 210.0)

    def test_a_rate_limit_failure_still_allows_successful_stocks_to_be_saved(
        self,
    ) -> None:
        def side_effect(candidate, api_key):
            if candidate.code == "4746":
                raise fetch_forecasts.FetchError(
                    "4746: edinetdb HTTP 429", kind="http", status=429
                )
            return {"forecastDividend": 210.0}, 61

        self.run_main("4746,9433", mock.Mock(side_effect=side_effect))
        self.assertEqual(self.state()["stocks"]["9433"]["forecastDividend"], 210.0)

    def test_an_authentication_failure_still_exits_non_zero(self) -> None:
        failing = mock.Mock(
            side_effect=fetch_forecasts.FetchError(
                "4746: edinetdb HTTP 401", kind="http", status=401
            )
        )
        with self.assertRaises(SystemExit):
            self.run_main("4746", failing)

    def test_an_unknown_code_stops_before_spending_the_quota(self) -> None:
        fetch = self.succeeding()
        with self.assertRaises(ValueError):
            self.run_main("4746,1234", fetch)
        fetch.assert_not_called()

    def test_the_api_key_is_required(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
            sys,
            "argv",
            ["refresh_one.py", "--codes", "4746", "--state", str(self.state_path)],
        ):
            with self.assertRaises(SystemExit):
                refresh_one.main()


if __name__ == "__main__":
    unittest.main()
