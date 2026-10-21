import copy
import json
import sqlite3
from datetime import date
from pathlib import Path
from unittest.mock import patch
import pytest
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import build_store as store
from public_dividend_policy import POLICY_ID, validate_public_payload, financial_dividend_projection
from fiscal_fixtures import annual_report_fixture


def build(tmp_path, *, display=None, empty=False, forecast=None, unreliable=False,
          yuho=None, actions=None, applied_actions=None, calculation=None, fiscal_month=3):
    calculation = {} if empty else ({2000: 10, 2001: 20, 2002: 30, 2003: 40}
                                    if calculation is None else calculation)
    display = {2002: 30, 2003: 40} if display is None else display
    record = {'series': calculation, 'streakSeries': calculation,
              'displaySeries': display if not empty else {}, 'displayPolicy': POLICY_ID,
              'fiscalMonth': fiscal_month, 'externalYears': sorted(set(calculation)-set(display)), 'externalSource': 'external',
              'reportedYears': sorted(calculation), 'connectionStatus': 'scaled',
              'connectionReason': 'PRIVATE_DETAIL', 'streakReliable': not unreliable}
    if applied_actions is not None:
        record['appliedActions'] = applied_actions
    if yuho:
        record['yuhoDisplayYears'] = {y: dict(
            {'converted': display[y], 'status':'adopt'},
            **(a if isinstance(a, dict) else
               {'asOf': a, 'basis': '実額', 'appliedEvents': []})) for y, a in yuho.items()}
        record['externalYears'] = sorted(set(calculation)-set(display) | set(yuho))
    root = tmp_path/'repo';(root/'data').mkdir(parents=True)
    (root/'data/dividend_breakdown.json').write_text(json.dumps({'9999': {
        '2000': {'base': 5, 'special': 5}, '2002': {'base': 25, 'special': 5}}}))
    financial = {'code': '9999', 'name': 'fixture', 'annual': {'2000': 123456.789},
                 'dividendPerShare': {'2000': 123456.789, '2003': 40},
                 'basisTransitionMetrics': {'dividendPerShare': {'2000': 123456.789}},
                 'annualPartial': {'2000': 123456.789},
                 'displayProvenance': {'dividendPerShare': {'2000': {'sourceCategory': 'e'}},
                    'basisTransitionMetrics': {'dividendPerShare': {'2000': {'sourceCategory': 'e'}}}},
                 'confirmedDividend': 123456.789, 'confirmedFiscalYearEnd':'2000-03-31'}
    out=tmp_path/'public.sqlite'
    with patch.object(store,'REPOSITORY_ROOT',root), patch.object(store,'load_daily_prices',
            return_value=({'9999':1000},{'9999':123456.789},'fixture')), \
         patch.object(store,'load_price_session_meta',return_value=None), \
         patch.object(store,'load_yield_split_adjustments',return_value=[]):
        store.create_database(out,[financial],{},{},{'9999': forecast or {}},[Path('fixture')]*4,'fixture.csv',
                              fiscal_by_code={'9999':record},
                              stock_actions_by_code=actions,
                              calendar_by_code={'9999':{'series':{2000:10,2001:20}}},today=date(2004,10,5))
    with sqlite3.connect(out) as conn:
        payload=json.loads(conn.execute('SELECT payload FROM stocks').fetchone()[0])
    return payload, record


def test_external_yearly_values_never_reach_public_payload_or_aliases(tmp_path):
    forecast={'confirmedDividend':123456.789,'confirmedFiscalYearEnd':'2000-03-31',
              'forecastDividend':50,'forecastFiscalYear':2004,'forecastPeriod':'2004年3月期(予)'}
    payload,record=build(tmp_path,forecast=forecast)
    assert payload['annual']=={'2002':30,'2003':40}
    assert payload['dividendPerShare']=={'2003':40}
    assert payload['basisTransitionMetrics']['dividendPerShare']=={}
    assert set(payload['dividendBreakdown'])=={'2002'}
    assert '2000' not in payload['annualPartial']
    assert payload['annualPartial']=={'2004':50}
    assert 'confirmedDividend' not in payload
    assert '123456.789' not in json.dumps(payload)
    assert 'PRIVATE_DETAIL' not in json.dumps(payload)
    assert 'streakSeries' not in json.dumps(payload)
    assert payload['streakIncrease']==3
    assert payload['streakNonDecrease']==3
    expected=store.base_dividend_series(record['streakSeries'],{'2000':{'base':5},'2002':{'base':25}})
    stats=store.fiscal_dividend_stats(expected)
    assert payload['streakBase']==stats['streakIncrease']
    assert payload['streakNoDecreaseBase']==stats['streakNonDecrease']
    assert payload['cagr3'] is None
    assert payload['dividendYield']==4
    assert payload['dividendSeries']['hiddenYearsArePrefix']


