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
from urllib.error import HTTPError


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "fetch_forecasts", ROOT / "scripts" / "fetch_forecasts.py"
)
assert SPEC and SPEC.loader
fetch_forecasts = importlib.util.module_from_spec(SPEC)
# dataclass はモジュールが sys.modules にいる前提で型を解決する
sys.modules["fetch_forecasts"] = fetch_forecasts
SPEC.loader.exec_module(fetch_forecasts)


# 東計電算(4746 / E05066 / 12月決算)のGET /v1/companies/E05066/earnings の実応答。
# 2026-10-01に1株→4株の分割があり、中間は分割前・期末は分割後の株数に対して払われる。
TOUKEI_Q2_EARNING = {
    "disclosure_date": "2026-08-03",
    "quarter": 2,
    "fiscal_year_end": "2026-12-31",
    "forecast_dividend_per_share": 97.5,
    "adjusted_forecast_dividend_per_share": 119.125,
    "forecast_split_adjustment_factor": 4.0,
    "forecast_split_effective_date": "2026-10-01",
    "forecast_share_basis": "indeterminate",
    "interim_dividend_per_share": 86.5,
    "yearend_dividend_per_share": 97.5,
}
TOUKEI_Q1_EARNING = {
    "disclosure_date": "2026-05-07",
    "quarter": 1,
    "fiscal_year_end": "2026-12-31",
    "forecast_dividend_per_share": 173.0,
    "adjusted_forecast_dividend_per_share": 43.25,
    "adjusted_annual_dividend_per_share": 173.0,
    "dividend_per_share": 173.0,
    "forecast_split_adjustment_factor": 4.0,
    "forecast_split_effective_date": "2026-10-01",
    "forecast_share_basis": "pre_split",
    "forecast_share_basis_evidence": "announcement_date",
    "interim_dividend_per_share": 62.5,
    "yearend_dividend_per_share": 110.5,
}


class ParseForecastResponseTest(unittest.TestCase):
    def parse(self, earning: dict, fiscal_month: int = 12) -> dict:
        return fetch_forecasts.parse_forecast_response(
            {"earnings": [earning]}, fiscal_month
        )

    def test_reads_the_interim_and_yearend_legs(self) -> None:
        """以前は forecast_ を頭に付けた名前を探していて常にNoneだった。"""
        parsed = self.parse(TOUKEI_Q2_EARNING)
        self.assertEqual(parsed["forecastInterimDividend"], 86.5)
        self.assertEqual(parsed["forecastFinalDividend"], 97.5)
        self.assertEqual(parsed["forecastDividend"], 97.5)

    def test_keeps_the_split_fields_needed_by_the_display(self) -> None:
        parsed = self.parse(TOUKEI_Q2_EARNING)
        self.assertEqual(parsed["forecastDividendAdjusted"], 119.125)
        self.assertEqual(parsed["forecastSplitFactor"], 4)
        self.assertEqual(parsed["forecastSplitEffectiveDate"], "2026-10-01")
        self.assertEqual(parsed["forecastShareBasis"], "indeterminate")

    def test_reads_the_pre_split_disclosure(self) -> None:
        parsed = self.parse(TOUKEI_Q1_EARNING)
        self.assertEqual(parsed["forecastInterimDividend"], 62.5)
        self.assertEqual(parsed["forecastFinalDividend"], 110.5)
        self.assertEqual(parsed["forecastDividend"], 173)
        self.assertEqual(parsed["forecastDividendAdjusted"], 43.25)
        self.assertEqual(parsed["forecastShareBasis"], "pre_split")

    def test_confirmed_dividend_still_prefers_the_adjusted_annual(self) -> None:
        """既存の挙動（adjusted_annual_dividend_per_share を優先）は変えない。"""
        parsed = self.parse(TOUKEI_Q1_EARNING)
        self.assertEqual(parsed["confirmedDividend"], 173)
        self.assertEqual(parsed["confirmedFiscalYearEnd"], "2026-12-31")
        without_adjusted = dict(TOUKEI_Q1_EARNING)
        without_adjusted.pop("adjusted_annual_dividend_per_share")
        without_adjusted["dividend_per_share"] = 160.0
        self.assertEqual(self.parse(without_adjusted)["confirmedDividend"], 160)

    def test_period_and_fiscal_year_are_unchanged(self) -> None:
        parsed = self.parse(TOUKEI_Q2_EARNING)
        self.assertEqual(parsed["forecastPeriod"], "2026年12月期(予)")
        self.assertEqual(parsed["forecastFiscalYear"], 2026)

    def test_a_stock_without_a_split_has_empty_split_fields(self) -> None:
        parsed = self.parse(
            {
                "disclosure_date": "2026-05-10",
                "quarter": 4,
                "fiscal_year_end": "2026-03-31",
                "forecast_dividend_per_share": 84.0,
                "interim_dividend_per_share": 40.0,
                "yearend_dividend_per_share": 44.0,
            },
            3,
        )
        self.assertEqual(parsed["forecastDividend"], 84)
        self.assertEqual(parsed["forecastInterimDividend"], 40)
        self.assertEqual(parsed["forecastFinalDividend"], 44)
        self.assertIsNone(parsed["forecastDividendAdjusted"])
        self.assertIsNone(parsed["forecastSplitFactor"])
        self.assertIsNone(parsed["forecastSplitEffectiveDate"])
        self.assertIsNone(parsed["forecastShareBasis"])

    def test_an_unreadable_effective_date_becomes_none(self) -> None:
        for raw in ("", "近日", "2026-13-01", "2026/10/01", None):
            with self.subTest(raw=raw):
                earning = dict(TOUKEI_Q2_EARNING)
                earning["forecast_split_effective_date"] = raw
                self.assertIsNone(
                    self.parse(earning)["forecastSplitEffectiveDate"]
                )

    def test_other_date_shapes_are_normalised_to_ten_chars(self) -> None:
        for raw in ("2026-10-01T00:00:00+09:00", "20261001", 20261001):
            with self.subTest(raw=raw):
                earning = dict(TOUKEI_Q2_EARNING)
                earning["forecast_split_effective_date"] = raw
                self.assertEqual(
                    self.parse(earning)["forecastSplitEffectiveDate"],
                    "2026-10-01",
                )


