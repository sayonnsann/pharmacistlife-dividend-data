import copy
import importlib.util
import json
import sqlite3
import subprocess
import sys
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import build_store as store
from payout_estimate import FIELDS, estimate_fields
from public_dividend_policy import validate_public_payload
from fiscal_fixtures import annual_report_fixture

ROOT = Path(__file__).resolve().parents[1]


def fixture(ratios=(1, 1, 1)):
    annual = {"2013": 20, **{str(2016+i): 20*r for i, r in enumerate(ratios)}}
    return {"annual": annual, "eps": dict.fromkeys(annual, 100),
            "payoutRatioTotalBased": {str(2016+i): 20 for i in range(len(ratios))}}


def calculate(payload):
    return estimate_fields(payload, {int(y) for y in payload["annual"] if len(y) == 4})


@pytest.mark.parametrize("ratios,accepted", [
    ((1, 1), False), ((1, 1, 1), True),
    ((.5, .8, 2), True), ((.5, 1.25, 2), True),
    ((.5, .79999, 2), False), ((.5, 1.25001, 2), False),
    ((.49999, 1, 1), False), ((1, 1, 2.00001), False),
    ((0, 1, 1), False), ((1, 1, 1, 1), True),
])
def test_unrounded_inclusive_ratio_and_overlap_boundaries(ratios, accepted):
    assert bool(calculate(fixture(ratios))) is accepted


def test_total_priority_no_interior_or_latest_fill_and_percent_units():
    p = fixture()
    p["annual"].update({"2019": 20, "2021": 20})
    p["eps"].update({"2019": 100, "2021": 100})
    p["payoutRatioTotalBased"]["2020"] = 23.45
    original = copy.deepcopy(p)
    result = calculate(p)
    assert result["payoutRatioEstimated"] == {"2013": 20.0}
    assert result["payoutRatioDisplay"] == {"2013": 20, **p["payoutRatioTotalBased"]}
    assert result["payoutRatioSource"]["2013"] == "dps_eps_estimate"
    assert result["payoutRatioSource"]["2020"] == "total_cash_paid"
    assert p == original


@pytest.mark.parametrize("eps", [0, -100, None, True, float("inf"), float("nan")])
def test_nonpositive_missing_and_nonfinite_eps_are_not_zero_filled(eps):
    p = fixture()
    p["eps"]["2013"] = eps
    assert calculate(p) == {}


@pytest.mark.parametrize("dps,expected", [(0, 0), (1000, 1000), (1000.00001, None), (-1, None)])
def test_raw_1000_cap_and_zero_dividend(dps, expected):
    p = fixture()
    p["annual"]["2013"] = dps
    p["eps"]["2013"] = 100
    result = calculate(p)
    assert result.get("payoutRatioEstimated", {}).get("2013") == expected


def test_zero_total_conflict_both_zero_not_overlap_and_no_total():
    p = fixture()
    p["payoutRatioTotalBased"]["2018"] = 0
    assert calculate(p) == {}  # positive DPS vs zero total rejects company
    p["annual"]["2018"] = 0
    assert calculate(p) == {}  # both zero cannot be the third comparison year
    p["annual"]["2019"] = 20
    p["eps"]["2019"] = 100
    p["payoutRatioTotalBased"]["2019"] = 20
    assert calculate(p)["payoutRatioEstimated"] == {"2013": 20}
    p["payoutRatioTotalBased"] = {}
    p["payoutRatioConsolidated"] = {"2016": 20, "2017": 20, "2018": 20}
    assert calculate(p) == {}


def test_total_over_1000_remains_in_ratio_test_then_only_display_is_capped():
    p = fixture()
    p["annual"]["2018"] = 1100
    p["payoutRatioTotalBased"]["2018"] = 1100
    result = calculate(p)
    assert "2018" not in result["payoutRatioDisplay"]
    assert p["payoutRatioTotalBased"]["2018"] == 1100
    p["annual"]["2018"] = 20
    assert calculate(p) == {}


def test_fiscal_year_exact_join_and_official_display_years_only():
    p = fixture()
    p["annual"]["2012"] = 20
    p["eps"]["2012_03"] = 100
    assert calculate(p)["payoutRatioEstimated"] == {"2013": 20}
    assert estimate_fields(p, {2016, 2017, 2018}) == {}
    p["annual"] = {"2014": 20, **{y: v for y, v in p["annual"].items() if y >= "2016"}}
    assert calculate(p) == {}  # EPS 2013 must not be shifted to 2014


