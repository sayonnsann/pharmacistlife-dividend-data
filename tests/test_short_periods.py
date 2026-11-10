"""段階2: 決算期変更の短い期を、色と注釈つきで画面に残す（payload の shortPeriodDividends・shortPeriods）。

合成の小さなデータだけを使う。edinet-direct 側の作り方は
edinet-direct/tests/test_short_period_display.py。
"""
from __future__ import annotations

import copy
import importlib.util
import json
import re
import sqlite3
import tempfile
from datetime import date
from pathlib import Path
from unittest import mock

import pytest

from fiscal_fixtures import annual_report_fixture

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("build_store", ROOT / "scripts" / "build_store.py")
build_store = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(build_store)

SERIES = {2011: 10.0, 2012: 11.0, 2013: 12.0, 2014: 13.0, 2015: 14.0}


def bridge(between, status, end="2014-12-31"):
    return {"periodStart": None, "periodEnd": end, "lengthDays": None,
            "between": list(between), "status": status, "dps": None, "source": "edinet"}


def short_item(between, status="count", dividend=5.0, end="2014-12-31", source="edinet",
               start="2014-04-01", months=9):
    return {"periodStart": start, "periodEnd": end, "months": months, "dividend": dividend,
            "status": status, "between": None if between is None else list(between),
            "source": source}


def raw_record(series, bridges=None, shorts=None):
    record = {
        "fiscalMonth": 12,
        "series": {str(y): v for y, v in series.items()},
        "connection": {"status": "edinet_only", "reason": ""},
        "externalYears": [],
    }
    if bridges is not None:
        record["streakBridges"] = bridges
    if shorts is not None:
        record["shortPeriodDividends"] = shorts
    return record


def build_payloads(fiscal_raw: dict, financial_extra: dict | None = None,
                   stock_actions: dict | None = None) -> dict[str, str]:
    codes = sorted(fiscal_raw)
    financials = [{"code": code, "name": f"テスト{code}", **(financial_extra or {}).get(code, {})}
                  for code in codes]
    tickers = {code: {"code": code, "name": f"テスト{code}"} for code in codes}
    fiscal = build_store.normalize_fiscal_dividends(annual_report_fixture(fiscal_raw))
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / "data").mkdir()
        (root / "data" / "dividend_breakdown.json").write_text("{}", encoding="utf-8")
        path = root / "store.sqlite"
        with mock.patch.object(build_store, "REPOSITORY_ROOT", root), mock.patch.object(
            build_store, "load_daily_prices", return_value=({}, {}, "2026-11-10")
        ):
            build_store.create_database(
                path, financials, {}, tickers, {},
                [Path("f"), Path("s"), Path("t"), Path("fc")], "fixture.csv",
                stock_actions or {}, Path("actions.json"), fiscal, Path("fiscal.json"),
                {}, Path("calendar.json"), date(2026, 11, 10),
            )
        with sqlite3.connect(path) as connection:
            return dict(connection.execute("SELECT code, payload FROM stocks").fetchall())


# ---------------------------------------------------------------------------
# 読み込みの確かめ
# ---------------------------------------------------------------------------


def test_loader_keeps_short_period_dividends_in_period_order():
    loaded = build_store.normalize_fiscal_dividends(annual_report_fixture({
        "9991": raw_record(SERIES, shorts=[short_item([2014, 2015], end="2014-12-31"),
                                           short_item(None, end="2012-06", start=None,
                                                      months=None, dividend=None,
                                                      status="unknown", source="ir")]),
        "9992": raw_record(SERIES),
    }))
    assert [i["periodEnd"] for i in loaded["9991"]["shortPeriodDividends"]] == ["2012-06", "2014-12-31"]
    assert "shortPeriodDividends" not in loaded["9992"]