def feed(edinet_code: str, fiscal_month: int = 12) -> dict:
    return {"edinetCode": edinet_code, "fiscalMonth": fiscal_month}


class FetchLoopResilienceTest(unittest.TestCase):
    """1銘柄のAPI失敗で、その日の取得を全部捨てない・後続を止めない。"""

    CODES = ("4746", "9433", "8058", "2914", "7203")

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        base = Path(self.directory.name)
        self.state_path = base / "forecasts_state.json"
        self.edinet_dir = base / "edinet"
        self.edinet_dir.mkdir()
        for index, code in enumerate(self.CODES):
            (self.edinet_dir / f"{code}.json").write_text(
                json.dumps(feed(f"E{index:05d}")), encoding="utf-8"
            )
        self.fiscal_path = base / "fiscal_dividends.json"
        self.fiscal_path.write_text(
            json.dumps(
                {code: {"series": {"2025": 100.0}} for code in self.CODES}
            ),
            encoding="utf-8",
        )
        self.missing_calendar = base / "calendar_dividends_frozen.json"
        self.addCleanup(self.directory.cleanup)

    def argv(self) -> list[str]:
        return [
            "fetch_forecasts.py",
            "--fiscal-dividends",
            str(self.fiscal_path),
            "--calendar-dividends",
            str(self.missing_calendar),
            "--edinet-dir",
            str(self.edinet_dir),
            "--state",
            str(self.state_path),
            "--today",
            "2026-08-06",
        ]

    def run_main(self, fetch: mock.Mock, daily_limit: int = 95) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "EDINETDB_API_KEY": "test-key",
                "PRIORITY_CODES": "",
                "DVC_FORECAST_DAILY": str(daily_limit),
            },
        ), mock.patch.object(sys, "argv", self.argv()), mock.patch.object(
            fetch_forecasts, "load_dividend_yields", return_value={}
        ), mock.patch.object(
            fetch_forecasts, "fetch_one", fetch
        ):
            fetch_forecasts.main()

    def state(self) -> dict:
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    @staticmethod
    def parsed(value: float) -> dict:
        return {"forecastDividend": value, "forecastPeriod": "2026年12月期(予)"}

    def failing_fetch(self, *failures: str, status: int = 500) -> mock.Mock:
        def side_effect(candidate, api_key):
            if candidate.code in failures:
                raise fetch_forecasts.FetchError(
                    f"{candidate.code}: edinetdb HTTP {status}",
                    kind="http",
                    status=status,
                )
            return self.parsed(80.0), None

        return mock.Mock(side_effect=side_effect)

    def test_one_failure_does_not_discard_the_other_stocks(self) -> None:
        self.run_main(self.failing_fetch("8058"))
        stocks = self.state()["stocks"]
        self.assertEqual(len(stocks), len(self.CODES))
        for code in self.CODES:
            if code == "8058":
                continue
            self.assertEqual(stocks[code]["forecastDividend"], 80.0)
            self.assertEqual(stocks[code]["lastFetchedAt"], "2026-08-06")

    def test_a_failure_alone_is_not_a_non_zero_exit(self) -> None:
        """ここが非0だとワークフローが止まり、株価も利回りも更新されない。"""
        try:
            self.run_main(self.failing_fetch("8058", "2914"))
        except SystemExit as error:  # pragma: no cover - 失敗時の説明用
            self.fail(f"個別銘柄の失敗で終了コードが非0になった: {error}")

    def test_the_failure_is_recorded_without_losing_the_saved_value(
        self,
    ) -> None:
        self.state_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "queuePosition": 0,
                    "stocks": {
                        "8058": {
                            "forecastDividend": 60.0,
                            "lastFetchedAt": "2026-07-01",
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        self.run_main(self.failing_fetch("8058", status=503))
        record = self.state()["stocks"]["8058"]
        # 前回取れている予想は残す（消すと画面から予想が消える）
        self.assertEqual(record["forecastDividend"], 60.0)
        self.assertEqual(record["lastFetchedAt"], "2026-07-01")
        self.assertEqual(record["lastFailedAt"], "2026-08-06")
        self.assertEqual(record["lastFailureKind"], "http")
        self.assertEqual(record["failureCount"], 1)
        self.assertIn("503", record["lastFailureDetail"])

    def test_repeated_failures_increment_the_counter(self) -> None:
        self.run_main(self.failing_fetch("8058"))
        self.run_main(self.failing_fetch("8058"))
        self.assertEqual(self.state()["stocks"]["8058"]["failureCount"], 2)

    def test_a_success_clears_the_failure_marks(self) -> None:
        self.run_main(self.failing_fetch("8058"))
        self.run_main(self.failing_fetch())
        record = self.state()["stocks"]["8058"]
        self.assertEqual(record["forecastDividend"], 80.0)
        self.assertNotIn("lastFailureKind", record)
        self.assertNotIn("failureCount", record)

    def prime_all_as_already_fetched(self) -> None:
        """5銘柄すべてをdueでない状態にする（＝待ち行列の巡回対象）。"""
        self.state_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "queuePosition": 0,
                    "stocks": {
                        code: {"lastFetchedAt": "2026-08-06"}
                        for code in self.CODES
                    },
                }
            ),
            encoding="utf-8",
        )

    def test_the_queue_advances_even_when_a_stock_fails(self) -> None:
        self.prime_all_as_already_fetched()
        # 1日3件だけ処理する。3件目(7203)が失敗しても位置は3まで進む。
        # 進まないと翌日も同じ銘柄を叩き続け、待ち行列が止まる。
        self.run_main(self.failing_fetch("7203"), daily_limit=3)
        self.assertEqual(self.state()["queuePosition"], 3)

    def test_the_queue_advances_when_every_stock_fails(self) -> None:
        self.prime_all_as_already_fetched()
        self.run_main(self.failing_fetch(*self.CODES), daily_limit=3)
        self.assertEqual(self.state()["queuePosition"], 3)

    def test_progress_is_saved_during_the_loop(self) -> None:
        """全部終わってから1回だけ書くと、後半で落ちた日の分が丸ごと消える。"""
        original = fetch_forecasts.write_state
        saved_counts: list[int] = []

        def spy(path, state):
            saved_counts.append(len(state["stocks"]))
            original(path, state)

        with mock.patch.object(fetch_forecasts, "SAVE_INTERVAL", 2), \
                mock.patch.object(fetch_forecasts, "write_state", spy):
            self.run_main(self.failing_fetch())
        # 2件目・4件目の直後と、最後の1回
        self.assertEqual(saved_counts, [2, 4, 5])

    def test_an_authentication_failure_stops_with_a_non_zero_exit(self) -> None:
        for status in (401, 403):
            with self.subTest(status=status):
                fetch = self.failing_fetch(*self.CODES, status=status)
                with self.assertRaises(SystemExit) as caught:
                    self.run_main(fetch)
                self.assertNotIn(caught.exception.code, (0, None))
                # 全滅すると分かっている以上、残りは叩かない
                self.assertEqual(fetch.call_count, 1)

    def test_the_state_is_saved_before_a_fatal_exit(self) -> None:
        # 待ち行列はコード順なので、9433に当たるまでの分は取れている
        fetch = self.failing_fetch("9433", status=401)
        with self.assertRaises(SystemExit):
            self.run_main(fetch)
        self.assertTrue(self.state_path.exists())
        stocks = self.state()["stocks"]
        self.assertGreater(fetch.call_count, 1)
        # 叩いた分（成功も失敗も）がすべて書き出されている
        self.assertEqual(len(stocks), fetch.call_count)
        self.assertEqual(stocks["9433"]["lastFailureKind"], "http")

    def test_too_many_failures_in_a_row_is_treated_as_a_global_failure(
        self,
    ) -> None:
        with mock.patch.object(
            fetch_forecasts, "CONSECUTIVE_FAILURE_LIMIT", 3
        ):
            fetch = self.failing_fetch(*self.CODES)
            with self.assertRaises(SystemExit) as caught:
                self.run_main(fetch)
        self.assertNotIn(caught.exception.code, (0, None))
        self.assertEqual(fetch.call_count, 3)

    def test_a_success_resets_the_consecutive_counter(self) -> None:
        with mock.patch.object(
            fetch_forecasts, "CONSECUTIVE_FAILURE_LIMIT", 3
        ):
            # 待ち行列はコード順。2914・4746が失敗、7203で成功して連続が切れ、
            # 最後の9433がまた失敗する並び。合計4件失敗しても止まらない。
            fetch = self.failing_fetch("2914", "4746", "9433")
            self.run_main(fetch)
        self.assertEqual(fetch.call_count, len(self.CODES))
        self.assertEqual(len(self.state()["stocks"]), len(self.CODES))
        self.assertEqual(self.state()["stocks"]["7203"]["forecastDividend"], 80.0)

    def test_a_machine_readable_summary_line_is_printed(self) -> None:
        """CI側がこの行を見て、まとまった失敗を警告に出す。"""
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            self.run_main(self.failing_fetch("8058"))
        self.assertIn("summary selected=5 ok=4 failed=1", buffer.getvalue())

    def test_the_summary_line_has_no_thousands_separator(self) -> None:
        """桁区切りが入るとCI側の数値の取り出しが壊れる。"""
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            self.run_main(self.failing_fetch())
        summary = [
            line
            for line in buffer.getvalue().splitlines()
            if line.startswith("summary ")
        ]
        self.assertEqual(len(summary), 1)
        self.assertNotIn(",", summary[0])

    def test_a_broken_response_is_a_per_stock_failure(self) -> None:
        """不正JSON・想定外の形も1銘柄の失敗として扱う（全体は止めない）。"""

        def side_effect(candidate, api_key):
            if candidate.code == "8058":
                raise fetch_forecasts.FetchError(
                    "8058: edinetdb応答が不正なJSONです", kind="invalid_json"
                )
            return self.parsed(80.0), None

        self.run_main(mock.Mock(side_effect=side_effect))
        self.assertEqual(
            self.state()["stocks"]["8058"]["lastFailureKind"], "invalid_json"
        )
        self.assertEqual(len(self.state()["stocks"]), len(self.CODES))