def test_interior_hole_and_empty_display_preserve_calculation(tmp_path):
    p,_=build(tmp_path/'hole',display={2000:10,2002:30,2003:40})
    assert not p['dividendSeries']['hiddenYearsArePrefix']
    assert p['streakIncrease']==3
    p,_=build(tmp_path/'empty',display={})
    assert p['annual']=={} and p['dividendPerShare']=={'2003':40} and p['dividendBreakdown']=={}
    assert p['streakIncrease']==3 and p['dividendYield'] is None


def test_empty_fiscal_uses_calendar_only_for_calculation(tmp_path):
    p,_=build(tmp_path,empty=True)
    assert p['annual']=={} and p['dividendYield'] is None
    assert p['streakIncrease']==1
    assert p['dividendSeries']['calculationStartYear']==2000


def test_streak_reliability_is_unchanged(tmp_path):
    p,_=build(tmp_path,unreliable=True)
    assert p['streakIncrease'] is None and p['streakNonDecrease'] is None


def test_official_financial_dps_is_independent_of_external_fiscal_dividend_year():
    financial = {'dividendPerShare': {'2000': 9},
                 'basisTransitionMetrics': {'dividendPerShare': {'2000': 8}}}
    assert financial_dividend_projection(financial) == financial
    validate_public_payload(financial, {2003}, {2000, 2003},
                            financial_years={'main': {2000}, 'basisTransitionMetrics': {2000}})


@pytest.mark.parametrize('category,document_type',[('d','other'),('f','unknown')])
def test_official_documents_and_unknown_ir_type_are_displayed(tmp_path,category,document_type):
    document=annual_report_fixture({'9999':{'series':{'2000':9}}})
    document['9999']['yearProvenance']['2000'].update(sourceCategory=category,documentType=document_type)
    path=tmp_path/'private.json';path.write_text(json.dumps(document))
    assert store.load_fiscal_dividends(path)['9999']['displaySeries']=={2000:9}


def test_loader_fails_closed_on_legacy_or_poisoned_display(tmp_path):
    path=tmp_path/'private.json'
    legacy={'9999':{'series':{'2000':123456.789}}}
    path.write_text(json.dumps(legacy))
    with pytest.raises(ValueError,match='表示方針'):store.load_fiscal_dividends(path)
    migrated=annual_report_fixture(legacy)
    migrated['9999']['yearProvenance']['2000']['sourceCategory']='e'
    path.write_text(json.dumps(migrated))
    with pytest.raises(ValueError,match='unsafe display'):store.load_fiscal_dividends(path)


def yuho_document():
    document = annual_report_fixture({'9999': {'series': {'2000': 10, '2001': 20}}})
    r = document['9999']
    r['displaySeries']['2000'] = 10.2
    r['externalYears'] = [2000]
    r['yearProvenance']['2000'].update(sourceCategory='b', acquisitionRoute='ownerSavedPdf',
        sourceUrl='https://issuer/yuho.pdf', docTitle='有価証券報告書')
    r['displayAdoptions'] = {'2000': {'raw': 102, 'converted': 10.2, 'basis': '実額',
        'sourceUrl': 'https://issuer/yuho.pdf', 'docTitle': '有価証券報告書',
        'asOf': '2004-10-05', 'status': 'adopt'}}
    return document


