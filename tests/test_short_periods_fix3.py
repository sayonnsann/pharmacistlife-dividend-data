"""段階2の仕上げ: 1か月に満たない短い期（9444 型の8日の期）と、分割を当てる決まり（8113 型）。

合成の小さなデータだけを使う。edinet-direct 側は edinet-direct/tests/test_short_period_fix3.py。
- months は null か0を受け付ける（1〜12 は今までどおり）。負の数・文字列・小数・真偽値は止める
- 画面（serving/checker.html）は months が null か0なら「8日」のように日数で出す
- 未反映の分割は「効力日が配当の基準日より後なら当てる」。権利落ち日は使わない
"""
from __future__ import annotations

import copy
import importlib.util
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from fiscal_fixtures import annual_report_fixture

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("build_store", ROOT / "scripts" / "build_store.py")
build_store = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(build_store)

import test_short_periods as base  # noqa: E402  （同じ補助を使う）

SERIES = base.SERIES


def eight_day_item(**overrides):
    item = {"periodStart": "2026-05-01", "periodEnd": "2026-05-08", "months": None,
            "lengthDays": 8, "dividend": 0.0, "status": "stop", "between": [2014, 2015],
            "source": "edinet"}
    item.update(overrides)
    return item


def load(shorts):
    return build_store.normalize_fiscal_dividends(annual_report_fixture({
        "9991": base.raw_record(SERIES, shorts=shorts)}))


# ---------------------------------------------------------------------------
# months の形
# ---------------------------------------------------------------------------


def test_loader_accepts_null_months_with_length_days():
    loaded = load([eight_day_item()])
    item = loaded["9991"]["shortPeriodDividends"][0]
    assert item["months"] is None and item["lengthDays"] == 8
    assert list(item)[:4] == ["periodStart", "periodEnd", "months", "lengthDays"]


def test_loader_accepts_zero_months_as_before_stage2_finish():
    # 段階2の前の edinet-direct は8日の期を months=0 で書いていた。止めずに受け付ける
    loaded = load([eight_day_item(months=0, lengthDays=None)])
    item = loaded["9991"]["shortPeriodDividends"][0]
    assert item["months"] == 0 and "lengthDays" not in item


def test_twelve_or_fewer_months_are_still_accepted():
    loaded = load([base.short_item([2014, 2015], months=1), base.short_item([2015, 2016],
                                                                             end="2015-12-31",
                                                                             start="2015-01-01",
                                                                             months=12)])
    assert [i["months"] for i in loaded["9991"]["shortPeriodDividends"]] == [1, 12]


@pytest.mark.parametrize("months", [-1, "8", "0", 0.5, 9.0, True, False, 13, [], {}])
def test_other_month_shapes_still_stop(months):
    with pytest.raises(ValueError, match="months"):
        load([eight_day_item(months=months)])


@pytest.mark.parametrize("length_days", [0, -8, "8", 8.0, True])
def test_broken_length_days_stop(length_days):
    with pytest.raises(ValueError, match="lengthDays"):
        load([eight_day_item(lengthDays=length_days)])


def test_payload_and_public_check_keep_length_days():
    from public_dividend_policy import validate_short_period_fields
    fiscal_raw = {"9991": base.raw_record(SERIES, [base.bridge([2014, 2015], "stop", end="2026-05-08")],
                                          [eight_day_item()])}
    financial_extra = {"9991": {"shortPeriods": [
        {"periodStart": "2026-05-01", "periodEnd": "2026-05-08", "months": None, "lengthDays": 8,
         "metrics": {"revenue": 1000}}]}}
    data = json.loads(base.build_payloads(fiscal_raw, financial_extra)["9991"])
    assert data["shortPeriodDividends"][0]["months"] is None
    assert data["shortPeriodDividends"][0]["lengthDays"] == 8
    assert data["shortPeriods"][0]["lengthDays"] == 8
    validate_short_period_fields(copy.deepcopy(data))
    with pytest.raises(ValueError, match="short"):
        validate_short_period_fields({"shortPeriods": [{**data["shortPeriods"][0], "days": 8}]})


# ---------------------------------------------------------------------------
# 画面: months が null か0なら日数で出す
# ---------------------------------------------------------------------------

JSC = "/System/Library/Frameworks/JavaScriptCore.framework/Versions/Current/Helpers/jsc"


