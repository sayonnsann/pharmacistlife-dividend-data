import copy
from pathlib import Path
import re
import shutil
import subprocess
import sys
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from financial_display import prepare_financial_display
from public_dividend_policy import validate_public_payload


def marked(decision="connect", field="roe", note="この年は単体決算"):
    return {"financialBasisByYear": {"2016": {"basis": "nonConsolidated", "decision": decision,
            "note": note, "fields": [field]}}, "basisSwitchMetrics": {field: {"2016": 10}}}


def test_store_preserves_annotations_and_removes_unmarked_duplicate():
    source = marked()
    source.update({"roe": {"2016": 999, "2017": 12}, "basisTransitionMetrics": {"roe": {"2016": 888}}})
    original = copy.deepcopy(source)
    out = prepare_financial_display(source)
    assert out["roe"] == {"2017": 12} and out["basisTransitionMetrics"]["roe"] == {}
    assert out["basisSwitchMetrics"]["roe"] == {"2016": 10}
    assert source == original
    validate_public_payload(out, set(), set(), financial_years={})
    out["annual"] = {"2016": 10}
    with pytest.raises(ValueError):
        validate_public_payload(out, set(), set(), financial_years={})


@pytest.mark.parametrize("decision", ["hide", "review"])
def test_store_discards_hidden_overlay(decision):
    assert prepare_financial_display(marked(decision))["basisSwitchMetrics"]["roe"] == {}


@pytest.mark.parametrize("note", [None, "", "   "])
def test_store_rejects_values_without_note(note):
    with pytest.raises(ValueError, match="注記"):
        prepare_financial_display(marked(note=note))


def test_store_rejects_dividend_alias_in_financial_overlay():
    with pytest.raises(ValueError, match="項目"):
        prepare_financial_display(marked(field="dividendPerShare"))


