import copy
import json
import sqlite3
from datetime import date
from pathlib import Path
from unittest import mock

import pytest

from scripts import build_store


def split(effective_date="2026-07-01"):
    return {
        "eventId": "9999-split",
        "securityCode": "9999",
        "effectiveDate": effective_date,
        "oldShares": 1,
        "newShares": 2,
        "action": "split",
        "status": "provisional",
        "applyDividendAdjustment": True,
        "epsAdjustedByIssuer": None,
        "source": {"url": "https://example.com/split.pdf", "type": "issuer_ir"},
    }


def test_breakdown_uses_annual_ratio_and_preserves_source_without_mutation():
    breakdown = {
        "2024": {
            "base": 100.0, "special": 20.0, "kind": "記念",
            "fiscalYear": "2024-03", "evidence": {"quote": "普通配当100円"},
        },
        "2025": {"base": 120.0, "special": 0.0},
        "2026": {"base": 140.0},
        "invalid": {"base": 10.0, "special": "不明"},
    }
    original = copy.deepcopy(breakdown)
    adjustment = build_store.split_adjustment([split("2024-07-01")])
    result, warnings = build_store.adjust_dividend_breakdown(
        breakdown, {2024: 60.0, 2025: 120.0, 2026: 140.0}, adjustment, fiscal_month=3
    )
    assert result["2024"]["base"] == 50.0
    assert result["2024"]["special"] == 10.0
    assert result["2024"]["evidence"] == original["2024"]["evidence"]
    assert result["2024"]["kind"] == "記念"
    assert result["2024"]["fiscalYear"] == "2024-03"
    assert result["2025"] == original["2025"]
    assert result["2026"] == original["2026"]
    assert result["invalid"] == original["invalid"]
    assert breakdown == original
    assert warnings == []


def test_breakdown_uses_cumulative_factor_and_annual_rounding():
    second = {**split("2026-10-01"), "eventId": "second-split", "newShares": 3}
    result, warnings = build_store.adjust_dividend_breakdown(
        {"2025": {"base": 100.0, "special": 20.0}},
        {2025: 20.0},
        build_store.split_adjustment([split(), second]), fiscal_month=3,
    )
    assert result["2025"] == {"base": 16.6667, "special": 3.3333}
    assert warnings == []


@pytest.mark.parametrize("relative", [0.97, 1.0, 1.03])
def test_breakdown_within_three_percent_of_same_basis_is_unchanged(relative):
    breakdown = {"2024": {"base": 16.666666, "special": 0.0}}
    result, warnings = build_store.adjust_dividend_breakdown(
        breakdown, {2024: 16.666666 * relative}, None, fiscal_month=3
    )
    assert result == breakdown
    assert warnings == []


@pytest.mark.parametrize("ratio,accepted", [
    (0.5, True), (2.0, True), (0.485, True), (0.515, True),
    (0.484, False), (0.516, False), (0.7, False),
])
def test_integer_split_ratio_and_relative_tolerance(ratio, accepted):
    result, warnings = build_store.adjust_dividend_breakdown(
        {"2024": {"base": 80.0, "special": 20.0}},
        {2024: 100.0 * ratio}, None, fiscal_month=3, code="9999",
    )
    assert bool(result)
    assert bool(warnings) != accepted
    if accepted:
        assert sum(result["2024"].values()) == pytest.approx(100.0 * ratio)
    else:
        assert result == {"2024": {"base": 80.0, "special": 20.0}}
        assert "9999/2024" in warnings[0]
        assert f"比r={ratio}" in warnings[0]


@pytest.mark.parametrize("effective_date,accepted", [
    ("2024-03-31", False), ("2024-04-01", True),
])
def test_noninteger_ratio_requires_event_after_period_end(effective_date, accepted):
    event = {**split(effective_date), "oldShares": 3, "newShares": 5}
    result, warnings = build_store.adjust_dividend_breakdown(
        {"2024": {"base": 80.0, "special": 20.0}}, {2024: 60.0},
        build_store.split_adjustment([event]), fiscal_month=3,
    )
    assert bool(result)
    assert bool(warnings) != accepted
    if not accepted:
        assert result == {"2024": {"base": 80.0, "special": 20.0}}