@pytest.mark.parametrize("bad", [
    "not a list",
    ["not an object"],
    [short_item([2014, 2015], source="haitoukin")],
    [short_item([2014, 2015], status="maybe")],
    [short_item([2014, 2016])],
    [short_item([2014, 2015], dividend=-1)],
    [short_item([2014, 2015], months=13)],
    [short_item([2014, 2015], end="2014/12/31")],
])
def test_loader_rejects_broken_or_non_issuer_short_periods(bad):
    with pytest.raises(ValueError, match="shortPeriodDividends"):
        build_store.normalize_fiscal_dividends(annual_report_fixture({
            "9991": raw_record(SERIES, shorts=bad),
        }))


# ---------------------------------------------------------------------------
# payload: countedIn と、当たらない会社のバイト
# ---------------------------------------------------------------------------


def test_counted_in_marks_only_short_periods_inside_the_current_streak():
    flat = {2012: 10.0, 2013: 10.0, 2014: 11.0}
    broken = {2011: 10.0, 2012: 9.0, 2013: 12.0, 2014: 13.0}
    fiscal_raw = {
        # 連続増配・連続非減配の両方で数えた
        "9991": raw_record(SERIES, [bridge([2014, 2015], "count")],
                           [short_item([2014, 2015])]),
        # 連続非減配だけで数えた（連続増配は 2013→2014 で止まる）
        "9994": raw_record(flat, [bridge([2012, 2013], "count", "2012-12-31")],
                           [short_item([2012, 2013], end="2012-12-31")]),
        # 連続の外（2011→2012 で途切れている）
        "9995": raw_record(broken, [bridge([2011, 2012], "count", "2011-12-31")],
                           [short_item([2011, 2012], end="2011-12-31")]),
        # 無配と、配当が分からない期と、streakBridges に入らなかった期（between が無い）
        "9996": raw_record(SERIES, [bridge([2012, 2013], "unknown", "2012-12-31")],
                           [short_item([2012, 2013], status="unknown", dividend=None,
                                       end="2012-12-31"),
                            short_item(None, end="2016-06-30")]),
        "9997": raw_record(SERIES, [bridge([2014, 2015], "stop")],
                           [short_item([2014, 2015], status="stop", dividend=0.0)]),
    }
    data = {code: json.loads(text) for code, text in build_payloads(fiscal_raw).items()}
    assert data["9991"]["shortPeriodDividends"][0]["countedIn"] == [
        "streakIncrease", "streakNonDecrease"]
    assert data["9994"]["shortPeriodDividends"][0]["countedIn"] == ["streakNonDecrease"]
    assert data["9995"]["shortPeriodDividends"][0]["countedIn"] == []
    assert [i["countedIn"] for i in data["9996"]["shortPeriodDividends"]] == [[], []]
    assert data["9997"]["shortPeriodDividends"][0]["countedIn"] == []
    # B（今の決まり）: 無配の短い期は数えないが止めないので、SERIES の4年がそのまま残る
    assert data["9997"]["streakIncrease"] == 4
    for text in build_payloads(fiscal_raw).values():
        assert "streakBridges" not in text and "CountedPairs" not in text


def test_counted_in_agrees_with_the_streak_short_period_marker():
    fiscal_raw = {"9991": raw_record(SERIES, [bridge([2014, 2015], "count")],
                                     [short_item([2014, 2015])])}
    data = json.loads(build_payloads(fiscal_raw)["9991"])
    assert data["streakShortPeriodsCounted"]["streakIncrease"] == 1
    assert data["shortPeriodDividends"][0]["countedIn"]