def test_loader_allows_independent_yuho_value_without_changing_calculation():
    r = store.normalize_fiscal_dividends(yuho_document())['9999']
    assert r['streakSeries'][2000] == 10
    assert r['displaySeries'][2000] == 10.2
    assert r['externalYears'] == [2000]
    assert r['yuhoDisplayYears'] == {2000: yuho_document()['9999']['displayAdoptions']['2000']}


@pytest.mark.parametrize('mutation', ['external', 'wrong_value', 'mismatch', 'source', 'no_marker'])
def test_yuho_exception_fails_closed(mutation):
    d = yuho_document(); r = d['9999']
    if mutation == 'external': r['yearProvenance']['2000']['sourceCategory'] = 'e'
    if mutation == 'wrong_value': r['displayAdoptions']['2000']['converted'] = 10
    if mutation == 'mismatch':
        r['displaySeries']['2000'] = r['displayAdoptions']['2000']['converted'] = 10.31
    if mutation == 'source': r['displayAdoptions']['2000']['sourceUrl'] = 'https://other/source'
    if mutation == 'no_marker': r.pop('displayAdoptions')
    with pytest.raises(ValueError, match='unsafe display'):
        store.normalize_fiscal_dividends(d)


def test_public_annual_uses_yuho_value_and_streaks_keep_external_value(tmp_path):
    before, _ = build(tmp_path/'before')
    after, _ = build(tmp_path/'after', display={2000:10.2,2002:30,2003:40}, yuho={2000:'2004-10-05'})
    assert after['annual']['2000'] == 10.2
    assert after['dividendSeries']['startYear'] == 2000
    assert after['dividendSeries']['calculationStartYear'] == 2000
    assert not after['dividendSeries']['hiddenYearsArePrefix']
    for key in ('streakIncrease','streakNonDecrease','streakBase','streakNoDecreaseBase'):
        assert before[key] == after[key]
    assert '2000' not in after['dividendBreakdown']
    assert 'displayAdoptions' not in json.dumps(after)
    validate_public_payload(after, {2000,2002,2003}, {2000,2001,2002,2003},
                            financial_years={'main':{2003},'basisTransitionMetrics':set()})


def yuho_event(day, old=1, new=2, identity='pdd-event', status='confirmed'):
    return {'securityCode':'9999', 'effectiveDate':day, 'oldShares':old, 'newShares':new,
            'eventId':identity, 'status':status, 'action':'split', 'epsAdjustedByIssuer':True,
            'applyDividendAdjustment':True, 'source':{'url':'https://issuer/split'}}


def test_spk_adoption_uses_same_remaining_factor_as_series(tmp_path):
    document = yuho_document()
    r = document['9999']
    r['series'] = r['streakSeries'] = {'2011':25.5, '2012':26.5, '2013':27.5}
    r['displaySeries'] = dict(r['series'])
    r['externalYears'] = [2011,2012]
    provenance = r['yearProvenance']['2000']
    r['yearProvenance'] = {str(y):dict(provenance) for y in (2011,2012)}
    r['yearProvenance']['2013'] = {'sourceCategory':'a', 'documentType':'annualSecuritiesReport',
                                   'displayEligible':True}
    adoption = r['displayAdoptions']['2000']
    r['displayAdoptions'] = {str(y):dict(adoption, raw=v*2, converted=v, asOf='2026-10-21',
        appliedEvents=[yuho_event('2020-04-01',2,4,'producer-different-id')])
        for y,v in ((2011,25.5),(2012,26.5))}
    # Producer IDs and share notation may differ; match date and new/old ratio.
    r['appliedActions'] = [{'effectiveDate':'2020-04-01', 'ratio':2, 'eventId':'record-id'}]
    normalized = store.normalize_fiscal_dividends(document)
    actions = {'9999':[yuho_event('2020-04-01'), yuho_event('2026-04-01')]}
    out = tmp_path/'spk.sqlite'
    with patch.object(store,'load_daily_prices', return_value=({}, {}, 'fixture')), \
         patch.object(store,'load_price_session_meta',return_value=None), \
         patch.object(store,'load_yield_split_adjustments',return_value=[]):
        store.create_database(out,[{'code':'9999'}],{},{},{},[Path('fixture')]*4, 'fixture.csv',
            fiscal_by_code=normalized, stock_actions_by_code=actions, today=date(2026,10,21))
    with sqlite3.connect(out) as conn:
        p = json.loads(conn.execute('SELECT payload FROM stocks').fetchone()[0])
    assert p['annual'] == {'2011':12.75,'2012':13.25,'2013':13.75}
    assert 'appliedEvents' not in json.dumps(p)
    assert 'yuhoDisplayYears' not in json.dumps(p)


