"""期中分割の台帳補完と、秘密情報を出さない比較ツール。合成データのみ。"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from datetime import date
from pathlib import Path
from unittest import mock

import pytest
from fiscal_fixtures import annual_report_fixture

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import build_store as store

TODAY = date(2026, 11, 11)
KAO = dict(forecastDividend=39, forecastInterimDividend=78,
           forecastFinalDividend=39, forecastDividendAdjusted=78,
           forecastPeriod="2026年12月期(予)", forecastFiscalYear=2026)


def event(day="2026-07-01", old=1, new=2, status="confirmed"):
    return dict(eventId=day, securityCode="4452", action="split",
                effectiveDate=day, oldShares=old, newShares=new, status=status,
                applyDividendAdjustment=True, epsAdjustedByIssuer=False,
                source=dict(url="https://example.com/split.pdf"))


def resolve(record=None, events=None, today=TODAY, **kwargs):
    return store.forecast_on_price_basis(
        KAO if record is None else record, today=today,
        adjustment=store.split_adjustment([event()] if events is None else events),
        fiscal_month=12, **kwargs)


@pytest.mark.parametrize("adjusted", [78, None])
def test_kao_is_composed_with_or_without_adjusted(adjusted):
    result = resolve(dict(KAO, forecastDividendAdjusted=adjusted))
    assert (result["value"], result["basis"]) == (78, "post_split_composed_ledger")
    assert (result["interim"], result["final"]) == (39, 39)


def test_no_qualifying_split_preserves_the_entire_raw_result():
    expected = dict(value=39, basis="raw", interim=78, final=39)
    for events in ([], [event("2025-07-01")], [event(status="provisional")],
                   [event("2026-01-01")], [event("2026-12-31")]):
        assert resolve(events=events) == expected


@pytest.mark.parametrize("day,expected,basis", [
    ("2026-06-30", 156, "pre_split_composed_ledger"),
    ("2026-07-01", 78, "post_split_composed_ledger"),
])
def test_effective_day_switch(day, expected, basis):
    result = resolve(today=date.fromisoformat(day))
    assert (result["value"], result["basis"]) == (expected, basis)


def test_early_split_does_not_divide_already_post_split_interim():
    for day in ("2026-04-01", "2026-06-30"):
        result = resolve(dict(KAO, forecastInterimDividend=39),
                         events=[event(day)])
        assert (result["value"], result["basis"]) == (78, "post_split_single_basis_ledger")
    # 一致検算は、時期による組み立ての判定よりも先。
    result = resolve(dict(KAO, forecastDividend=117), events=[event("2026-07-01")])
    assert (result["value"], result["basis"]) == (117, "single_basis_as_reported_ledger")


def test_reverse_split_and_multiple_events():
    record = dict(KAO, forecastDividend=60, forecastInterimDividend=10,
                  forecastFinalDividend=60, forecastDividendAdjusted=110)
    assert resolve(record, [event(old=5, new=1)])["value"] == 110
    result = resolve(events=[event(), event("2026-10-01", old=2, new=1)])
    assert (result["value"], result["basis"]) == (None, "split_unresolved")
    # 期外・provisionalは件数に含めない。
    assert resolve(events=[event(), event("2025-10-01"),
                           event("2026-10-01", status="provisional")])["value"] == 78


def test_pending_bar_uses_period_label_without_double_adjusting():
    adjustment = store.split_adjustment([event()])
    result = store.pending_dividends(
        KAO, {2025}, today=TODAY, adjustment=adjustment)
    assert result["2026"]["value"] == 78
    assert result["2026"]["basis"] == "post_split_composed_ledger"
    assert result["2026"]["interim"] + result["2026"]["final"] == 78


def test_unknown_end_is_raw_but_known_short_period_with_split_is_held():
    record = dict(KAO, forecastPeriod="FY2026")
    assert store.forecast_on_price_basis(
        record, today=TODAY, adjustment=store.split_adjustment([event()]))["basis"] == "raw"
    short = dict(shortPeriodDividends=[dict(periodEnd="2026-12-31", months=9)])
    assert resolve(fiscal_record=short)["basis"] == "split_unresolved"
    assert resolve(events=[], fiscal_record=short)["basis"] == "raw"
    assert resolve(dict(KAO, forecastPeriodMonths=9))["basis"] == "split_unresolved"
    # 年＋決算月という既存の手がかりでも解決する。
    assert resolve(record)["value"] == 78


def test_pre_split_without_legs_needs_a_pre_split_claim():
    record = dict(KAO, forecastInterimDividend=None, forecastFinalDividend=None)
    assert resolve(record, today=date(2026, 6, 30))["basis"] == "split_unresolved"
    result = resolve(dict(record, forecastShareBasis="pre_split"), today=date(2026, 6, 30))
    assert (result["value"], result["basis"]) == (39, "pre_split_reported_ledger")


def test_metadata_wins_and_post_split_priority_is_legs_then_adjusted():
    record = dict(KAO, forecastSplitEffectiveDate="2026-10-01", forecastSplitFactor=4)
    result = resolve(record)
    assert (result["value"], result["basis"]) == (58.5, "post_split_composed")
    assert result["warnings"]
    # 記録の効力日が使われ、台帳の日付で上書きされない。
    assert resolve(record, today=date(2026, 9, 30))["value"] == 234
    without_legs = dict(record, forecastInterimDividend=None, forecastFinalDividend=None)
    assert resolve(without_legs)["basis"] == "post_split_adjusted"
    assert resolve(without_legs)["value"] == 78
    assert resolve(dict(without_legs, forecastDividendAdjusted=None))["basis"] == "split_unresolved"


def test_composed_value_wins_and_warning_uses_existing_tolerance():
    assert resolve(dict(KAO, forecastDividendAdjusted=60))["value"] == 78
    assert resolve(dict(KAO, forecastDividendAdjusted=60))["warnings"]
    assert "warnings" not in resolve(dict(KAO, forecastDividendAdjusted=78.3))


def test_ratio_warning_is_notification_only_and_skips_unresolved_and_zero_actual():
    warning = store.forecast_ratio_warning(KAO, 50, {"2025": 100})
    assert "比=0.5" in warning and "分割 N=2" in warning and "期中台帳=なし" in warning
    assert store.forecast_ratio_warning(KAO, 100, {"2025": 100}) is None
    assert store.forecast_ratio_warning(KAO, None, {"2025": 100}) is None
    assert store.forecast_ratio_warning(KAO, 50, {"2025": 0}) is None
    assert store.forecast_ratio_warning(KAO, 50, {}) is None
    assert "期中台帳=ある" in store.forecast_ratio_warning(
        KAO, 50, {"2025": 100}, adjustment=store.split_adjustment([event()]))
    assert "併合 N=5" in store.forecast_ratio_warning(KAO, 500, {"2025": 100})


@pytest.mark.parametrize("unresolved", [True, False])
def test_store_keeps_actual_and_summarizes_unresolved_or_conflicting_forecast(tmp_path, capsys, unresolved):
    record = (dict(KAO, forecastInterimDividend=None, forecastFinalDividend=None,
                   forecastDividendAdjusted=None) if unresolved else
              dict(KAO, forecastDividendAdjusted=60))
    actions = {"4452": [event()]}
    fiscal = store.normalize_fiscal_dividends(annual_report_fixture({
        "4452": dict(fiscalMonth=12, series={"2025": 156})}))
    path = tmp_path / "store.sqlite"
    with mock.patch.object(store, "REPOSITORY_ROOT", tmp_path), \
         mock.patch.object(store, "load_daily_prices", return_value=({"4452": 4000}, {}, "2026-11-11")), \
         mock.patch.object(store, "load_yield_split_adjustments", return_value=[]), \
         mock.patch.object(store, "load_price_session_meta", return_value=None):
        store.create_database(
            path, [dict(code="4452", name="fixture")], {}, {}, {"4452": record},
            [Path(name) for name in ("f", "s", "t", "fc")], "fixture.csv",
            stock_actions_by_code=actions, fiscal_by_code=fiscal,
            today=TODAY)
    with sqlite3.connect(path) as db:
        value, raw = db.execute("SELECT forecast_yield, payload FROM stocks").fetchone()
    payload = json.loads(raw)
    output = capsys.readouterr().out
    assert payload["annual"] == {"2025": 78}
    if unresolved:
        assert value is None and payload["forecastYield"] is None
        assert payload["forecastDividend"] is None and payload["forecastBasis"] == "split_unresolved"
        assert payload["annualPending"] == {} and payload["annualPartial"] == {}
        assert "予想分割保留: 1社 / コード: 4452" in output
    else:
        assert value == 1.95 and payload["forecastDividend"] == 78
        assert payload["annualPending"]["2026"]["value"] == 78
        assert "4452: 内訳合成=78 / adjusted=60 / 内訳を採用" in output


def test_future_ledger_is_available_without_adjusting_actual_early(tmp_path):
    path = tmp_path / "actions.json"
    path.write_text(json.dumps(dict(events=[event()])), encoding="utf-8")
    assert store.load_stock_actions(path, as_of=date(2026, 6, 30)) == {}
    future = store.load_stock_actions(path, as_of=date.max)
    assert resolve(events=future["4452"], today=date(2026, 6, 30))["value"] == 156


@pytest.mark.parametrize("basis", [
    "post_split_composed", "post_split_composed_ledger",
    "post_split_single_basis_ledger", "post_split_adjusted_ledger",
])
def test_new_post_split_bases_keep_the_existing_price_date_guard(basis):
    public = [dict(effectiveDate="2026-07-01")]
    record = dict(forecastSource=dict(disclosureDate="2026-05-01"))
    assert store.yield_safety.forecast_hold_reason(
        record, public, TODAY, forecast_basis=basis, price_as_of=TODAY) is None
    assert store.yield_safety.forecast_hold_reason(
        record, public, TODAY, forecast_basis=basis,
        price_as_of=date(2026, 6, 30)) is not None


def test_comparison_cli_small_state_and_isolated_before_dependencies(tmp_path):
    before = tmp_path / "before" / "scripts"
    before.mkdir(parents=True)
    (before / "private_helper.py").write_text("BASIS = 'raw'\n", encoding="utf-8")
    (before / "build_store.py").write_text(
        "from private_helper import BASIS\n"
        "def apply_forecast_override(code, record, overrides): return record\n"
        "def forecast_on_price_basis(record, *, today):\n"
        "    return dict(value=record['forecastDividend'], basis=BASIS)\n", encoding="utf-8")
    state = tmp_path / "state.json"
    state.write_text(json.dumps(dict(stocks={
        "4452": KAO, "1111": dict(KAO, forecastDividend=50),
        "2222": dict(KAO, forecastDividend=100),
        "3333": dict(KAO, forecastInterimDividend=None,
                     forecastFinalDividend=None, forecastDividendAdjusted=None),
    })), encoding="utf-8")
    actions = tmp_path / "actions.json"
    actions.write_text(json.dumps(dict(events=[
        event(), dict(event(), securityCode="3333", eventId="other")
    ])), encoding="utf-8")
    empty = tmp_path / "empty.json"
    empty.write_text('{"events": []}', encoding="utf-8")
    fiscal = tmp_path / "fiscal.json"
    fiscal.write_text(json.dumps(annual_report_fixture({
        code: dict(fiscalMonth=12, series={"2025": value})
        for code, value in [("4452", 156), ("1111", 100), ("2222", 100), ("3333", 156)]
    })), encoding="utf-8")
    overrides = tmp_path / "overrides.json"
    overrides.write_text('{"schemaVersion": 1, "overrides": {}}', encoding="utf-8")
    summary = tmp_path / "summary.txt"
    command = [
        sys.executable, "-B", str(ROOT / "scripts" / "compare_forecast_resolution.py"),
        "--state", str(state), "--before-module", str(before / "build_store.py"),
        "--fiscal-dividends", str(fiscal), "--stock-actions", str(actions),
        "--stock-actions-extracted", str(empty), "--forecast-overrides", str(overrides),
        "--today", TODAY.isoformat(),
    ]
    env = dict(os.environ, GITHUB_STEP_SUMMARY=str(summary), PYTHONDONTWRITEBYTECODE="1")
    result = subprocess.run(command, text=True, capture_output=True, env=env)
    assert result.returncode == 0, result.stderr
    assert "予想を持つ会社: 4社" in result.stdout
    assert "| 4452 | 39 | raw | 78 | post_split_composed_ledger |" in result.stdout
    assert "1111: 比=0.5" in result.stdout and "2222: 比=" not in result.stdout
    assert "予想分割保留: 1社 / コード: 3333" in result.stdout
    assert summary.read_text(encoding="utf-8") == result.stdout + "\n"
    assert str(tmp_path) not in result.stdout + result.stderr
    # main取り込み後相当: 前後が同じscripts一式なら変更ゼロ。
    shutil.copytree(ROOT / "scripts", tmp_path / "same" / "scripts")
    command[command.index("--before-module") + 1] = str(tmp_path / "same" / "scripts" / "build_store.py")
    same = subprocess.run(command, text=True, capture_output=True, env=env)
    assert same.returncode == 0, same.stderr
    assert "変更: 0社" in same.stdout
    command[command.index("--state") + 1] = str(tmp_path / "unreadable-secret-host.json")
    failed = subprocess.run(command, text=True, capture_output=True, env=env)
    assert failed.returncode != 0
    assert "unreadable-secret-host" not in failed.stdout + failed.stderr
    assert str(tmp_path) not in failed.stdout + failed.stderr
