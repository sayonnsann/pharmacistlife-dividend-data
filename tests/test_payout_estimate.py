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
    annual = {"2013": 20, **{str(2014+i): 20*r for i, r in enumerate(ratios)}}
    return {"annual": annual, "eps": dict.fromkeys(annual, 100),
            "payoutRatioTotalBased": {str(2014+i): 20 for i in range(len(ratios))}}


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
    p["annual"]["2012"] = dps
    p["eps"]["2012"] = 100
    result = calculate(p)
    assert result.get("payoutRatioEstimated", {}).get("2012") == expected


def test_zero_total_conflict_both_zero_not_overlap_and_no_total():
    p = fixture()
    p["payoutRatioTotalBased"]["2016"] = 0
    assert calculate(p) == {}  # positive DPS vs zero total rejects company
    p["annual"]["2016"] = 0
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
    p["annual"]["2016"] = 1100
    p["payoutRatioTotalBased"]["2016"] = 1100
    result = calculate(p)
    assert "2016" not in result["payoutRatioDisplay"]
    assert p["payoutRatioTotalBased"]["2016"] == 1100
    p["annual"]["2016"] = 20
    assert calculate(p) == {}


def test_fiscal_year_exact_join_and_official_display_years_only():
    p = fixture()
    p["annual"]["2012"] = 20
    p["eps"]["2012_03"] = 100
    assert calculate(p)["payoutRatioEstimated"] == {"2013": 20}
    assert estimate_fields(p, {2014, 2015, 2016}) == {}
    p["annual"] = {"2012": 20, **{y: v for y, v in p["annual"].items() if y >= "2014"}}
    assert calculate(p) == {}  # EPS 2013 must not be shifted to 2012


@pytest.mark.parametrize("ratio,accepted", [
    (.5, True), (2, True), (.499999, False), (2.000001, False), (0, False),
])
def test_boundary_uses_unrounded_previous_year_ratio(ratio, accepted):
    p = fixture()
    p["annual"]["2013"] = 20 * ratio
    diagnostics = []
    result = estimate_fields(p, set(map(int, p["annual"])), diagnostics=diagnostics)
    assert bool(result) is accepted
    assert p["payoutRatioTotalBased"]["2014"] == 20
    if not accepted:
        assert diagnostics == [{"reason": "boundary_ratio", "years": ["2013", "2014"],
                                "ratio": ratio}]


@pytest.mark.parametrize("damage", ["missing", "loss", "zero_total"])
def test_unavailable_boundary_rejects_even_when_older_estimates_exist(damage):
    p = fixture()
    p["annual"]["2012"] = 20
    p["eps"]["2012"] = 100
    if damage == "missing": del p["annual"]["2013"]
    if damage == "loss": p["eps"]["2013"] = -100
    if damage == "zero_total":
        p["payoutRatioTotalBased"]["2014"] = p["annual"]["2014"] = 0
        p["payoutRatioTotalBased"]["2017"] = p["annual"]["2017"] = 20
        p["eps"]["2017"] = 100
    diagnostics = []
    assert estimate_fields(p, set(map(int, p["annual"])), diagnostics=diagnostics) == {}
    assert diagnostics == [{"reason": "boundary_unavailable", "years": ["2013", "2014"]}]


def test_seam_scan_holds_preserve_prior_reason_and_bps_only_is_not_added():
    holds = store.load_payout_estimate_holds(ROOT / "data/payout_estimate_holds.json")
    assert len(holds) == 109
    assert sum("seam_scan_20261005" in reason for reason in holds.values()) == 72
    assert "IR統合のつなぎ目のずれ(EPS)、seam_scan_20261005" in holds["7532"]
    assert "2011→2012" in holds["7532"]
    assert "provisional" in holds["5821"] and "seam_scan_20261005" in holds["5821"]
    assert "5940" not in holds  # BPSのみflag、EPSの基準不一致は検出されていない



def step_fixture(eps):
    p = fixture()
    p["eps"].update({str(y): v for y, v in eps.items()})
    p["annual"].update({str(y): 20 for y in eps})
    return p


@pytest.mark.parametrize("factor", [2, 3, 4, 5, 10])
@pytest.mark.parametrize("inverse", [False, True])
def test_split_like_eps_step_keeps_only_newer_side(factor, inverse):
    older = 100 / factor if inverse else 100 * factor
    p = step_fixture({2010: older, 2011: older, 2012: 100, 2013: 100})
    diagnostics = []
    original = copy.deepcopy(p)
    result = estimate_fields(p, set(map(int, p["annual"])), diagnostics=diagnostics)
    assert result["payoutRatioEstimated"] == {"2012": 20, "2013": 20}
    assert diagnostics[0]["reason"] == "eps_integer_step"
    assert diagnostics[0]["years"] == ["2011", "2012"]
    assert diagnostics[0]["integerMultiple"] == factor
    assert diagnostics[0]["removedYears"] == ["2010", "2011"]
    assert p == original


@pytest.mark.parametrize("ratio,cut", [
    (1.79, False), (1.93, False), (1.94, True), (2.06, True), (2.06001, False),
    (2.5, False), (4.84999, False), (4.85, True), (5.15, True), (5.15001, False),
])
def test_eps_step_integer_tolerance_is_inclusive_and_unrounded(ratio, cut):
    p = step_fixture({2011: 100 * ratio, 2012: 100, 2013: 100})
    assert ("2011" not in calculate(p)["payoutRatioEstimated"]) is cut