def test_payload_without_short_periods_is_byte_for_byte_unchanged():
    flat = {2012: 10.0, 2013: 10.0, 2014: 11.0}
    fiscal_raw = {
        "9991": raw_record(SERIES, [bridge([2014, 2015], "count")], [short_item([2014, 2015])]),
        "9992": raw_record(SERIES),
        "9993": raw_record(flat, [bridge([2012, 2013], "count", "2012-12-31")]),
    }
    financial_extra = {"9991": {"shortPeriods": [{"periodStart": "2014-04-01",
                                                  "periodEnd": "2014-12-31", "months": 9,
                                                  "metrics": {"eps": 4.0}}]}}
    payloads = build_payloads(fiscal_raw, financial_extra)
    without = build_payloads({
        "9991": raw_record(SERIES, [bridge([2014, 2015], "count")]),
        "9992": raw_record(SERIES),
        "9993": raw_record(flat, [bridge([2012, 2013], "count", "2012-12-31")]),
    })
    assert payloads["9992"] == without["9992"]
    assert payloads["9993"] == without["9993"]
    first = json.loads(payloads["9991"])
    assert "shortPeriodDividends" in first and "shortPeriods" in first
    rest = {k: v for k, v in first.items() if k not in ("shortPeriodDividends", "shortPeriods")}
    assert json.dumps(rest, ensure_ascii=False) == json.dumps(
        {k: v for k, v in json.loads(without["9991"]).items()}, ensure_ascii=False)


# ---------------------------------------------------------------------------
# この画面で足す未反映の分割（12か月の期と同じ決まり。期末の日で当てる）
# ---------------------------------------------------------------------------


def adjustment(day, factor=0.5, *, eps_issuer=False):
    return {"events": [{"effectiveDate": day, "adjustmentFactor": factor,
                        "applyDividendAdjustment": True, "status": "confirmed",
                        "epsAdjustedByIssuer": eps_issuer}]}


def test_short_period_dividend_gets_the_unapplied_split_by_its_period_end():
    items = [short_item([2014, 2015], dividend=10.0)]
    stats = {"streakIncreaseCountedPairs": [], "streakNonDecreaseCountedPairs": []}
    after = build_store.short_period_dividend_payload(items, stats, [], adjustment("2016-07-01"))
    assert after[0]["dividend"] == 5.0
    before = build_store.short_period_dividend_payload(items, stats, [], adjustment("2014-03-01"))
    assert before[0]["dividend"] == 10.0
    inside = build_store.short_period_dividend_payload(items, stats, [], adjustment("2014-10-01"))
    assert inside[0]["dividend"] is None
    assert build_store.short_period_dividend_payload(items, stats, [], None)[0]["dividend"] == 10.0
    assert items[0]["dividend"] == 10.0  # 元の値は書き換えない


def test_period_end_factor_matches_the_year_rule_for_neighbouring_years():
    # 12か月の期（12月決算）と同じ係数になる（間に分割が無いとき）
    adj = adjustment("2016-07-01")
    year_factor = build_store.adjustment_factor_for_period(adj, 2015, fiscal_month=12, field="dividend")
    short_factor = build_store.adjustment_factor_for_period(
        adj, None, fiscal_month=None, field="dividend", period_end=date(2014, 12, 31))
    assert year_factor == short_factor == 0.5


def test_short_period_eps_gets_the_same_adjustment_as_the_years():
    fiscal_raw = {"9991": raw_record(SERIES, [bridge([2014, 2015], "count")],
                                     [short_item([2014, 2015])])}
    financial_extra = {"9991": {
        "eps": {"2014": 40.0, "2015": 50.0},
        "shortPeriods": [{"periodStart": "2014-04-01", "periodEnd": "2014-12-31",
                          "months": 9, "metrics": {"eps": 30.0, "revenue": 1000}}],
    }}
    actions = {"9991": [{"effectiveDate": "2016-07-01", "oldShares": 1, "newShares": 2,
                         "status": "confirmed", "epsAdjustedByIssuer": False,
                         "applyDividendAdjustment": True, "eventId": "9991-split", "securityCode": "9991",
                         "source": "test"}]}
    with mock.patch.object(build_store, "split_adjustment",
                           return_value={"dividendFactor": 0.5, "events": [
                               {**actions["9991"][0], "adjustmentFactor": 0.5}]}):
        data = json.loads(build_payloads(fiscal_raw, financial_extra, actions)["9991"])
    assert data["eps"] == {"2014": 20.0, "2015": 25.0}
    assert data["shortPeriods"][0]["metrics"] == {"eps": 15.0, "revenue": 1000}


