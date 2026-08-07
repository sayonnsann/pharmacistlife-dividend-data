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

    def run_main(
        self,
        fetch: mock.Mock,
        daily_limit: int = 95,
        events: tuple[list, int, int, int] = ([], 0, 0, 0),
        slots: str = "20",
    ) -> None:
        # load_dividend_yields と同じ理由で、イベントAPIも既定では叩かせない。
        # 「イベントが1件も無い日」＝これまでと同じ挙動になる。
        with mock.patch.dict(
            os.environ,
            {
                "EDINETDB_API_KEY": "test-key",
                "PRIORITY_CODES": "",
                "DVC_FORECAST_DAILY": str(daily_limit),
                "DVC_EVENT_SLOTS": slots,
            },
        ), mock.patch.object(sys, "argv", self.argv()), mock.patch.object(
            fetch_forecasts, "load_dividend_yields", return_value={}
        ), mock.patch.object(
            fetch_forecasts, "collect_events", return_value=events
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

    def test_consecutive_rate_limit_failures_stop_normally(self) -> None:
        """429だけは枠切れとして扱い、後続のDB構築を止めない。"""
        buffer = io.StringIO()
        with mock.patch.object(
            fetch_forecasts, "CONSECUTIVE_FAILURE_LIMIT", 3
        ), contextlib.redirect_stderr(buffer):
            fetch = self.failing_fetch(*self.CODES, status=429)
            self.run_main(fetch)
        self.assertEqual(fetch.call_count, 3)
        self.assertIn("取得枠を使い切ったため、本日はここまで", buffer.getvalue())
        self.assertEqual(
            self.state()["stocks"][self.CODES[0]]["lastFailureKind"], "http"
        )

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


# ---------------------------------------------------------------------------
# 開示イベント（/v1/events）
# ---------------------------------------------------------------------------

# 東計電算(4746)が2026-08-03に出したQ2決算短信。分割と大幅増配はこの中で
# 公表され、配当修正(dividend_revision)としては出ていない。近似発表日は
# 8/15なので、この決算短信を見ないと12日間気づけない。
TOUKEI_EVENT_RECORD = {
    "sec_code": "4746",
    "edinet_code": "E05066",
    "event_type": "earnings_summary",
    "event_date": "2026-08-03",
    "event_category": "earnings",
    "severity": "high",
    "filer_name": "株式会社東計電算",
    "title": "2026年12月期 第2四半期決算短信",
    "metadata": {
        "dividend_direction": "increase",
        "revision_direction": None,
        "is_correction": False,
        "is_earnings": True,
        "buyback_phase": None,
        "disclosure_time": "15:00",
    },
    "detected_at": "2026-08-03T15:12:00+09:00",
}


def disclosure(
    code: str,
    event_type: str,
    day: str,
    *,
    is_earnings: bool = True,
    dividend_signal: bool = False,
    edinet_code: str = "",
    event_id: str | None = None,
) -> "fetch_forecasts.DisclosureEvent":
    return fetch_forecasts.DisclosureEvent(
        event_id=event_id or f"{event_type}:{code}:{day}",
        event_type=event_type,
        event_date=fetch_forecasts.date.fromisoformat(day),
        sec_code=code,
        edinet_code=edinet_code,
        is_earnings=is_earnings,
        has_dividend_signal=dividend_signal,
    )


def candidate(
    code: str,
    *,
    edinet_code: str = "",
    dividend_yield: float = 0.0,
    priority_rank: int | None = None,
    last_fetched: str | None = None,
) -> "fetch_forecasts.Candidate":
    return fetch_forecasts.Candidate(
        code=code,
        fiscal_month=12,
        edinet_code=edinet_code or f"E9{code[:4]}",
        dividend_yield=dividend_yield,
        priority_rank=priority_rank,
        event=None,
        last_fetched=(
            fetch_forecasts.date.fromisoformat(last_fetched)
            if last_fetched
            else None
        ),
    )


class ParseEventRecordTest(unittest.TestCase):
    def test_reads_the_real_response_shape(self) -> None:
        event = fetch_forecasts.parse_event_record(TOUKEI_EVENT_RECORD)
        assert event is not None
        self.assertEqual(event.event_type, "earnings_summary")
        self.assertEqual(event.sec_code, "4746")
        self.assertEqual(event.edinet_code, "E05066")
        self.assertEqual(event.event_date.isoformat(), "2026-08-03")
        self.assertTrue(event.is_earnings)
        self.assertTrue(event.has_dividend_signal)

    def test_the_id_is_stable_across_runs(self) -> None:
        """応答にIDが無いので自分で組む。日をまたいでも同じ値になること。"""
        first = fetch_forecasts.parse_event_record(TOUKEI_EVENT_RECORD)
        second = fetch_forecasts.parse_event_record(dict(TOUKEI_EVENT_RECORD))
        assert first is not None and second is not None
        self.assertEqual(first.event_id, second.event_id)
        self.assertEqual(first.event_id, "earnings_summary:4746:2026-08-03")

    def test_an_explicit_id_wins(self) -> None:
        record = dict(TOUKEI_EVENT_RECORD, event_id="evt_12345")
        event = fetch_forecasts.parse_event_record(record)
        assert event is not None
        self.assertEqual(event.event_id, "evt_12345")

    def test_types_we_do_not_act_on_are_dropped(self) -> None:
        # merger や buyback は実在する種別だが、配当予想の取り直しには
        # つながらないので取りに行かないし、来ても捨てる。
        for event_type in ("merger", "buyback", "subsidiary_status_change", ""):
            with self.subTest(event_type=event_type):
                record = dict(TOUKEI_EVENT_RECORD, event_type=event_type)
                self.assertIsNone(fetch_forecasts.parse_event_record(record))

    def test_records_we_cannot_place_are_dropped(self) -> None:
        for broken in (
            {"event_date": None},
            {"event_date": "近日"},
            {"sec_code": "", "edinet_code": ""},
        ):
            with self.subTest(broken=broken):
                record = dict(TOUKEI_EVENT_RECORD, **broken)
                self.assertIsNone(fetch_forecasts.parse_event_record(record))
        self.assertIsNone(fetch_forecasts.parse_event_record("文字列"))

    def test_a_missing_metadata_block_is_not_a_crash(self) -> None:
        record = dict(TOUKEI_EVENT_RECORD)
        record.pop("metadata")
        event = fetch_forecasts.parse_event_record(record)
        assert event is not None
        self.assertFalse(event.is_earnings)
        self.assertFalse(event.has_dividend_signal)

    def test_a_flat_dividend_direction_is_not_a_signal(self) -> None:
        for raw in (None, "", "none", "unchanged", "FLAT"):
            with self.subTest(raw=raw):
                record = dict(
                    TOUKEI_EVENT_RECORD,
                    metadata={"is_earnings": True, "dividend_direction": raw},
                )
                event = fetch_forecasts.parse_event_record(record)
                assert event is not None
                self.assertFalse(event.has_dividend_signal)


class EventWindowTest(unittest.TestCase):
    TODAY = fetch_forecasts.date(2026, 8, 6)

    def test_the_first_run_looks_back_a_few_days(self) -> None:
        since, until = fetch_forecasts.event_window({}, self.TODAY)
        self.assertEqual(since.isoformat(), "2026-08-03")
        self.assertEqual(until, self.TODAY)

    def test_a_recent_check_still_overlaps(self) -> None:
        """昨日見ていても数日は重ねて見る（枠に入らず持ち越した分のため）。"""
        since, _ = fetch_forecasts.event_window(
            {"lastCheckedAt": "2026-08-05"}, self.TODAY
        )
        self.assertEqual(since.isoformat(), "2026-08-03")

    def test_a_gap_is_covered(self) -> None:
        since, _ = fetch_forecasts.event_window(
            {"lastCheckedAt": "2026-07-30"}, self.TODAY
        )
        self.assertEqual(since.isoformat(), "2026-07-30")

    def test_a_long_outage_is_clamped(self) -> None:
        """半年止まっていても遡りすぎない（枠を食い潰さないため）。"""
        since, _ = fetch_forecasts.event_window(
            {"lastCheckedAt": "2026-01-05"}, self.TODAY
        )
        self.assertEqual(since.isoformat(), "2026-07-23")

    def test_an_unreadable_record_falls_back_to_the_default(self) -> None:
        for raw in ("", "きのう", 20260805, None):
            with self.subTest(raw=raw):
                since, _ = fetch_forecasts.event_window(
                    {"lastCheckedAt": raw}, self.TODAY
                )
                self.assertEqual(since.isoformat(), "2026-08-03")


class FakeResponse:
    def __init__(self, payload: dict, remaining: str = "80") -> None:
        self.payload = json.dumps(payload).encode("utf-8")
        self.headers = {"X-RateLimit-Remaining": remaining}

    def read(self, *args):
        return self.payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def events_payload(records: list[dict], total: int, next_offset: int | None) -> dict:
    return {
        "data": records,
        "meta": {
            "pagination": {
                "total": total,
                "limit": 100,
                "offset": 0,
                "next_offset": next_offset,
            }
        },
    }


class CollectEventsTest(unittest.TestCase):
    """/v1/events の呼び方（1日のリクエスト数と打ち切りの扱い）。"""

    SINCE = fetch_forecasts.date(2026, 8, 3)
    UNTIL = fetch_forecasts.date(2026, 8, 6)

    def record(self, code: str, event_type: str) -> dict:
        return dict(
            TOUKEI_EVENT_RECORD,
            sec_code=code,
            event_type=event_type,
            event_date="2026-08-05",
        )

    def run_collect(self, responder) -> tuple[list, int, int, str]:
        self.urls: list[str] = []

        def urlopen(request, timeout=None):
            self.urls.append(request.full_url)
            return FakeResponse(responder(request.full_url))

        buffer = io.StringIO()
        with mock.patch.object(
            fetch_forecasts, "urlopen", side_effect=urlopen
        ), contextlib.redirect_stdout(buffer):
            events, requests_used, truncated, ok_pages = (
                fetch_forecasts.collect_events(self.SINCE, self.UNTIL, "test-key")
            )
        self.ok_pages = ok_pages
        return events, requests_used, truncated, buffer.getvalue()

    def test_a_quiet_day_costs_one_request_per_type(self) -> None:
        events, requests_used, truncated, _ = self.run_collect(
            lambda url: events_payload([self.record("4746", "dividend_revision")], 1, None)
        )
        self.assertEqual(requests_used, len(fetch_forecasts.EVENT_SOURCES))
        self.assertEqual(requests_used, 4)
        self.assertEqual(truncated, 0)
        self.assertEqual(len(events), 4)

    def test_every_declared_type_is_asked_for(self) -> None:
        self.run_collect(lambda url: events_payload([], 0, None))
        asked = {
            url.split("event_type=")[1].split("&")[0] for url in self.urls
        }
        self.assertEqual(
            asked,
            {
                "dividend_revision",
                "stock_split",
                "reverse_split",
                "earnings_summary",
            },
        )
        for url in self.urls:
            self.assertIn("since=2026-08-03", url)
            self.assertIn("until=2026-08-06", url)
            self.assertIn("limit=100", url)

    def test_a_busy_day_is_truncated_and_says_so(self) -> None:
        """繁忙期の決算短信は1日780件。全部は追えないので打ち切る。"""

        def responder(url: str) -> dict:
            if "event_type=earnings_summary" in url:
                offset = int(url.split("offset=")[1].split("&")[0])
                return events_payload(
                    [self.record(f"{4000 + i}", "earnings_summary") for i in range(100)],
                    780,
                    offset + 100,
                )
            return events_payload([], 0, None)

        events, requests_used, truncated, log = self.run_collect(responder)
        # 決算短信は2ページ(200件)で打ち切り、残り580件はログに出す。
        self.assertEqual(truncated, 780 - 200)
        self.assertEqual(len(events), 200)
        self.assertEqual(requests_used, 5)
        self.assertIn("580件は上限2ページで打ち切り", log)
        self.assertIn("全780件", log)

    def test_pagination_stops_when_the_server_says_there_is_no_more(self) -> None:
        def responder(url: str) -> dict:
            if "event_type=dividend_revision" in url:
                return events_payload(
                    [self.record("4746", "dividend_revision")], 1, None
                )
            return events_payload([], 0, None)

        _, requests_used, truncated, _ = self.run_collect(responder)
        self.assertEqual(requests_used, 4)
        self.assertEqual(truncated, 0)

    def test_one_broken_type_does_not_lose_the_others(self) -> None:
        """決算短信が落ちても、配当修正だけは拾えるようにする。"""

        def urlopen(request, timeout=None):
            if "event_type=earnings_summary" in request.full_url:
                raise HTTPError(request.full_url, 500, "boom", {}, None)
            return FakeResponse(
                events_payload([self.record("4746", "dividend_revision")], 1, None)
            )

        with mock.patch.object(
            fetch_forecasts, "urlopen", side_effect=urlopen
        ), contextlib.redirect_stdout(io.StringIO()):
            events, requests_used, _, ok_pages = fetch_forecasts.collect_events(
                self.SINCE, self.UNTIL, "test-key"
            )
        self.assertEqual(len(events), 3)
        self.assertEqual(ok_pages, 3)
        # 失敗した1回もサーバー側の枠は消費している前提で数える
        self.assertEqual(requests_used, 4)

    def test_an_unexpected_body_is_not_an_exception(self) -> None:
        with mock.patch.object(
            fetch_forecasts,
            "urlopen",
            return_value=FakeResponse({"unexpected": True}),
        ), contextlib.redirect_stdout(io.StringIO()):
            events, requests_used, _, ok_pages = fetch_forecasts.collect_events(
                self.SINCE, self.UNTIL, "test-key"
            )
        self.assertEqual(events, [])
        # 応答は壊れていたので「見た」とは数えない
        self.assertEqual(ok_pages, 0)
        self.assertEqual(requests_used, 4)


class PlanEventSlotsTest(unittest.TestCase):
    CANDIDATES = [
        candidate("4746", edinet_code="E05066", last_fetched="2026-07-28"),
        candidate("9433", edinet_code="E04425", last_fetched="2026-07-28"),
        candidate("8058", edinet_code="E02528", last_fetched="2026-07-28"),
        candidate("2914", edinet_code="E00413", last_fetched="2026-07-28"),
    ]

    def plan(self, events, seen=None, slots=20):
        return fetch_forecasts.plan_event_slots(
            events, self.CANDIDATES, seen if seen is not None else {}, slots
        )

    def test_the_dividend_revision_comes_first(self) -> None:
        picks, _, _ = self.plan(
            [
                disclosure("4746", "earnings_summary", "2026-08-05"),
                disclosure("9433", "stock_split", "2026-08-04"),
                disclosure("8058", "dividend_revision", "2026-08-03"),
            ]
        )
        self.assertEqual([pick.code for pick in picks], ["8058", "9433", "4746"])

    def test_an_earnings_summary_that_touches_the_dividend_ranks_higher(
        self,
    ) -> None:
        picks, _, _ = self.plan(
            [
                disclosure("4746", "earnings_summary", "2026-08-05"),
                disclosure(
                    "9433",
                    "earnings_summary",
                    "2026-08-04",
                    dividend_signal=True,
                ),
            ]
        )
        self.assertEqual([pick.code for pick in picks], ["9433", "4746"])

    def test_a_company_uses_one_slot_even_with_several_events(self) -> None:
        """枠は会社数で数える。同じ銘柄を1日に2回叩いても意味がない。"""
        picks, ids, stats = self.plan(
            [
                disclosure("4746", "dividend_revision", "2026-08-03"),
                disclosure("4746", "stock_split", "2026-08-03"),
                disclosure("4746", "earnings_summary", "2026-08-03"),
            ]
        )
        self.assertEqual([pick.code for pick in picks], ["4746"])
        self.assertEqual(stats["companies"], 1)
        # 取れたら3件ともまとめて「見た」にする。
        self.assertEqual(len(ids["4746"]), 3)

    def test_the_slot_count_is_the_cap(self) -> None:
        picks, ids, stats = self.plan(
            [
                disclosure(code, "dividend_revision", "2026-08-03")
                for code in ("4746", "9433", "8058", "2914")
            ],
            slots=2,
        )
        self.assertEqual(len(picks), 2)
        self.assertEqual(stats["companies"], 4)
        # 枠から溢れた銘柄は「見た」にしない（翌日また拾う）。
        self.assertEqual(set(ids), {pick.code for pick in picks})

    def test_slots_of_zero_picks_nothing(self) -> None:
        picks, ids, _ = self.plan(
            [disclosure("4746", "dividend_revision", "2026-08-03")], slots=0
        )
        self.assertEqual(picks, [])
        self.assertEqual(ids, {})

    def test_an_event_we_already_acted_on_is_skipped(self) -> None:
        event = disclosure("4746", "dividend_revision", "2026-08-03")
        picks, _, stats = self.plan([event], seen={event.event_id: "2026-08-03"})
        self.assertEqual(picks, [])
        self.assertEqual(stats["already_seen"], 1)

    def test_a_stock_fetched_after_the_disclosure_is_skipped(self) -> None:
        picks, _, stats = self.plan(
            [disclosure("4746", "dividend_revision", "2026-07-20")]
        )
        self.assertEqual(picks, [])
        self.assertEqual(stats["already_fetched"], 1)

    def test_a_disclosure_on_the_fetch_day_is_still_retried(self) -> None:
        """朝7時の取得より後に出た開示を取りこぼさないよう、同日は取り直す。"""
        picks, _, _ = self.plan(
            [disclosure("4746", "dividend_revision", "2026-07-28")]
        )
        self.assertEqual([pick.code for pick in picks], ["4746"])

    def test_an_earnings_summary_that_is_not_an_earnings_release_is_skipped(
        self,
    ) -> None:
        picks, _, stats = self.plan(
            [
                disclosure(
                    "4746", "earnings_summary", "2026-08-03", is_earnings=False
                )
            ]
        )
        self.assertEqual(picks, [])
        self.assertEqual(stats["not_earnings"], 1)

    def test_a_stock_outside_the_queue_is_ignored(self) -> None:
        picks, _, stats = self.plan(
            [disclosure("1234", "dividend_revision", "2026-08-03")]
        )
        self.assertEqual(picks, [])
        self.assertEqual(stats["unmatched"], 1)

    def test_a_five_digit_code_still_matches(self) -> None:
        picks, _, _ = self.plan(
            [disclosure("47460", "dividend_revision", "2026-08-03")]
        )
        self.assertEqual([pick.code for pick in picks], ["4746"])

    def test_the_edinet_code_is_the_fallback(self) -> None:
        picks, _, _ = self.plan(
            [
                disclosure(
                    "", "dividend_revision", "2026-08-03", edinet_code="E05066"
                )
            ]
        )
        self.assertEqual([pick.code for pick in picks], ["4746"])

    def test_watched_stocks_win_a_tie(self) -> None:
        watched = [
            candidate("4746", last_fetched="2026-07-28"),
            candidate("9433", last_fetched="2026-07-28", priority_rank=0),
        ]
        picks, _, _ = fetch_forecasts.plan_event_slots(
            [
                disclosure("4746", "dividend_revision", "2026-08-03"),
                disclosure("9433", "dividend_revision", "2026-08-03"),
            ],
            watched,
            {},
            20,
        )
        self.assertEqual([pick.code for pick in picks], ["9433", "4746"])

    def test_a_pending_event_beats_a_new_event(self) -> None:
        old = disclosure("4746", "dividend_revision", "2026-08-03")
        fresh = disclosure("9433", "dividend_revision", "2026-08-05")
        picks, _, _ = fetch_forecasts.plan_event_slots(
            [fresh, old], self.CANDIDATES, {}, 1, {old.event_id}
        )
        self.assertEqual([pick.code for pick in picks], ["4746"])


class PruneSeenTest(unittest.TestCase):
    TODAY = fetch_forecasts.date(2026, 8, 6)

    def test_events_outside_the_window_are_dropped(self) -> None:
        seen = {
            "old": "2026-07-01",
            "edge": "2026-07-23",
            "fresh": "2026-08-05",
            "broken": 12345,
        }
        fetch_forecasts.prune_seen(seen, self.TODAY)
        self.assertEqual(set(seen), {"edge", "fresh"})

    def test_the_size_is_capped(self) -> None:
        seen = {f"e{index}": "2026-08-05" for index in range(10)}
        seen["newest"] = "2026-08-06"
        with mock.patch.object(fetch_forecasts, "EVENT_SEEN_LIMIT", 3):
            fetch_forecasts.prune_seen(seen, self.TODAY)
        self.assertEqual(len(seen), 3)
        self.assertIn("newest", seen)


class PendingEventTest(unittest.TestCase):
    TODAY = fetch_forecasts.date(2026, 8, 20)

    def test_pending_event_is_discarded_after_the_attempt_limit_with_a_log(
        self,
    ) -> None:
        event = disclosure("4746", "dividend_revision", "2026-08-03")
        block = {
            "seen": {},
            "pending": {
                event.event_id: fetch_forecasts.pending_record(
                    event, fetch_forecasts.date(2026, 8, 6), attempts=5
                )
            },
        }
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            pending = fetch_forecasts.load_pending_events(block, self.TODAY)
        self.assertEqual(pending, {})
        self.assertEqual(block["pending"], {})
        self.assertIn("イベント持ち越しを諦めました", output.getvalue())


class EventDrivenQueueTest(unittest.TestCase):
    """イベント枠が実際の待ち行列にどう効くか（main全体）。"""

    CODES = ("2914", "4746", "7203", "8058", "9433")
    # 全銘柄をこの日に取得済みにしておく。近似発表日(12月決算のQ1=5/15)より
    # 後なので due ではなく、通常の巡回対象になる。
    PRIMED = "2026-07-28"

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
            json.dumps({code: {"series": {"2025": 100.0}} for code in self.CODES}),
            encoding="utf-8",
        )
        self.missing_calendar = base / "calendar_dividends_frozen.json"
        self.addCleanup(self.directory.cleanup)
        self.write_state(
            {
                "version": 1,
                "queuePosition": 0,
                "stocks": {
                    code: {"lastFetchedAt": self.PRIMED} for code in self.CODES
                },
            }
        )

    def write_state(self, document: dict) -> None:
        self.state_path.write_text(
            json.dumps(document, ensure_ascii=False), encoding="utf-8"
        )

    def state(self) -> dict:
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def argv(self, today: str = "2026-08-06") -> list[str]:
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
            today,
        ]

    def run_main(
        self,
        events: list,
        *,
        daily_limit: int = 95,
        requests_used: int = 4,
        ok_pages: int = 4,
        fetch: mock.Mock | None = None,
        slots: str = "20",
        collect: mock.Mock | None = None,
        today: str = "2026-08-06",
    ) -> mock.Mock:
        fetch = fetch or mock.Mock(
            return_value=({"forecastDividend": 97.5}, None)
        )
        collect = collect or mock.Mock(
            return_value=(events, requests_used, 0, ok_pages)
        )
        self.collect = collect
        with mock.patch.dict(
            os.environ,
            {
                "EDINETDB_API_KEY": "test-key",
                "PRIORITY_CODES": "",
                "DVC_FORECAST_DAILY": str(daily_limit),
                "DVC_EVENT_SLOTS": slots,
            },
        ), mock.patch.object(sys, "argv", self.argv(today)), mock.patch.object(
            fetch_forecasts, "load_dividend_yields", return_value={}
        ), mock.patch.object(
            fetch_forecasts, "collect_events", collect
        ), mock.patch.object(
            fetch_forecasts, "fetch_one", fetch
        ), contextlib.redirect_stdout(io.StringIO()):
            fetch_forecasts.main()
        return fetch

    @staticmethod
    def fetched_codes(fetch: mock.Mock) -> list[str]:
        return [call.args[0].code for call in fetch.call_args_list]

    def test_the_toukei_case_is_picked_up_the_next_morning(self) -> None:
        """東計電算は8/3にQ2決算短信で増配と分割を公表。近似日は8/15。

        1日1件しか取れない状況でも、イベント枠がある限り翌朝の実行で
        4746が最初に取り直される。
        """
        fetch = self.run_main(
            [
                fetch_forecasts.parse_event_record(TOUKEI_EVENT_RECORD),
            ],
            daily_limit=5,
            requests_used=4,
        )
        self.assertEqual(self.fetched_codes(fetch)[0], "4746")

    def test_without_the_event_the_same_day_starts_elsewhere(self) -> None:
        """比較用: イベントが無ければ4746は巡回の順番待ちになる。"""
        fetch = self.run_main([], daily_limit=5, requests_used=4)
        self.assertEqual(self.fetched_codes(fetch)[0], "2914")

    def test_the_event_slot_is_filled_before_the_rotation(self) -> None:
        fetch = self.run_main(
            [disclosure("9433", "dividend_revision", "2026-08-03")],
            daily_limit=7,
            requests_used=4,
        )
        codes = self.fetched_codes(fetch)
        self.assertEqual(codes[0], "9433")
        # 残りは従来どおりの順（9433はイベント枠で取ったので出てこない）
        self.assertEqual(codes[1:], ["2914", "4746"])

    def test_the_event_requests_come_out_of_the_daily_budget(self) -> None:
        """イベント取得も同じ1日100件の枠を使う。使った分だけ予想を減らす。"""
        fetch = self.run_main([], daily_limit=8, requests_used=5)
        self.assertEqual(fetch.call_count, 3)

    def test_the_budget_never_goes_negative(self) -> None:
        fetch = self.run_main([], daily_limit=3, requests_used=9)
        self.assertEqual(fetch.call_count, 0)

    def test_an_event_pick_does_not_advance_the_rotation(self) -> None:
        """イベント枠は巡回の外から差し込む。位置を進めると別の銘柄が飛ぶ。"""
        fetch = self.run_main(
            [disclosure("9433", "dividend_revision", "2026-08-03")],
            daily_limit=7,
            requests_used=4,
        )
        self.assertEqual(fetch.call_count, 3)
        # 巡回から取ったのは2件だけなので位置は2（3ではない）
        self.assertEqual(self.state()["queuePosition"], 2)

    def test_a_successful_event_fetch_is_remembered(self) -> None:
        event = disclosure("9433", "dividend_revision", "2026-08-03")
        self.run_main([event], daily_limit=5)
        seen = self.state()["events"]["seen"]
        self.assertEqual(seen[event.event_id], "2026-08-03")
        self.assertEqual(
            self.state()["stocks"]["9433"]["lastEventAt"], "2026-08-03"
        )

    def test_a_failed_event_fetch_is_retried_tomorrow(self) -> None:
        event = disclosure("9433", "dividend_revision", "2026-08-03")
        failing = mock.Mock(
            side_effect=fetch_forecasts.FetchError(
                "9433: edinetdb HTTP 503", kind="http", status=503
            )
        )
        self.run_main([event], daily_limit=1, fetch=failing)
        # 取れていないので「見た」にはしない
        self.assertEqual(self.state()["events"]["seen"], {})

    def test_a_failed_event_is_retried_after_it_leaves_the_search_window(
        self,
    ) -> None:
        """検索窓から消えた後もstateの持ち越しから優先して拾う。"""
        event = disclosure("9433", "dividend_revision", "2026-08-03")
        failing = mock.Mock(
            side_effect=fetch_forecasts.FetchError(
                "9433: edinetdb HTTP 503", kind="http", status=503
            )
        )
        self.run_main([event], daily_limit=5, fetch=failing)
        self.assertIn(event.event_id, self.state()["events"]["pending"])

        # 8/20には8/3のイベントはAPIの検索窓外（持ち越しの上限日内）。
        fetch = self.run_main([], daily_limit=5, today="2026-08-20")
        self.assertEqual(self.fetched_codes(fetch)[0], "9433")
        self.assertNotIn(event.event_id, self.state()["events"]["pending"])
        self.assertEqual(
            self.state()["stocks"]["9433"]["lastEventAt"], "2026-08-03"
        )

    def test_the_window_start_is_remembered(self) -> None:
        self.run_main([], daily_limit=5)
        self.assertEqual(
            self.state()["events"]["lastCheckedAt"], "2026-08-06"
        )

    def test_a_day_where_no_request_landed_is_not_recorded_as_checked(
        self,
    ) -> None:
        """全滅した日に記録を進めると、その日の開示を二度と見ない。

        リクエストは4回送っている（枠は減っている）が、どれも応答が
        得られなかった場合。枠の消費と「見たかどうか」は別に数える。
        """
        fetch = self.run_main(
            [], daily_limit=9, requests_used=4, ok_pages=0
        )
        block = self.state()["events"]
        self.assertIsNone(block["lastCheckedAt"])
        self.assertIn("lastError", block)
        # 失敗しても枠は減っているので、予想取得は 9-4=5 件に絞られる
        self.assertEqual(fetch.call_count, 5)

    def test_the_event_api_failing_does_not_stop_the_daily_update(self) -> None:
        """イベントが取れなくても、株価と利回りの更新まで止めない。"""
        collect = mock.Mock(side_effect=RuntimeError("events down"))
        fetch = self.run_main([], daily_limit=5, collect=collect)
        self.assertEqual(fetch.call_count, 5)
        self.assertIn("lastError", self.state()["events"])

    def test_the_kill_switch_skips_the_event_api_entirely(self) -> None:
        collect = mock.Mock()
        fetch = self.run_main([], daily_limit=5, slots="0", collect=collect)
        collect.assert_not_called()
        # 枠を丸ごと予想取得に回す
        self.assertEqual(fetch.call_count, 5)

    def test_an_old_state_file_without_events_still_works(self) -> None:
        """本番で動いている state は events を持たない。versionは上げない。"""
        self.write_state(
            {
                "version": 1,
                "queuePosition": 1,
                "stocks": {"4746": {"lastFetchedAt": self.PRIMED}},
            }
        )
        self.run_main([], daily_limit=9, requests_used=4)
        document = self.state()
        self.assertEqual(document["version"], 1)
        self.assertEqual(len(document["stocks"]), len(self.CODES))
        self.assertEqual(document["events"]["lastCheckedAt"], "2026-08-06")

    def test_a_corrupt_events_block_is_rebuilt(self) -> None:
        self.write_state(
            {
                "version": 1,
                "queuePosition": 0,
                "stocks": {
                    code: {"lastFetchedAt": self.PRIMED} for code in self.CODES
                },
                "events": "壊れている",
            }
        )
        self.run_main([], daily_limit=5)
        self.assertEqual(self.state()["events"]["seen"], {})

    def test_old_seen_entries_are_dropped_when_saving(self) -> None:
        self.write_state(
            {
                "version": 1,
                "queuePosition": 0,
                "stocks": {
                    code: {"lastFetchedAt": self.PRIMED} for code in self.CODES
                },
                "events": {
                    "lastCheckedAt": "2026-08-05",
                    "lastEventDate": None,
                    "seen": {"ancient": "2025-01-01", "recent": "2026-08-05"},
                },
            }
        )
        self.run_main([], daily_limit=5)
        self.assertEqual(set(self.state()["events"]["seen"]), {"recent"})

    def test_the_summary_line_reports_the_event_usage(self) -> None:
        buffer = io.StringIO()
        collect = mock.Mock(
            return_value=(
                [disclosure("9433", "dividend_revision", "2026-08-03")],
                5,
                0,
                5,
            )
        )
        fetch = mock.Mock(return_value=({"forecastDividend": 97.5}, None))
        with mock.patch.dict(
            os.environ,
            {
                "EDINETDB_API_KEY": "test-key",
                "PRIORITY_CODES": "",
                "DVC_FORECAST_DAILY": "10",
                "DVC_EVENT_SLOTS": "20",
            },
        ), mock.patch.object(sys, "argv", self.argv()), mock.patch.object(
            fetch_forecasts, "load_dividend_yields", return_value={}
        ), mock.patch.object(
            fetch_forecasts, "collect_events", collect
        ), mock.patch.object(
            fetch_forecasts, "fetch_one", fetch
        ), contextlib.redirect_stdout(buffer):
            fetch_forecasts.main()
        summary = [
            line
            for line in buffer.getvalue().splitlines()
            if line.startswith("summary ")
        ]
        self.assertEqual(len(summary), 1)
        self.assertIn("eventPicks=1", summary[0])
        self.assertIn("eventRequests=5", summary[0])
        self.assertNotIn(",", summary[0])