def run_checker_js(tmp_path, assertions):
    html = (Path(__file__).resolve().parents[1] / "serving/checker.html").read_text()
    script = "\n".join(re.findall(r"<script[^>]*>(.*?)</script>", html, re.S))
    assert "  init()\n" in script
    script = 'var window={frameElement:null};\n' + script.replace("  init()\n", assertions + "\n")
    path = tmp_path / "checker.js"
    path.write_text(script)
    jsc = Path("/System/Library/Frameworks/JavaScriptCore.framework/Versions/A/Helpers/jsc")
    node = shutil.which("node")
    engine = [node] if node else [str(jsc)] if jsc.exists() else None
    if engine is None:
        pytest.skip("JavaScript engine unavailable")
    result = subprocess.run(engine + [str(path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


def test_checker_period_order_labels_and_gap_axis(tmp_path):
    run_checker_js(tmp_path, r'''
    function check(ok,msg){if(!ok)throw new Error(msg)}
    var periods={"2010_09":{fiscalYearEnd:"2010-09-30",label:"2010/9"},"2010":{fiscalYearEnd:"2010-12-31",label:"2010/12",periodLabel:"3か月決算"}};
    check(JSON.stringify(financialAxisYears(["2012","2010","2010_09"],periods))==='["2010_09","2010","2011","2012"]',"actual period order and missing year");
    check(JSON.stringify(validYears({"2010":2,"2010_09":1},periods))==='["2010_09","2010"]',"same-year periods not conflated");
    check(latest({"2010":2,"2010_09":1},periods).value===2,"latest actual end");
    var html=chart([{label:"売上",data:{"2010_09":10,"2010":20,"2012":30},color:"blue"}],"億円","bar","売上",periods);
    check(!/NaN|Infinity/.test(html),"finite chart geometry");
    check(html.indexOf("2010/9")<html.indexOf("2010/12"),"period labels ordered");
    check(html.indexOf("3か月決算")>=0,"ir duration retained");
    var n=chart([{label:"売上",data:{"2006_03":10,"2006":20},color:"blue"}],"億円","line","売上",{"2006_03":{fiscalYearEnd:"2006-03-31",label:"2006/3"},"2006":{fiscalYearEnd:"2006-12-31",label:"2006/12"}});
    check(!/NaN|Infinity/.test(n)&&n.indexOf("2006/3")<n.indexOf("2006/12"),"5950 periods");
    check(!/NaN/.test(trendSummary({"2010_09":10,"2010":20},"円")),"finite trend");
    check(buildBarChart({"2024":10,"2025":20},{},{},{},null,{},{}) .indexOf("NaN")<0,"dividend chart unaffected");
    ''')


def test_checker_connect_separate_hide_and_singleton_notes(tmp_path):
    run_checker_js(tmp_path, r'''
    function check(ok,msg){if(!ok)throw new Error(msg)}
    function company(decision){return {roe:{"2015":9,"2016":999,"2017":11},basisTransition:{fiscalYear:"2017"},basisTransitionMetrics:{roe:{"2015":9,"2016":888,"2017":11}},financialBasisByYear:{"2016":{decision:decision,basis:"nonConsolidated",note:"この年は単体決算",fields:["roe"]}},basisSwitchMetrics:{roe:{"2016":10}}}}
    function metric(c){c=financialDisplayCompany(c);return miniMetric("ROE",c.roe,"%","line","blue","説明",null,c,"roe")}
    var c=metric(company("connect")),s=metric(company("separate"));
    check((c.match(/<polyline/g)||[]).length===1&&c.indexOf("単体決算")>=0,"connect continuous and annotated");
    check((s.match(/<polyline/g)||[]).length===0&&s.indexOf("#d97706")>=0&&s.indexOf("単体決算")>=0,"separate singleton and normal line break");
    var multi=company("separate");multi.roe={"2014":8,"2015":9,"2016":999,"2017":11,"2018":12};delete multi.basisTransition;
    var sm=metric(multi);check((sm.match(/<polyline/g)||[]).length===2,"normal lines remain on both sides");
    ["hide","review"].forEach(function(d){var out=financialDisplayCompany(company(d));check(!("2016" in out.roe),"hidden legacy bypass "+d)});
    var one=company("connect");one.roe={};delete one.basisTransition;delete one.basisTransitionMetrics;
    check(metric(one).indexOf("単体決算")>=0,"single period annotated");
    var bad=company("connect");bad.financialBasisByYear["2016"].note="";check(!("2016" in financialDisplayCompany(bad).roe),"no-note overlay never emerges");
    var fc={financialBasisByYear:{"2016":{note:"単体CF",fields:["operatingCashFlow","investingCashFlow"]}}};
    check(financialMetricNotes(fc,"freeCashFlow",{"2016":1}).indexOf("単体CF")>=0,"derived FCF inherits basis note");
    ''')


def test_sqlite_preserves_periods_notes_and_adjusts_integrated_eps(tmp_path, monkeypatch):
    import json
    import sqlite3
    import build_store as store
    from datetime import date
    from fiscal_fixtures import annual_report_fixture
    root = tmp_path / "repo"
    (root / "data").mkdir(parents=True)
    monkeypatch.setattr(store, "REPOSITORY_ROOT", root)
    monkeypatch.setattr(store, "load_daily_prices", lambda *a: ({"9999": 1000}, {}, "fixture"))
    monkeypatch.setattr(store, "load_price_session_meta", lambda *a: None)
    monkeypatch.setattr(store, "load_yield_split_adjustments", lambda *a: [])
    financial = marked(field="eps")
    financial.update({"code": "9999", "name": "fixture", "eps": {"2016": 999, "2017": 20},
                      "payoutRatioTotalBased": {"2016": 99, "2017": 30},
                      "financialPeriods": {"2006_03": {"fiscalYearEnd": "2006-03-31", "label": "2006/3"}}})
    original = copy.deepcopy(financial)
    action = {"eventId": "split", "securityCode": "9999", "action": "split", "effectiveDate": "2026-04-01",
              "oldShares": 1, "newShares": 2, "status": "confirmed", "applyDividendAdjustment": True,
              "epsAdjustedByIssuer": False, "source": {"url": "https://issuer.example/split.pdf"}}
    fiscal = annual_report_fixture({"9999": {"fiscalMonth": 3, "series": {2025: 10, 2026: 12}}})
    path = tmp_path / "fixture.sqlite"
    store.create_database(path, [financial], {}, {}, {}, [Path("fixture")] * 4, "fixture.csv",
                          fiscal_by_code=fiscal, stock_actions_by_code={"9999": [action]}, today=date(2026, 10, 15))
    with sqlite3.connect(path) as conn:
        payload = json.loads(conn.execute("SELECT payload FROM stocks").fetchone()[0])
    assert payload["basisSwitchMetrics"]["eps"] == {"2016": 5}
    assert payload["eps"] == {"2017": 10}
    assert payload["payoutRatio"] == {"2017": 30}
    assert payload["financialBasisByYear"] == financial["financialBasisByYear"]
    assert payload["financialPeriods"] == financial["financialPeriods"]
    assert financial == original


def dated_eps():
    financial = marked(field="eps")
    financial["basisSwitchPerShareBasis"] = {"docID": "S100BASE", "source": "base.csv",
        "fiscalYearEnd": "2026-03-31", "filingDate": "2026-06-01"}
    financial["financialBasisByYear"]["2016"]["perShare"] = {"eps": {
        "status": "shown", "sourceDocID": "S100PAST", "source": "past.csv",
        "sourceFilingDate": "2017-06-01", "sourceValue": 20, "factor": 0.5,
        "targetFilingDate": "2026-06-01", "events": [
            {"eventId": "prior", "effectiveDate": "2025-04-01", "factor": 0.5}]}}
    return financial


@pytest.mark.parametrize("mutation", ["future_source", "wrong_target", "wrong_value", "withheld", "no_proof",
                                       "no_basis", "no_events", "bad_factor", "duplicate", "outside_window"])
def test_store_rejects_mixed_basis_or_unproven_per_share_values(mutation):
    financial = dated_eps()
    proof = financial["financialBasisByYear"]["2016"]["perShare"]["eps"]
    if mutation == "future_source":
        proof["sourceFilingDate"] = "2027-06-01"
    elif mutation == "wrong_target":
        proof["targetFilingDate"] = "2025-06-01"
    elif mutation == "wrong_value":
        financial["basisSwitchMetrics"]["eps"]["2016"] = 5
    elif mutation == "withheld":
        proof["status"] = "withheld"
    elif mutation == "no_basis":
        financial.pop("basisSwitchPerShareBasis")
    elif mutation == "no_events":
        proof.pop("events")
    elif mutation == "bad_factor":
        proof["events"][0]["factor"] = 0.25
    elif mutation == "duplicate":
        proof["events"].append(dict(proof["events"][0]))
    elif mutation == "outside_window":
        proof["events"][0]["effectiveDate"] = "2017-06-01"
    else:
        financial["financialBasisByYear"]["2016"].pop("perShare")
    with pytest.raises(ValueError, match="証跡"):
        prepare_financial_display(financial)


@pytest.mark.parametrize("late_old,late_new,include_at_filing", [(1, 2, True), (2, 1, True), (2, 1, False)])
def test_sqlite_applies_only_events_after_shared_filing_basis(tmp_path, monkeypatch, late_old, late_new, include_at_filing):
    import json
    import sqlite3
    import build_store as store
    from datetime import date
    from fiscal_fixtures import annual_report_fixture
    root = tmp_path / "repo"
    (root / "data").mkdir(parents=True)
    monkeypatch.setattr(store, "REPOSITORY_ROOT", root)
    monkeypatch.setattr(store, "load_daily_prices", lambda *a: ({"9999": 1000}, {}, "fixture"))
    monkeypatch.setattr(store, "load_price_session_meta", lambda *a: None)
    monkeypatch.setattr(store, "load_yield_split_adjustments", lambda *a: [])
    financial = dated_eps()
    financial.update(code="9999", name="fixture", eps={"2016": 999, "2017": 20, "2006_03": 8},
                     dividendPerShare={"2017": 100})
    original = copy.deepcopy(financial)
    def action(event_id, effective, old, new):
        return {"eventId": event_id, "securityCode": "9999", "action": "split" if new > old else "consolidation",
            "effectiveDate": effective, "oldShares": old, "newShares": new, "status": "confirmed",
            "applyDividendAdjustment": True, "epsAdjustedByIssuer": False,
            "source": {"url": "https://issuer.example/split.pdf"}}
    actions = [action("prior", "2025-04-01", 1, 2), action("later", "2026-07-01", late_old, late_new)]
    if include_at_filing:
        actions.append(action("at-filing", "2026-06-01", 1, 2))
    fiscal = annual_report_fixture({"9999": {"fiscalMonth": 3, "series": {2025: 10, 2026: 12}}})
    path = tmp_path / "fixture.sqlite"
    store.create_database(path, [financial], {}, {}, {}, [Path("fixture")] * 4, "fixture.csv",
                          fiscal_by_code=fiscal, stock_actions_by_code={"9999": actions}, today=date(2026, 10, 15))
    with sqlite3.connect(path) as conn:
        payload = json.loads(conn.execute("SELECT payload FROM stocks").fetchone()[0])
    factor = late_old / late_new
    assert payload["basisSwitchMetrics"]["eps"] == {"2016": 10 * factor}
    assert payload["eps"] == {"2017": 20 * factor, "2006_03": 8 * factor}
    assert payload["financialBasisByYear"] == financial["financialBasisByYear"]
    assert financial == original


def test_eps_events_with_unit_total_factor_still_use_the_filing_window():
    import build_store as store
    adjustment = {"epsBpsFactor": 1, "events": [
        {"status": "confirmed", "epsAdjustedByIssuer": False, "effectiveDate": "2025-04-01", "adjustmentFactor": 0.5},
        {"status": "confirmed", "epsAdjustedByIssuer": False, "effectiveDate": "2026-07-01", "adjustmentFactor": 2},
    ]}
    assert store.adjust_per_share_series({"2016": 10}, adjustment, field="epsBps", basis_date="2026-06-01") == {"2016": 20}


def test_checker_eps_bps_receive_standalone_notes(tmp_path):
    run_checker_js(tmp_path, r'''
    var c=financialDisplayCompany({code:"8194",eps:{"2015":53.96,"2017":86.61},bps:{"2015":560.17,"2017":675.77},
      financialBasisByYear:{"2016":{decision:"connect",basis:"nonConsolidated",note:"この年は単体決算の値です。",fields:["eps","bps"]}},
      basisSwitchMetrics:{eps:{"2016":84.62},bps:{"2016":617.735}}});
    ["eps","bps"].forEach(function(field){
      var html=miniMetric(field,c[field],"円","line","blue","説明",null,c,field);
      if(html.indexOf("2016年: この年は単体決算")<0)throw new Error(field+" lacks standalone note");
      if((html.match(/<polyline/g)||[]).length!==1)throw new Error(field+" not connected");
      if(/NaN|Infinity/.test(html))throw new Error("invalid geometry");
    });
    ''')


def value_basis_eps(*, partial=True):
    financial = dated_eps()
    mark = financial['financialBasisByYear']['2016']
    mark['basisDate'] = '2026-06-01'
    proof = mark['perShare']['eps']
    if partial:
        proof['sourceFilingDate'] = '2025-06-01'
    proof['valueBasis'] = {'observationId': 'audited-cell', 'status': 'determined',
        'reflectedEventIds': ['already-reflected'],
        'targetEventIds': ['already-reflected', 'prior'], 'appliedEventIds': ['prior']}
    return financial


@pytest.mark.parametrize('field', ['eps', 'bps'])
@pytest.mark.parametrize('partial', [True, False])
def test_store_accepts_complete_value_basis_for_both_event_windows(field, partial):
    financial = value_basis_eps(partial=partial)
    if field == 'bps':
        financial['basisSwitchMetrics']['bps'] = financial['basisSwitchMetrics'].pop('eps')
        mark = financial['financialBasisByYear']['2016']
        mark['fields'] = ['bps']
        mark['perShare']['bps'] = mark['perShare'].pop('eps')
    original = copy.deepcopy(financial)
    assert prepare_financial_display(financial)['basisSwitchMetrics'][field]['2016'] == 10
    assert financial == original


@pytest.mark.parametrize('partial', [True, False])
@pytest.mark.parametrize('mutation', [
    'applied_mismatch', 'extra_event', 'undetermined', 'already_reflected', 'product',
    'missing_observation', 'missing_s', 'missing_t', 'missing_applied', 'null_basis',
    'empty_basis', 'missing_status', 'duplicate_s', 'duplicate_t', 'duplicate_applied',
    'wrong_difference', 'reverse', 'held', 'missing_date', 'wrong_date', 'null_date',
    'missing_event_date', 'missing_event_factor', 'future_event', 'future_source',
    'wrong_target', 'wrong_value', 'withheld', 'no_source', 'no_doc', 'no_global_basis',
    'bool_factor', 'nan_factor', 'zero_factor', 'negative_factor', 'duplicate_events',
    'bad_event_id', 'bad_s_type', 'bad_s_id', 'bad_event_type', 'bad_events_type',
])
def test_store_rejects_incomplete_or_inconsistent_value_basis(partial, mutation):
    financial = value_basis_eps(partial=partial)
    mark = financial['financialBasisByYear']['2016']
    proof = mark['perShare']['eps']
    basis = proof['valueBasis']
    if mutation == 'applied_mismatch': basis['appliedEventIds'] = []
    elif mutation == 'extra_event': proof['events'].append({'eventId': 'extra', 'effectiveDate': '2026-01-01', 'factor': 1})
    elif mutation == 'undetermined': basis['status'] = 'review'
    elif mutation == 'already_reflected': basis['reflectedEventIds'].append('prior')
    elif mutation == 'product': proof['events'][0]['factor'] = .25
    elif mutation == 'missing_observation': basis.pop('observationId')
    elif mutation == 'missing_s': basis.pop('reflectedEventIds')
    elif mutation == 'missing_t': basis.pop('targetEventIds')
    elif mutation == 'missing_applied': basis.pop('appliedEventIds')
    elif mutation == 'null_basis': proof['valueBasis'] = None
    elif mutation == 'empty_basis': proof['valueBasis'] = {}
    elif mutation == 'missing_status': basis.pop('status')
    elif mutation.startswith('duplicate_') and mutation != 'duplicate_events':
        key = {'duplicate_s': 'reflectedEventIds', 'duplicate_t': 'targetEventIds', 'duplicate_applied': 'appliedEventIds'}[mutation]
        basis[key].append(basis[key][0])
    elif mutation == 'wrong_difference': basis['targetEventIds'].append('unapplied')
    elif mutation == 'reverse': basis['reflectedEventIds'].append('outside-target')
    elif mutation == 'held': basis['holdReason'] = 'ledger_revision_requires_review'
    elif mutation == 'missing_date': mark.pop('basisDate')
    elif mutation == 'wrong_date': mark['basisDate'] = '2026-05-01'
    elif mutation == 'null_date': mark['basisDate'] = None
    elif mutation == 'missing_event_date': proof['events'][0].pop('effectiveDate')
    elif mutation == 'missing_event_factor': proof['events'][0].pop('factor')
    elif mutation == 'future_event': proof['events'][0]['effectiveDate'] = '2026-06-02'
    elif mutation == 'future_source': proof['sourceFilingDate'] = '2027-01-01'
    elif mutation == 'wrong_target': proof['targetFilingDate'] = '2026-05-01'
    elif mutation == 'wrong_value': financial['basisSwitchMetrics']['eps']['2016'] = 11
    elif mutation == 'withheld': proof['status'] = 'withheld'
    elif mutation == 'no_source': proof.pop('source')
    elif mutation == 'no_doc': proof.pop('sourceDocID')
    elif mutation == 'no_global_basis': financial.pop('basisSwitchPerShareBasis')
    elif mutation == 'bool_factor': proof['events'][0]['factor'] = True
    elif mutation == 'nan_factor': proof['events'][0]['factor'] = float('nan')
    elif mutation == 'zero_factor': proof['events'][0]['factor'] = 0
    elif mutation == 'negative_factor': proof['events'][0]['factor'] = -.5
    elif mutation == 'duplicate_events': proof['events'].append(dict(proof['events'][0]))
    elif mutation == 'bad_event_id': proof['events'][0]['eventId'] = []
    elif mutation == 'bad_s_type': basis['reflectedEventIds'] = 'already-reflected'
    elif mutation == 'bad_s_id': basis['reflectedEventIds'] = [None]
    elif mutation == 'bad_event_type': proof['events'] = [None]
    elif mutation == 'bad_events_type': proof['events'] = {}
    else: raise AssertionError(mutation)
    with pytest.raises(ValueError, match='証跡'):
        prepare_financial_display(financial)


def test_store_partial_restatement_still_requires_value_basis_and_complete_unit_proof():
    financial = value_basis_eps()
    proof = financial['financialBasisByYear']['2016']['perShare']['eps']
    proof.pop('valueBasis')
    with pytest.raises(ValueError, match='証跡'):
        prepare_financial_display(financial)
    proof['valueBasis'] = {'status': 'determined', 'observationId': 'unit-cell',
        'reflectedEventIds': ['prior'], 'targetEventIds': ['prior'], 'appliedEventIds': []}
    proof.update(events=[], factor=1, sourceValue=10)
    assert prepare_financial_display(financial)['basisSwitchMetrics']['eps']['2016'] == 10
    proof['valueBasis']['appliedEventIds'] = ['prior']
    with pytest.raises(ValueError, match='証跡'):
        prepare_financial_display(financial)
