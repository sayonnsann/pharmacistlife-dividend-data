"""段階1b: 決算期変更の短い期（streakBridges）を、連続年数で1期と数える。

合成の小さなデータだけを使う。数え方は edinet-direct の
scripts/report_streak_changes.py と同じ（同じ入力で比べるテストは edinet-direct 側の
tests/test_streak_bridges.py::test_same_streaks_as_pdd）。
"""
from __future__ import annotations

import importlib.util
import json
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

# 8113 型: 2014年3月期 → 2014年12月期（9か月の短い期）→ 2015年12月期
SERIES = {2011: 10.0, 2012: 11.0, 2013: 12.0, 2014: 13.0, 2015: 14.0}


def bridge(between, status, end="2014-12-31"):
    return {"periodStart": None, "periodEnd": end, "lengthDays": None,
            "between": list(between), "status": status, "dps": None, "source": "edinet"}


def streaks(series, bridges):
    pairs = build_store.streak_bridges_by_pair(bridges) or None
    stats = build_store.fiscal_dividend_stats(series, bridges=pairs)
    return stats["streakIncrease"], stats["streakNonDecrease"]


def test_without_bridges_the_result_is_unchanged():
    assert build_store.fiscal_dividend_stats(SERIES) == build_store.fiscal_dividend_stats(
        SERIES, bridges=None)
    assert build_store.fiscal_dividend_stats(SERIES) == build_store.fiscal_dividend_stats(
        SERIES, bridges={})
    assert "streakIncreaseShortPeriods" not in build_store.fiscal_dividend_stats(SERIES)
    years = sorted(SERIES)
    assert build_store._streak_from_series(SERIES, years, allow_equal=False) == (4, True)


def test_counted_short_period_adds_one_period():
    stats = build_store.fiscal_dividend_stats(
        SERIES, bridges=build_store.streak_bridges_by_pair([bridge([2014, 2015], "count")]))
    assert stats["streakIncrease"] == 5
    assert stats["streakNonDecrease"] == 5
    assert stats["streakIncreaseShortPeriods"] == 1
    assert stats["streakNonDecreaseShortPeriods"] == 1
    # 天井の判定は変えない（系列の最初の年まで届いている）
    assert stats["streakIncreaseCapped"] is True


def test_short_period_before_the_collided_year_uses_k_minus_1_and_k():
    # between が [K-1, K] の短い期も、その組の比べで足す
    assert streaks(SERIES, [bridge([2012, 2013], "count", "2013-02-28")]) == (5, 5)


@pytest.mark.parametrize("policy,expected,capped", [
    ("stop_streak", 2, False),       # A: その比べで止める
    ("skip_without_stop", 4, True),  # B（2026-11-10 オーナーの判断）: 数えないが止めない。データの初めまで届く
])
def test_zero_short_period_at_that_comparison(policy, expected, capped, monkeypatch):
    monkeypatch.setattr(build_store, "SHORT_PERIOD_NO_DIVIDEND_POLICY", policy)
    stats = build_store.fiscal_dividend_stats(
        SERIES, bridges=build_store.streak_bridges_by_pair([bridge([2012, 2013], "stop")]))
    assert stats["streakIncrease"] == expected
    assert stats["streakIncreaseCapped"] is capped
    assert stats["streakIncreaseShortPeriods"] == 0


def test_unknown_short_period_adds_nothing_and_does_not_stop():
    assert streaks(SERIES, [bridge([2014, 2015], "unknown")]) == (4, 4)


def test_bridge_outside_the_counted_streak_is_ignored():
    series = {2011: 10.0, 2012: 9.0, 2013: 12.0, 2014: 13.0}
    assert streaks(series, [bridge([2011, 2012], "count")]) == (2, 2)


def test_unreliable_streak_counts_no_short_periods():
    stats = build_store.fiscal_dividend_stats(
        SERIES, streak_reliable=False,
        bridges=build_store.streak_bridges_by_pair([bridge([2014, 2015], "count")]))
    assert stats["streakIncrease"] is None
    assert stats["streakIncreaseShortPeriods"] == 0


def test_base_streaks_take_the_same_bridges():
    breakdown = {"2013": {"base": 11.5, "special": 0.5}}
    pairs = build_store.streak_bridges_by_pair([bridge([2014, 2015], "count")])
    assert build_store.streak_base_from_breakdown(SERIES, breakdown, bridges=pairs) == 5
    assert build_store.streak_no_decrease_base_from_breakdown(SERIES, breakdown, bridges=pairs) == 5
    assert build_store.streak_base_from_breakdown(SERIES, breakdown) == 4