class EndToEndEventTest(unittest.TestCase):
    """HTTPの境界だけを差し替えて、イベント検知から予想の保存までを通す。

    他のテストは collect_events を差し替えているので、URLの組み立て・
    /v1/events の応答の読み取り・優先枠・予想の保存がひと続きに動くことは
    ここでしか確かめられない。
    """

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        base = Path(self.directory.name)
        self.edinet_dir = base / "edinet"
        self.edinet_dir.mkdir()
        for code, edinet_code in (("4746", "E05066"), ("9433", "E04425")):
            (self.edinet_dir / f"{code}.json").write_text(
                json.dumps(feed(edinet_code)), encoding="utf-8"
            )
        self.fiscal_path = base / "fiscal.json"
        self.fiscal_path.write_text(
            json.dumps(
                {
                    "4746": {"series": {"2025": 173.0}},
                    "9433": {"series": {"2025": 200.0}},
                }
            ),
            encoding="utf-8",
        )
        self.state_path = base / "state.json"
        # 7/28に取得済み＝画面には古い173円が出ている状態
        self.state_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "queuePosition": 0,
                    "stocks": {
                        code: {
                            "forecastDividend": 173.0,
                            "lastFetchedAt": "2026-07-28",
                        }
                        for code in ("4746", "9433")
                    },
                }
            ),
            encoding="utf-8",
        )
        self.missing_calendar = base / "missing.json"
        self.addCleanup(self.directory.cleanup)

    def urlopen(self, request, timeout=None):
        url = request.full_url
        self.seen_urls.append(url)
        if url.startswith(fetch_forecasts.EDINETDB_EVENTS_URL):
            if "event_type=earnings_summary" in url:
                return FakeResponse(events_payload([TOUKEI_EVENT_RECORD], 1, None))
            return FakeResponse(events_payload([], 0, None))
        if "E05066" in url:
            return FakeResponse({"earnings": [TOUKEI_Q2_EARNING]})
        return FakeResponse({"earnings": [TOUKEI_Q1_EARNING]})

    def test_the_split_and_raise_reach_the_state_file(self) -> None:
        self.seen_urls: list[str] = []
        argv = [
            "fetch_forecasts.py",
            "--fiscal-dividends", str(self.fiscal_path),
            "--calendar-dividends", str(self.missing_calendar),
            "--edinet-dir", str(self.edinet_dir),
            "--state", str(self.state_path),
            "--today", "2026-08-04",
        ]
        with mock.patch.dict(
            os.environ,
            {
                "EDINETDB_API_KEY": "test-key",
                "PRIORITY_CODES": "",
                # イベント4リクエスト＋予想1件だけ取れる枠にして、
                # 「1件しか取れないならどれを取るか」を見る
                "DVC_FORECAST_DAILY": "5",
                "DVC_EVENT_SLOTS": "20",
            },
        ), mock.patch.object(sys, "argv", argv), mock.patch.object(
            fetch_forecasts, "load_dividend_yields", return_value={}
        ), mock.patch.object(
            fetch_forecasts, "urlopen", side_effect=self.urlopen
        ), contextlib.redirect_stdout(io.StringIO()):
            fetch_forecasts.main()

        document = json.loads(self.state_path.read_text(encoding="utf-8"))
        record = document["stocks"]["4746"]
        # 8/3の決算短信の中身（分割後97.5円・分割前換算119.125円）が入る
        self.assertEqual(record["forecastDividend"], 97.5)
        self.assertEqual(record["forecastDividendAdjusted"], 119.125)
        self.assertEqual(record["forecastSplitFactor"], 4)
        self.assertEqual(record["forecastSplitEffectiveDate"], "2026-10-01")
        self.assertEqual(record["lastFetchedAt"], "2026-08-04")
        self.assertEqual(record["lastEventAt"], "2026-08-03")
        # 9433は枠が無いので古いまま（＝4746が先に取られた証拠）
        self.assertEqual(document["stocks"]["9433"]["forecastDividend"], 173.0)
        # 同じ開示で翌日また枠を使わないよう、処理済みとして残る
        self.assertIn(
            "earnings_summary:4746:2026-08-03", document["events"]["seen"]
        )
        self.assertEqual(document["events"]["lastCheckedAt"], "2026-08-04")
        # イベント4回＋予想1回 = 5リクエスト（1日の枠と一致）
        self.assertEqual(len(self.seen_urls), 5)

    def run_day(self, today: str) -> str:
        argv = [
            "fetch_forecasts.py",
            "--fiscal-dividends", str(self.fiscal_path),
            "--calendar-dividends", str(self.missing_calendar),
            "--edinet-dir", str(self.edinet_dir),
            "--state", str(self.state_path),
            "--today", today,
        ]
        buffer = io.StringIO()
        with mock.patch.dict(
            os.environ,
            {
                "EDINETDB_API_KEY": "test-key",
                "PRIORITY_CODES": "",
                "DVC_FORECAST_DAILY": "5",
                "DVC_EVENT_SLOTS": "20",
            },
        ), mock.patch.object(sys, "argv", argv), mock.patch.object(
            fetch_forecasts, "load_dividend_yields", return_value={}
        ), mock.patch.object(
            fetch_forecasts, "urlopen", side_effect=self.urlopen
        ), contextlib.redirect_stdout(buffer):
            fetch_forecasts.main()
        return buffer.getvalue()

    def test_the_same_disclosure_does_not_take_a_slot_twice(self) -> None:
        """同じ開示は翌日以降イベント枠を取らない（毎日叩き直さない）。

        問い合わせ期間は必ず3日重ねるので、翌日も同じイベントが応答に
        入ってくる。処理済みとして覚えていないと、同じ銘柄で枠を使い続ける。
        """
        self.seen_urls = []
        first = self.run_day("2026-08-04")
        second = self.run_day("2026-08-05")
        self.assertIn("eventPicks=1", first)
        self.assertIn("eventPicks=0", second)
        # 2日目も同じイベントは応答に入っている（＝重複を弾いた結果である）
        self.assertIn("seen=1", second)


class EventSlotSizeTest(unittest.TestCase):
    def test_the_default_is_twenty(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(fetch_forecasts.event_slot_size(), 20)

    def test_a_bad_value_is_rejected_loudly(self) -> None:
        for raw in ("いち", "-1", "101", "1.5"):
            with self.subTest(raw=raw):
                with mock.patch.dict(os.environ, {"DVC_EVENT_SLOTS": raw}):
                    with self.assertRaises(ValueError):
                        fetch_forecasts.event_slot_size()


if __name__ == "__main__":
    unittest.main()