# ---------------------------------------------------------------------------
# 公開の確かめ（会社の資料だけ・決めた欄だけ）
# ---------------------------------------------------------------------------


def test_public_payload_rejects_unknown_fields_and_sources():
    from public_dividend_policy import validate_public_payload
    good = {"code": "9991", "shortPeriodDividends": [{**short_item([2014, 2015]), "countedIn": []}],
            "shortPeriods": [{"periodStart": None, "periodEnd": "2014-12", "months": None,
                              "metrics": {"eps": 1.0}}]}
    validate_public_payload(copy.deepcopy(good), set(), set())
    for broken in (
        {"shortPeriodDividends": [{**short_item([2014, 2015], source="haitoukin"), "countedIn": []}]},
        {"shortPeriodDividends": [{**short_item([2014, 2015]), "countedIn": ["streakBase"]}]},
        {"shortPeriodDividends": [{**short_item([2014, 2015]), "countedIn": [], "dps": 3}]},
        {"shortPeriods": [{"periodEnd": "2014-12", "metrics": {"dividendPerShare": 1.0}}]},
        {"shortPeriods": [{"periodEnd": "2014-12", "metrics": {"eps": "1"}}]},
        {"shortPeriods": []},
    ):
        with pytest.raises(ValueError, match="short"):
            validate_public_payload({**good, **broken}, set(), set())


# ---------------------------------------------------------------------------
# 無配の短い期の決まり（A=止める / B=数えないが止めない）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("policy,expected", [("stop_streak", 0), ("skip_without_stop", 4)])
def test_no_dividend_policy_switches_the_stop(policy, expected, monkeypatch):
    pairs = build_store.streak_bridges_by_pair([bridge([2014, 2015], "stop")])
    monkeypatch.setattr(build_store, "SHORT_PERIOD_NO_DIVIDEND_POLICY", policy)
    stats = build_store.fiscal_dividend_stats(SERIES, bridges=pairs)
    assert stats["streakIncrease"] == expected
    assert stats["streakIncreaseShortPeriods"] == 0


def test_checker_html_picks_the_no_dividend_text_from_the_same_constant():
    html = (ROOT / "serving" / "checker.html").read_text(encoding="utf-8")
    match = re.search(r'var SHORT_PERIOD_NO_DIVIDEND_POLICY="([a-z_]+)"', html)
    assert match and match.group(1) == build_store.SHORT_PERIOD_NO_DIVIDEND_POLICY == "skip_without_stop"
    texts = re.search(r"var SHORT_PERIOD_NO_DIVIDEND_TEXT=(\{[^}]*\})", html).group(1)
    texts = json.loads(re.sub(r"(\w+):", r'"\1":', texts))
    assert texts == {
        "stop_streak": "無配のため、連続年数はここで途切れます",
        "skip_without_stop": "無配のため、連続年数では数えていません",
    }
    assert set(texts) == set(build_store.NO_DIVIDEND_POLICIES)
    # 文は直書きせず、定数で引く
    assert "SHORT_PERIOD_NO_DIVIDEND_TEXT[SHORT_PERIOD_NO_DIVIDEND_POLICY]" in html
    assert html.count("無配のため、連続年数") == 2


# ---------------------------------------------------------------------------
# 段階2の追加: 期の中に分割がある短い期（中間と期末を別々に調整した基準日 recordDates）
# ---------------------------------------------------------------------------

RECORD_DATES_8113 = {"interim": ["2014-09-30"], "final": "2014-12-31"}


def split_item(dividend=4.24, record_dates=RECORD_DATES_8113):
    # edinet-direct が 19 ÷ (3 × 3) ＋ 6.4 ÷ 3 = 4.24 にした 8113 型の期
    return {**short_item([2014, 2015], dividend=dividend), "recordDates": record_dates,
            "recordDateSource": None if record_dates is None else {
                "type": "NotesRegardingDividendTextBlock", "docIds": ["S100TEST"]}}