@pytest.mark.parametrize('basis', ['実額', '不明', '調整済み'])
def test_yuho_all_raw_bases_remain_visible_with_series_factor(tmp_path, basis):
    actions = {'9999':[yuho_event('2004-01-01')]}
    before,_ = build(tmp_path/'before', actions=actions)
    after,_ = build(tmp_path/'after', display={2000:10.2,2002:30,2003:40},
                    yuho={2000:{'asOf':'2004-10-05','basis':basis,'appliedEvents':[]}},
                    actions=actions)
    assert after['annual'] == {'2000':5.1,'2002':15,'2003':20}
    assert after['dividendSeries']['startYear'] == 2000
    for key in ('streakIncrease','streakNonDecrease','streakBase','streakNoDecreaseBase'):
        assert after[key] == before[key]


@pytest.mark.parametrize('month,cutoff,next_day', [
    (3,'2000-03-31','2000-04-01'), (12,'2000-12-31','2001-01-01'),
    (2,'2000-02-29','2000-03-01')])
def test_yuho_fiscal_boundary_today_consolidation_and_ledger_dedup(tmp_path,month,cutoff,next_day):
    events = [yuho_event(cutoff, identity='cutoff'),
              yuho_event(next_day, identity='after-cutoff'),
              yuho_event('2004-10-05',5,1,'today'),
              yuho_event('2004-10-06',identity='future'),
              yuho_event('2004-09-01',identity='provisional',status='provisional')]
    manual = tmp_path/'manual.json'
    extracted = tmp_path/'extracted.json'
    manual.write_text(json.dumps({'events':events}))
    extracted.write_text(json.dumps({'events':[events[1]]}))
    actions = store.load_stock_actions([manual,extracted],as_of=date(2004,10,5))
    assert len(actions['9999']) == 4  # future excluded, repeated eventId counted once
    # Both display and calculation use the FY end (even with raw component dates).
    adoption = {'asOf':'2004-06-01','basis':'不明','appliedEvents':[],
                'components':[{'kind':'interim','basisDate':'2000-01-01'},
                              {'kind':'final','basisDate':'2000-06-30'}]}
    payload,_ = build(tmp_path/'build',calculation={2000:10,1999:10},
                      display={2000:10.2,1999:10}, yuho={2000:adoption},
                      actions=actions, fiscal_month=month, applied_actions=[])
    # after-cutoff split ×.5, today's consolidation ×5, provisional split ×.5.
    assert payload['annual']['2000'] == pytest.approx(10.2 * 1.25)
    # The shared series filter also drops events through the latest FY end.
    assert payload['annual']['1999'] == pytest.approx(10 * 1.25)
    adjustment = store.split_adjustment(actions['9999'])
    assert store.adjustment_factor_for_period(
        adjustment,2000,fiscal_month=month,field='dividend') == 1.25
    assert store.adjustment_factor_for_period(
        adjustment,1999,fiscal_month=month,field='dividend') == .625


@pytest.mark.parametrize('recorded', [None, [],
    [{'effectiveDate':'2000-04-01','ratio':3}],
    [{'effectiveDate':'2000-04-02','ratio':2}]])
def test_yuho_applied_event_missing_or_date_ratio_mismatch_stops_build(tmp_path,recorded):
    adoption = {'asOf':'2004-10-05','basis':'実額',
                'appliedEvents':[yuho_event('2000-04-01')]}
    with pytest.raises(ValueError,match='9999/2000:.*appliedEvents not in record appliedActions'):
        build(tmp_path,display={2000:10.2},yuho={2000:adoption},applied_actions=recorded)
    assert not (tmp_path/'public.sqlite').exists()