class FetchOneErrorTypeTest(unittest.TestCase):
    """fetch_one が投げる例外の種別（ループ側の判断材料）。"""

    def candidate(self) -> fetch_forecasts.Candidate:
        return fetch_forecasts.Candidate(
            code="4746",
            fiscal_month=12,
            edinet_code="E05066",
            dividend_yield=8.46,
            priority_rank=None,
            event=None,
            last_fetched=None,
        )

    def test_http_errors_carry_the_status(self) -> None:
        with mock.patch.object(
            fetch_forecasts,
            "urlopen",
            side_effect=HTTPError("u", 429, "Too Many Requests", {}, None),
        ):
            with self.assertRaises(fetch_forecasts.FetchError) as caught:
                fetch_forecasts.fetch_one(self.candidate(), "key")
        self.assertEqual(caught.exception.status, 429)
        self.assertEqual(caught.exception.kind, "http")
        self.assertFalse(caught.exception.is_fatal)

    def test_authentication_errors_are_fatal(self) -> None:
        for status in (401, 403):
            with self.subTest(status=status):
                with mock.patch.object(
                    fetch_forecasts,
                    "urlopen",
                    side_effect=HTTPError("u", status, "no", {}, None),
                ):
                    with self.assertRaises(fetch_forecasts.FetchError) as caught:
                        fetch_forecasts.fetch_one(self.candidate(), "key")
                self.assertTrue(caught.exception.is_fatal)

    def test_a_timeout_is_a_network_failure(self) -> None:
        with mock.patch.object(
            fetch_forecasts, "urlopen", side_effect=TimeoutError()
        ):
            with self.assertRaises(fetch_forecasts.FetchError) as caught:
                fetch_forecasts.fetch_one(self.candidate(), "key")
        self.assertEqual(caught.exception.kind, "network")
        self.assertFalse(caught.exception.is_fatal)

    def test_an_unexpected_body_is_a_parse_failure(self) -> None:
        """200で返ってきても中身が想定外なら、その銘柄の失敗として扱う。"""

        class Response:
            headers = {"X-RateLimit-Remaining": "40"}

            def read(self, *args):
                return b'{"unexpected": true}'

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        with mock.patch.object(
            fetch_forecasts, "urlopen", return_value=Response()
        ):
            with self.assertRaises(fetch_forecasts.FetchError) as caught:
                fetch_forecasts.fetch_one(self.candidate(), "key")
        self.assertEqual(caught.exception.kind, "parse")
        self.assertIsNone(caught.exception.status)
        self.assertFalse(caught.exception.is_fatal)


if __name__ == "__main__":
    unittest.main()