def test_loader_keeps_record_dates_only_for_the_calculation():
    loaded = build_store.normalize_fiscal_dividends(annual_report_fixture({
        "9991": raw_record(SERIES, shorts=[split_item()]),
        "9992": raw_record(SERIES, shorts=[short_item([2014, 2015])]),
    }))
    assert loaded["9991"]["shortPeriodDividends"][0]["recordDates"] == RECORD_DATES_8113
    assert "recordDateSource" not in loaded["9991"]["shortPeriodDividends"][0]
    assert "recordDates" not in loaded["9992"]["shortPeriodDividends"][0]
    stats = {"streakIncreaseCountedPairs": [], "streakNonDecreaseCountedPairs": []}
    payload = build_store.short_period_dividend_payload(
        loaded["9991"]["shortPeriodDividends"], stats, [], None)
    assert set(payload[0]) == {"periodStart", "periodEnd", "months", "dividend", "status",
                               "between", "source", "countedIn"}
    assert payload[0]["dividend"] == 4.24


@pytest.mark.parametrize("bad", [
    {"interim": "2014-09-30", "final": "2014-12-31"},
    {"interim": [], "final": "2014-12"},
    {"interim": ["2014-12-31"], "final": "2014-09-30"},
    {"interim": ["2014-03-31"], "final": "2014-12-31"},
    {"final": "2014-12-31"},
])
def test_loader_rejects_broken_record_dates(bad):
    with pytest.raises(ValueError, match="recordDates"):
        build_store.normalize_fiscal_dividends(annual_report_fixture({
            "9991": raw_record(SERIES, shorts=[split_item(record_dates=bad)]),
        }))


def test_unreflected_split_uses_the_same_record_date_rule():
    stats = {"streakIncreaseCountedPairs": [], "streakNonDecreaseCountedPairs": []}

    def payload(items, day):
        return build_store.short_period_dividend_payload(items, stats, [], adjustment(day))[0]

    with_dates = [split_item()]
    # 期より後: 中間にも期末にも当たる（今までと同じ）
    assert payload(with_dates, "2016-07-01")["dividend"] == round(4.24 * 0.5, 4)
    # 期の中でも中間の基準日より前: どちらにも当たらない
    assert payload(with_dates, "2014-06-01")["dividend"] == 4.24
    # 中間の基準日と同じ日: 当てない（基準日より後だけ当てる）
    assert payload(with_dates, "2014-09-30")["dividend"] == 4.24
    # 中間と期末の間: 中間だけに当たるが、この画面には中間と期末の額が無いので決められない
    assert payload(with_dates, "2014-10-01")["dividend"] is None
    # 基準日が無い期で、期の中に分割: 決められない（今までと同じ）
    assert payload([short_item([2014, 2015], dividend=10.0)], "2014-10-01")["dividend"] is None
    # 期の中に分割が無い期は、今までと同じ（期末の日で比べる）
    plain = [short_item([2014, 2015], dividend=10.0)]
    assert payload(plain, "2016-07-01")["dividend"] == 5.0
    assert payload(plain, "2014-03-01")["dividend"] == 10.0


def test_public_payload_still_rejects_record_dates():
    # recordDates は計算に使うだけで公開しない（公開の確かめの欄の一覧は変えていない）
    from public_dividend_policy import validate_public_payload
    item = {**short_item([2014, 2015]), "countedIn": [], "recordDates": RECORD_DATES_8113}
    with pytest.raises(ValueError, match="short"):
        validate_public_payload({"code": "9991", "shortPeriodDividends": [item]}, set(), set())


def test_split_inside_period_payload_end_to_end():
    fiscal_raw = {"9991": raw_record(SERIES, [bridge([2014, 2015], "count")], [split_item()])}
    data = json.loads(build_payloads(fiscal_raw)["9991"])
    assert data["shortPeriodDividends"][0]["dividend"] == 4.24
    assert "recordDates" not in data["shortPeriodDividends"][0]
