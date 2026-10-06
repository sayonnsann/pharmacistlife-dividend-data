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


def build(tmp_path, *, display=None, empty=False, forecast=None, unreliable=False):
    calculation = {} if empty else {2000: 10, 2001: 20, 2002: 30, 2003: 40}
    display = {2002: 30, 2003: 40} if display is None else display
    record = {'series': calculation, 'streakSeries': calculation,
              'displaySeries': display if not empty else {}, 'displayPolicy': POLICY_ID,
              'fiscalMonth': 3, 'externalYears': sorted(set(calculation)-set(display)), 'externalSource': 'external',
              'reportedYears': sorted(calculation), 'connectionStatus': 'scaled',
              'connectionReason': 'PRIVATE_DETAIL', 'streakReliable': not unreliable}
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


@pytest.mark.parametrize('payload',[{'streakSeries':{'2000':123456.789}},
                                   {'annual':{'2000':123456.789}},
                                   {'dividendBreakdown':{'2000':{'base':123456.789}}},
                                   {'annualPending':{'2000':{'value':123456.789}}},
                                   {'confirmedDividend':123456.789,'confirmedFiscalYearEnd':'2000-03-31'}])
def test_public_validation_catches_unexpected_years_and_private_keys(payload):
    with pytest.raises(ValueError):validate_public_payload(payload,{2003},{2000,2003})