def test_applied_actions_and_ledger_are_deduplicated_in_cumulative_ratio():
    event = {**split(), "oldShares": 3, "newShares": 5}
    applied = [
        {"eventId": "producer", "effectiveDate": "2026-07-01", "ratio": 5 / 3},
        {"eventId": "second", "effectiveDate": "2026-10-01", "ratio": 1.5},
    ]
    result, warnings = build_store.adjust_dividend_breakdown(
        {"2025": {"base": 80.0, "special": 20.0}}, {2025: 40.0},
        build_store.split_adjustment([event]), fiscal_month=3,
        applied_actions=applied,
    )
    assert result["2025"] == {"base": 32.0, "special": 8.0}
    assert warnings == []


@pytest.mark.parametrize("detail,annual", [
    ({"base": 0.0, "special": 0.0}, 10.0),
    ({"base": -1.0, "special": 20.0}, 10.0),
    ({"base": None, "special": 20.0}, 10.0),
    ({"base": 20.0, "special": 10.0}, 0.0),
])
def test_unexplained_breakdown_is_kept_for_legacy_replacement(detail, annual):
    result, warnings = build_store.adjust_dividend_breakdown(
        {"2024": detail}, {2024: annual}, None, fiscal_month=3,
    )
    assert result == {"2024": detail}
    assert len(warnings) == 1
    assert "内訳は未補正のまま使用" in warnings[0]
    base = build_store.finite_number(detail["base"])
    expected = base if base is not None and base >= 0 else annual
    assert build_store.base_dividend_series({2024: annual}, result) == {2024: expected}


def test_zero_total_and_zero_annual_are_kept():
    detail = {"2024": {"base": 0.0, "special": 0.0}}
    assert build_store.adjust_dividend_breakdown(
        detail, {2024: 0.0}, None, fiscal_month=3
    ) == (detail, [])


@pytest.mark.parametrize("basis,already_applied", [
    ("fiscal", False), ("fiscal", True), ("calendar", False),
])
@pytest.mark.parametrize("breakdown_already_applied,unexplained", [
    (False, False), (True, False), (False, True),
])
def test_store_uses_same_breakdown_for_streaks_and_display(
    tmp_path, already_applied, basis, breakdown_already_applied, unexplained
):
    series = {2023: 100.0, 2024: 120.0, 2025: 100.0}
    detail = {"base": 100.0, "special": 20.0, "kind": "記念"}
    if already_applied:
        series = {year: value / 2 for year, value in series.items()}
    if breakdown_already_applied:
        detail = {**detail, "base": 50.0, "special": 10.0}
    if unexplained:
        detail = {**detail, "base": 100.0, "special": 0.0}
    fiscal = {
        "series": series, "fiscalMonth": 3, "connectionStatus": "edinet_only",
        "connectionReason": "", "externalSource": None, "externalYears": [],
        "appliedActions": ([{
            "eventId": "producer-format", "effectiveDate": "2026-07-01", "ratio": 2,
        }] if already_applied else []),
    }
    root = tmp_path / "repo"
    (root / "data").mkdir(parents=True)
    (root / "data/dividend_breakdown.json").write_text(
        json.dumps({"9999": {"2024": detail}}), encoding="utf-8"
    )
    original = copy.deepcopy(fiscal)
    output = tmp_path / "store.sqlite"
    with mock.patch.object(build_store, "REPOSITORY_ROOT", root), mock.patch.object(
        build_store, "load_daily_prices",
        return_value=({"9999": 1000.0}, {"9999": 100.0}, "2026-08-05"),
    ):
        build_store.create_database(
            output, [{"code": "9999", "name": "テスト"}], {}, {}, {},
            [Path("f"), Path("s"), Path("t"), Path("fc")],
            "fixture.csv", {"9999": [split()]}, Path("actions.json"),
            {"9999": fiscal} if basis == "fiscal" else {}, None,
            {"9999": {"series": series}} if basis == "calendar" else {}, None,
            today=date(2026, 8, 5),
        )
    with sqlite3.connect(output) as connection:
        payload = json.loads(connection.execute("SELECT payload FROM stocks").fetchone()[0])
    assert payload["annual"] == {"2023": 50.0, "2024": 60.0, "2025": 50.0}
    if unexplained:
        assert payload["dividendBreakdown"] == {"2024": detail}
        assert payload["streakNoDecreaseBase"] == 0
        assert "9999/2024" in payload["warnings"][0]
        assert "比r=0.6" in payload["warnings"][0]
    else:
        assert payload["dividendBreakdown"]["2024"] == {
            "base": 50.0, "special": 10.0, "kind": "記念",
        }
        assert payload["streakNoDecreaseBase"] == 2
    assert payload["streakBase"] == 0
    assert fiscal == original