def test_yuho_validation_failure_preserves_existing_database(tmp_path):
    build(tmp_path,display={2000:10.2},yuho={2000:'2004-10-05'})
    out = tmp_path/'public.sqlite'
    previous = out.read_bytes()
    with pytest.raises(ValueError,match='appliedEvents not in record appliedActions'):
        with patch.object(store,'load_daily_prices',return_value=({}, {}, 'fixture')), \
             patch.object(store,'load_price_session_meta',return_value=None), \
             patch.object(store,'load_yield_split_adjustments',return_value=[]):
            store.create_database(out,[{'code':'9999'}],{},{},{},[Path('fixture')]*4,'fixture.csv',
                fiscal_by_code={'9999':{
                    'series':{2000:10},'streakSeries':{2000:10},'displaySeries':{2000:10.2},
                    'displayPolicy':POLICY_ID,'fiscalMonth':3,
                    'yuhoDisplayYears':{2000:{'status':'adopt','converted':10.2,'asOf':'2004-10-05',
                        'appliedEvents':[yuho_event('2000-04-01')]}}}},
                today=date(2004,10,5))
    assert out.read_bytes() == previous


@pytest.mark.parametrize('value,reference,allowed', [
    (10.3,10,True), (9.7,10,True), (10.30001,10,False), (9.69999,10,False),
    (0,0,True), (.01,0,False)])
def test_yuho_build_rechecks_three_percent_boundary(tmp_path,value,reference,allowed):
    def run():
        return build(tmp_path,calculation={2000:reference},display={2000:value},
                     yuho={2000:'2004-10-05'})
    if allowed:
        assert run()[0]['annual']['2000'] == value
    else:
        with pytest.raises(ValueError,match='streakSeries mismatch'):
            run()
        assert not (tmp_path/'public.sqlite').exists()


@pytest.mark.parametrize('adoption_has_event', [False, True])
def test_yuho_recorded_events_are_not_reapplied_and_later_events_are_applied(tmp_path,adoption_has_event):
    adoption = {'asOf':'2004-06-01','basis':'調整済み',
                'appliedEvents':([yuho_event('2004-01-01',2,4,'producer-id')] if adoption_has_event else [])}
    actions = {'9999':[yuho_event('2004-01-01',identity='ledger-id'),
                      yuho_event('2004-09-01',identity='after-asof')]}
    payload,_ = build(tmp_path,display={2000:10.2},yuho={2000:adoption},actions=actions,
        applied_actions=[{'effectiveDate':'2004-01-01','ratio':2,'eventId':'record-id'}])
    assert payload['annual']['2000'] == 5.1
    assert 'yuhoDisplayYears' not in json.dumps(payload)


@pytest.mark.parametrize('fields,match', [
    ({'asOf':'2004-10-06'},'future'),
    ({'appliedEvents':{}},'array'),
    ({'appliedEvents':[yuho_event('2000-04-01',0,2)]},'ratio'),
    ({'appliedEvents':[None]},'object'),
    ({'appliedEvents':[{'effectiveDate':'bad','ratio':2}]},'date'),
    ({'converted':10.1},'streakSeries mismatch')])
def test_yuho_future_asof_and_malformed_adoption_stop_build(tmp_path,fields,match):
    adoption = dict({'asOf':'2004-10-05','basis':'実額','appliedEvents':[]},**fields)
    with pytest.raises(ValueError,match=match):
        build(tmp_path,display={2000:10.2},yuho={2000:adoption})


@pytest.mark.parametrize('payload',[{'streakSeries':{'2000':123456.789}},
                                   {'annual':{'2000':123456.789}},
                                   {'dividendBreakdown':{'2000':{'base':123456.789}}},
                                   {'annualPending':{'2000':{'value':123456.789}}},
                                   {'confirmedDividend':123456.789,'confirmedFiscalYearEnd':'2000-03-31'}])
def test_public_validation_catches_unexpected_years_and_private_keys(payload):
    with pytest.raises(ValueError):validate_public_payload(payload,{2003},{2000,2003})