def raw_record(series, bridges=None):
    record = {
        "fiscalMonth": 12,
        "series": {str(y): v for y, v in series.items()},
        "connection": {"status": "edinet_only", "reason": ""},
        "externalYears": [],
    }
    if bridges is not None:
        record["streakBridges"] = bridges
    return record


def test_loader_accepts_and_keeps_streak_bridges():
    loaded = build_store.normalize_fiscal_dividends(annual_report_fixture({
        "9991": raw_record(SERIES, [bridge([2014, 2015], "count")]),
        "9992": raw_record(SERIES),
    }))
    assert loaded["9991"]["streakBridges"] == [{"between": [2014, 2015], "status": "count"}]
    assert "streakBridges" not in loaded["9992"]


@pytest.mark.parametrize("bad", [
    {"bad": "not a list"},
    ["not an object"],
    [{"between": [2014, 2016], "status": "count"}],
    [{"between": [2014], "status": "count"}],
    [{"between": ["2014", "2015"], "status": "count"}],
    [{"between": [2014, 2015], "status": "maybe"}],
])
def test_loader_rejects_broken_streak_bridges(bad):
    with pytest.raises(ValueError, match="streakBridges"):
        build_store.normalize_fiscal_dividends(annual_report_fixture({
            "9991": raw_record(SERIES, bad),
        }))


def build_payloads(fiscal_raw: dict, breakdown: dict | None = None) -> dict[str, str]:
    codes = sorted(fiscal_raw)
    financials = [{"code": code, "name": f"テスト{code}"} for code in codes]
    tickers = {code: {"code": code, "name": f"テスト{code}"} for code in codes}
    fiscal = build_store.normalize_fiscal_dividends(annual_report_fixture(fiscal_raw))
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / "data").mkdir()
        (root / "data" / "dividend_breakdown.json").write_text(
            json.dumps(breakdown or {}), encoding="utf-8")
        path = root / "store.sqlite"
        with mock.patch.object(build_store, "REPOSITORY_ROOT", root), mock.patch.object(
            build_store, "load_daily_prices", return_value=({}, {}, "2026-11-10")
        ):
            build_store.create_database(
                path, financials, {}, tickers, {},
                [Path("f"), Path("s"), Path("t"), Path("fc")], "fixture.csv",
                {}, Path("actions.json"), fiscal, Path("fiscal.json"),
                {}, Path("calendar.json"), date(2026, 11, 10),
            )
        with sqlite3.connect(path) as connection:
            return dict(connection.execute("SELECT code, payload FROM stocks").fetchall())


def test_payload_marks_only_companies_that_counted_a_short_period():
    flat = {2012: 10.0, 2013: 10.0, 2014: 11.0}
    fiscal_raw = {
        "9991": raw_record(SERIES, [bridge([2014, 2015], "count")]),
        "9992": raw_record(SERIES),
        "9993": raw_record(SERIES, [bridge([2014, 2015], "unknown")]),
        # 連続非減配だけで足す（連続増配の比べは 2013→2014 で止まる）
        "9994": raw_record(flat, [bridge([2012, 2013], "count", "2012-12-31")]),
    }
    breakdown = {"9991": {"2013": {"base": 11.5, "special": 0.5}}}
    payloads = build_payloads(fiscal_raw, breakdown)
    data = {code: json.loads(text) for code, text in payloads.items()}

    assert data["9991"]["streakIncrease"] == 5
    assert data["9991"]["streakBase"] == 5
    assert data["9991"]["streakShortPeriodsCounted"] == {
        "streakIncrease": 1, "streakNonDecrease": 1,
        "streakBase": 1, "streakNoDecreaseBase": 1,
    }
    assert data["9994"]["streakIncrease"] == 1
    assert data["9994"]["streakNonDecrease"] == 3
    assert data["9994"]["streakShortPeriodsCounted"] == {
        "streakNonDecrease": 1, "streakNoDecreaseBase": 1,
    }
    for code in ("9992", "9993"):
        assert "streakShortPeriodsCounted" not in data[code]
    for text in payloads.values():
        assert "streakBridges" not in text

    # 当たらない会社の payload は、streakBridges の欄が無い入力と1バイトも変わらない
    without = build_payloads(
        {code: raw_record(SERIES if code != "9994" else flat) for code in fiscal_raw},
        breakdown,
    )
    assert payloads["9992"] == without["9992"]
    assert payloads["9993"] == without["9993"]
    assert payloads["9991"] != without["9991"]


def test_streak_bridges_is_a_private_key_of_the_public_payload():
    from public_dividend_policy import PRIVATE_KEYS, validate_public_payload
    assert "streakBridges" in PRIVATE_KEYS
    with pytest.raises(ValueError, match="非公開"):
        validate_public_payload({"code": "9991", "streakBridges": []}, set(), set())