def test_holds_and_private_basis_risks_are_company_wide():
    holds = store.load_payout_estimate_holds(ROOT / "data/payout_estimate_holds.json")
    for c in ("4452", "6981", "4825", "2897", "8316", "4231", "7126"):
        assert c in holds
    fiscal = {"displayPolicy": store.POLICY_ID, "fiscalMonth": 3,
              "streakReliable": False, "displayBasis": {"reliable": True}}
    assert store.payout_estimate_allowed("7466", fiscal, None, holds)
    for code in holds:
        assert not store.payout_estimate_allowed(code, fiscal, None, holds)
    for adj in ({"hasProvisional": True}, {"fallbacks": [{"field": "eps"}]},
                {"events": [{"status": "confirmed", "epsAdjustedByIssuer": None}]}):
        assert not store.payout_estimate_allowed("7466", fiscal, adj, holds)
    fiscal["displayBasis"]["reliable"] = False
    assert not store.payout_estimate_allowed("7466", fiscal, None, holds)


def test_missing_hold_file_fails_closed(tmp_path):
    with pytest.raises(FileNotFoundError):
        store.load_payout_estimate_holds(tmp_path / "missing.json")


@pytest.mark.parametrize("damage", ["external", "value", "source", "partial", "hold"])
def test_public_validator_rejects_unapproved_estimates(damage):
    p = fixture()
    p.update(calculate(p))
    years = {int(y) for y in p["annual"]}
    allowed = True
    if damage == "external": years.remove(2013)
    if damage == "value": p["payoutRatioEstimated"]["2013"] = 2000
    if damage == "source": p["payoutRatioSource"]["2013"] = "total_cash_paid"
    if damage == "partial": del p["payoutRatioDisplay"]
    if damage == "hold": allowed = False
    with pytest.raises(ValueError):
        validate_public_payload(p, years, years, payout_estimate_allowed=allowed)


def test_public_validator_requires_explicit_company_approval():
    p = fixture()
    p.update(calculate(p))
    years = {int(y) for y in p["annual"]}
    with pytest.raises(ValueError):
        validate_public_payload(p, years, years)
    validate_public_payload(p, years, years, payout_estimate_allowed=True)


def test_builder_adjusts_once_uses_official_annual_and_off_matches_head_exactly(tmp_path):
    baseline_path = tmp_path / "baseline.py"
    baseline_path.write_bytes(subprocess.check_output(["git", "show", "HEAD:scripts/build_store.py"], cwd=ROOT))
    spec = importlib.util.spec_from_file_location("payout_baseline", baseline_path)
    baseline = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(baseline)
    document = annual_report_fixture({code: {"series": {str(y): 20 for y in (2012,2013,2016,2017,2018)},
                                   "externalYears": [2012], "fiscalMonth": 3}
                                      for code in ("7466", "4452", "6981")})
    fiscal = store.normalize_fiscal_dividends(document)
    fins = [{"code": c, "name": c, "eps": dict.fromkeys(document[c]["series"], 100),
             "dividendPerShare": {"2013": 999},
             "payoutRatioTotalBased": {str(y): 20 for y in (2016,2017,2018)},
             "basisTransitionMetrics": {"payoutRatioTotalBased": {"2013": 999}}} for c in document]
    event = {"eventId": "split", "securityCode": "7466", "action": "split", "oldShares": 1,
             "newShares": 2, "effectiveDate": "2019-04-01", "status": "confirmed",
             "epsAdjustedByIssuer": False, "source": {"url": "https://example.com/split"}}
    rows = {}
    for label, module, enabled in (("head", baseline, False), ("off", store, False), ("on", store, True)):
        path = tmp_path / (label + ".sqlite")
        with patch.object(module, "REPOSITORY_ROOT", tmp_path), \
             patch.object(module, "PAYOUT_ESTIMATE_ENABLED", enabled, create=True), \
             patch.object(module, "load_daily_prices", return_value=({}, {}, "fixture")), \
             patch.object(module, "load_yield_split_adjustments", return_value=[]), \
             patch.object(module, "load_price_session_meta", return_value=None):
            module.create_database(path, fins, {}, {}, {}, [Path("fixture")]*4, "fixture.csv",
                                   stock_actions_by_code={"7466": [event]}, fiscal_by_code=fiscal,
                                   today=date(2026,10,6))
        with sqlite3.connect(path) as conn:
            rows[label] = conn.execute("SELECT * FROM stocks ORDER BY code").fetchall()
    assert rows["head"] == rows["off"]  # every column, including serialized payload bytes
    for off, on in zip(rows["off"], rows["on"]):
        assert off[:-1] == on[:-1]
        p = json.loads(on[-1])
        if on[0] in ("4452", "6981"):
            assert off == on
        else:
            assert p["annual"]["2013"] == 10 and p["eps"]["2013"] == 50
            assert p["payoutRatioEstimated"] == {"2013": 20}
            assert p["payoutRatioDisplay"]["2016"] == 20
            assert {k: v for k, v in p.items() if k not in FIELDS} == json.loads(off[-1])