def _js_functions(names):
    html = (ROOT / "serving" / "checker.html").read_text(encoding="utf-8")
    parts = []
    for name in names:
        match = re.search(r"  function " + name + r"\(.*", html)
        assert match, name
        parts.append(match.group(0))
    return "\n".join(parts)


def _run_js(body):
    runner = shutil.which("jsc") or (JSC if Path(JSC).exists() else None)
    if runner is None:
        pytest.skip("JavaScript を動かす道具（jsc）が無い")
    script = _js_functions(["isFiniteNumber", "shortYM", "shortPeriodDays", "shortMonthsLabel",
                            "shortPeriodText"]) + "\n" + body
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
        f.write(script)
    try:
        out = subprocess.run([runner, f.name], capture_output=True, text=True, timeout=30)
    finally:
        Path(f.name).unlink()
    assert out.returncode == 0, out.stderr
    return out.stdout.splitlines()


def test_checker_html_shows_days_for_null_or_zero_months():
    lines = _run_js("\n".join([
        'print(shortMonthsLabel({periodStart:"2026-05-01",periodEnd:"2026-05-08",months:null,lengthDays:8}))',
        # lengthDays が無ければ期の初日と末日から数える
        'print(shortMonthsLabel({periodStart:"2026-05-01",periodEnd:"2026-05-08",months:0}))',
        'print(shortMonthsLabel({periodStart:"2026-05-01",periodEnd:"2026-05-08",months:null}))',
        'print(shortMonthsLabel({periodStart:"2014-04-01",periodEnd:"2014-12-31",months:9}))',
        # 期首が分からなければ、今までどおり出さない
        'print("["+shortMonthsLabel({periodStart:null,periodEnd:"2012-06",months:null})+"]")',
        'print(shortPeriodText({periodStart:"2026-05-01",periodEnd:"2026-05-08",months:null,lengthDays:8}))',
        'print(shortPeriodText({periodStart:"2014-04-01",periodEnd:"2014-12-31",months:9}))',
    ]))
    assert lines == ["8日", "8日", "8日", "9か月", "[]",
                     "決算期変更の短い期（2026年5月の8日）",
                     "決算期変更の短い期（2014年4月〜12月の9か月）"]


# ---------------------------------------------------------------------------
# 分割を当てる決まり: 効力日が配当の基準日より後なら当てる。権利落ち日は使わない（8113 型）
# ---------------------------------------------------------------------------


def adjustment_with_ex_date(day, ex_date, factor=1 / 3):
    return {"events": [{"effectiveDate": day, "exRightsDate": ex_date, "exDate": ex_date,
                        "recordDate": "2014-09-30", "adjustmentFactor": factor,
                        "applyDividendAdjustment": True, "status": "confirmed"}]}


def test_8113_type_interim_counts_as_affected_by_the_effective_date():
    # 配当の基準日 2014-09-30、分割の基準日 2014-09-30、権利落ち日 2014-09-26、効力日 2014-10-01。
    # 効力日で決めると中間には当たり、期末には当たらない → この画面には中間と期末の額が無いので
    # 決められない（None）。権利落ち日で決めていれば、どちらにも当たらず 4.24 のままになる
    stats = {"streakIncreaseCountedPairs": [], "streakNonDecreaseCountedPairs": []}
    item = base.split_item()
    result = build_store.short_period_dividend_payload(
        [item], stats, [], adjustment_with_ex_date("2014-10-01", "2014-09-26"))
    assert result[0]["dividend"] is None
    # 期の後に効力日がある分割（権利落ち日は期末より前）は、中間にも期末にも当てる
    later = build_store.short_period_dividend_payload(
        [base.short_item([2014, 2015], dividend=9.0)], stats, [],
        adjustment_with_ex_date("2015-01-01", "2014-12-26"))
    assert later[0]["dividend"] == round(9.0 / 3, 4)


def test_the_rule_comment_is_kept_in_the_code():
    text = (ROOT / "scripts" / "build_store.py").read_text(encoding="utf-8")
    body = text.split("def _short_period_unreflected_factor", 1)[1].split("\ndef ", 1)[0]
    assert "権利落ち日は株価の切り替わりの日で、配当の換算には使わない" in body
    assert "exRightsDate" not in body and "exDate" not in body