@pytest.mark.parametrize("implied,natural", [
    (.8, True), (1, True), (1.0349, True), (1.25, True),
    (.79999, False), (1.25001, False), (5, False),
])
@pytest.mark.parametrize("inverse", [False, True])
def test_integer_eps_change_explained_by_income_is_retained(implied, natural, inverse):
    older, newer = (100, 500) if inverse else (500, 100)
    p = step_fixture({2011: older, 2012: newer, 2013: 100})
    p["netIncome"] = {"2011": 1000000, "2012": 1000000 * newer / older * implied}
    # 2012→2013には検査対象の段差を作らない。
    p["eps"]["2013"] = newer
    p["annual"]["2013"] = newer * .2
    diagnostics = []
    result = estimate_fields(p, set(map(int, p["annual"])), diagnostics=diagnostics)
    assert ("2011" in result["payoutRatioEstimated"]) is natural
    assert diagnostics[0]["reason"] == ("eps_step_explained_by_income" if natural
                                         else "eps_integer_step")


@pytest.mark.parametrize("income", [{}, {"2011": None, "2012": 100},
                                    {"2011": -100, "2012": 20}])
def test_unusable_income_does_not_clear_split_suspicion(income):
    p = step_fixture({2011: 500, 2012: 100, 2013: 100})
    p["netIncome"] = income
    assert calculate(p)["payoutRatioEstimated"] == {"2012": 20, "2013": 20}


def test_multiple_steps_use_newest_cutoff_and_never_fill_gaps():
    p = step_fixture({2009: 1000, 2010: 1000, 2011: 500, 2012: 500, 2013: 100})
    assert calculate(p)["payoutRatioEstimated"] == {"2013": 20}
    p = step_fixture({2010: 500, 2012: 100, 2013: 100})
    assert "2010" in calculate(p)["payoutRatioEstimated"]  # 隔年を段差にしない
    p = step_fixture({2010: 500, 2011: 500, 2012: 100, 2013: 100})
    del p["annual"]["2012"]
    assert calculate(p)["payoutRatioEstimated"] == {"2013": 20}  # DPS欠損年のEPSも検査


def test_loss_to_profit_is_not_a_split_ratio():
    p = step_fixture({2010: 100, 2011: -500, 2012: 100, 2013: 100})
    assert calculate(p)["payoutRatioEstimated"] == {"2010": 20, "2012": 20, "2013": 20}


def test_7532_like_fivefold_old_eps_and_profit_growth():
    p = step_fixture({2010: 500, 2011: 500, 2012: 100, 2013: 100})
    p["netIncome"] = {"2011": 1000000, "2012": 1000000}
    assert calculate(p)["payoutRatioEstimated"] == {"2012": 20, "2013": 20}
    # 実7532の20.98→6.44は単純な5倍段差ではない。72社の保留が補う。
    p["eps"].update({"2011": 20.98, "2012": 6.44, "2013": 100})
    holds = store.load_payout_estimate_holds(ROOT / "data/payout_estimate_holds.json")
    fiscal = {"displayPolicy": store.POLICY_ID, "fiscalMonth": 6,
              "displayBasis": {"reliable": True}}
    assert not store.payout_estimate_allowed("7532", fiscal, None, holds)


def test_public_validator_rejects_years_older_than_detected_step():
    p = step_fixture({2011: 500, 2012: 100, 2013: 100})
    p.update(calculate(p))
    p["payoutRatioEstimated"]["2011"] = 4
    p["payoutRatioDisplay"]["2011"] = 4
    p["payoutRatioSource"]["2011"] = "dps_eps_estimate"
    with pytest.raises(ValueError):
        validate_public_payload(p, set(map(int, p["annual"])), set(map(int, p["annual"])),
                                payout_estimate_allowed=True)


def test_icda_profit_decline_is_not_mistaken_for_a_twofold_split():
    p = {"annual": dict.fromkeys(("2014", "2015", "2016", "2017", "2018"), 50),
         "eps": {"2014": 273.63, "2015": 137.18, "2016": 100, "2017": 100, "2018": 100},
         "netIncome": {"2014": 555242000, "2015": 288076000},
         "payoutRatioTotalBased": {"2016": 50, "2017": 50, "2018": 50}}
    diagnostics = []
    result = estimate_fields(p, set(map(int, p["annual"])), diagnostics=diagnostics)
    assert result["payoutRatioEstimated"] == {"2014": 18.27, "2015": 36.45}
    assert diagnostics[0]["reason"] == "eps_step_explained_by_income"
    assert diagnostics[0]["impliedShareRatio"] == pytest.approx(1.03489831577)


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
    document = annual_report_fixture({code: {"series": {str(y): 20 for y in (2012,2015,2016,2017,2018)},
                                   "externalYears": [2012], "fiscalMonth": 3}
                                      for code in ("7466", "4452", "6981", "7532")})
    fiscal = store.normalize_fiscal_dividends(document)
    fins = [{"code": c, "name": c, "eps": dict.fromkeys(document[c]["series"], 100),
             "dividendPerShare": {"2015": 999},
             "payoutRatioTotalBased": {str(y): 20 for y in (2016,2017,2018)},
             "basisTransitionMetrics": {"payoutRatioTotalBased": {"2015": 999}}} for c in document]
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
        if on[0] in ("4452", "6981", "7532"):
            assert off == on
        else:
            assert p["annual"]["2015"] == 10 and p["eps"]["2015"] == 50
            assert p["payoutRatioEstimated"] == {"2015": 20}
            assert p["payoutRatioDisplay"]["2016"] == 20
            assert {k: v for k, v in p.items() if k not in FIELDS} == json.loads(off[-1])
