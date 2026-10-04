import copy
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import audit_yield_numerator as audit_tool
store = audit_tool.store


def event(day='2026-04-01', old=1, new=2, **kwargs):
    return dict(eventId=day, securityCode='1234', action='split',
                effectiveDate=day, oldShares=old, newShares=new,
                status='confirmed', applyDividendAdjustment=True,
                epsAdjustedByIssuer=False, source={'url': 'https://example.com/ir'},
                **kwargs)


class YieldNumeratorTest(unittest.TestCase):
    def factor(self, events, **kwargs):
        args = dict(source_year=2026, fiscal_month=3, active_adjustments=[],
                    today=date(2026, 10, 4))
        args.update(kwargs)
        adjustment = store.split_adjustment(events)
        unchanged = copy.deepcopy(adjustment)
        result = store.yield_numerator_factor('1234', events, adjustment, **args)
        self.assertEqual(adjustment, unchanged)
        return result

    def test_boundaries_and_consolidation(self):
        for day, expected in [('2026-03-31', 1), ('2026-04-01', .5),
                              ('2026-10-04', .5), ('2026-10-05', 1)]:
            self.assertEqual(self.factor([event(day)]), expected)
        self.assertEqual(self.factor([event(old=3, new=1)]), 3)
        self.assertEqual(self.factor([event('2024-02-29')], source_year=2024, fiscal_month=2), 1)

    def test_active_match_requires_code_date_and_ratio(self):
        active = dict(code='1234', execution_date='2026-04-01', ratio=2)
        self.assertEqual(self.factor([event()], active_adjustments=[active]), 1)
        for key, value in [('code', '5678'), ('execution_date', '2026-03-24'), ('ratio', 3)]:
            self.assertEqual(self.factor([event()], active_adjustments=[{**active, key: value}]), .5)
        self.assertEqual(self.factor([event(old=2, new=4)], active_adjustments=[active]), 1)

    def test_ex_date_window_and_relative_ratio_tolerance(self):
        for day, expected in [('2026-03-24', .5), ('2026-03-25', 1),
                              ('2026-04-01', 1), ('2026-04-02', .5)]:
            self.assertEqual(self.factor([event()], active_adjustments=[dict(
                code='1234', execution_date=day, ratio=2)]), expected)
        for ratio, expected in [(1.998, 1), (2.002, 1), (1.9979, .5), (2.0021, .5)]:
            self.assertEqual(self.factor([event()], active_adjustments=[dict(
                code='1234', execution_date='2026-03-30', ratio=ratio)]), expected)

    def test_actual_ex_date_pairs(self):
        for code, effective, ex_day, ratio in [
            ('5706', '2026-10-01', '2026-09-29', 10),
            ('8316', '2026-10-01', '2026-09-29', 2),
            ('8309', '2026-08-01', '2026-07-30', 4),
            ('9065', '2026-10-01', '2026-09-29', 5),
            ('1815', '2026-10-01', '2026-09-29', 2),
        ]:
            e = event(effective, new=ratio)
            e['securityCode'] = code
            with self.subTest(code=code):
                self.assertEqual(store.yield_numerator_factor(
                    code, [e], store.split_adjustment([e]), source_year=2026,
                    fiscal_month=3, today=date(2026, 10, 4), active_adjustments=[
                        dict(code=code, execution_date=ex_day, ratio=ratio)]), 1)

    def test_dps_restated_is_not_a_numerator_override(self):
        for flag in (False, True, None):
            for day in ('2014-01-01', '2025-04-01', '2026-02-21', '2026-03-31'):
                self.assertEqual(self.factor([event(day, dpsRestated=flag)]), 1)
        self.assertEqual(self.factor([event('2025-01-01', dpsRestated=False)],
                                     source_year=2025, fiscal_month=6), 1)

    def test_explicit_override_respects_active_state_and_future_date(self):
        e = event('2026-02-21', new=3)
        overrides = frozenset({e['eventId']})
        self.assertAlmostEqual(self.factor([e], override_event_ids=overrides), 1 / 3)
        self.assertEqual(self.factor([e], override_event_ids=frozenset({'another-event'})), 1)
        self.assertEqual(self.factor([e], override_event_ids=overrides, active_adjustments=[
            dict(code='1234', execution_date='2026-02-19', ratio=3)]), 1)
        self.assertEqual(self.factor([e], override_event_ids=overrides, today=date(2026, 2, 20)), 1)
        # 期末後のイベントを指定しても係数は一度だけ掛ける。
        e = event()
        self.assertEqual(self.factor([e], override_event_ids=frozenset({e['eventId']})), .5)

    def test_override_file_missing_invalid_and_duplicate_are_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'overrides.json'
            with redirect_stdout(io.StringIO()) as log:
                self.assertEqual(store.load_yield_numerator_overrides(path), frozenset())
            self.assertIn('指定なし', log.getvalue())
            valid = dict(eventId='8227-2026-02-21-split-1-to-3', reason='reviewed', checkedAt='2026-10-04')
            for doc in ({}, {'schemaVersion': True, 'events': []},
                        {'schemaVersion': 1, 'events': [valid, valid]},
                        {'schemaVersion': 1, 'events': [valid, {'eventId': 'bad'}]},
                        {'schemaVersion': 1, 'events': [{**valid, 'checkedAt': 'invalid'}]}):
                path.write_text(json.dumps(doc))
                self.assertEqual(store.load_yield_numerator_overrides(path), frozenset())
            path.write_text('{broken')
            self.assertEqual(store.load_yield_numerator_overrides(path), frozenset())
            path.write_text(json.dumps({'schemaVersion': 1, 'events': [valid]}))
            self.assertEqual(store.load_yield_numerator_overrides(path), frozenset({valid['eventId']}))
        self.assertEqual(store.load_yield_numerator_overrides(store.DEFAULT_YIELD_NUMERATOR_OVERRIDES),
                         frozenset({'8227-2026-02-21-split-1-to-3'}))

    def test_fallback_uses_legacy_product(self):
        for args in [dict(active_adjustments=None), dict(source_year=None),
                     dict(fiscal_month=None), dict(fiscal_month=13)]:
            with redirect_stdout(io.StringIO()) as log:
                self.assertEqual(self.factor([event('2025-01-01'), event()], **args), .25)
            self.assertIn('従来計算', log.getvalue())

    def test_source_year(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / '1234.json').write_text(json.dumps({'dps': {'2025': 30, '2026': 40}}))
            self.assertEqual(store.load_yield_source_year('1234', path), 2026)
            self.assertIsNone(store.load_yield_source_year('5678', path))
            (path / '1234.json').write_text('{"dps": {"bad": 1}}')
            self.assertIsNone(store.load_yield_source_year('1234', path))

    def test_loader_urls_active_and_failure(self):
        state = {'adjustments': [dict(code='1234', execution_date='2026-04-01', ratio=2, active=True), dict(active=False)]}
        for base in [store.DAILY_PRICE_CSV_URL, store.DAILY_PRICE_CSV_URL_NO_CACHE, 'file:///tmp/database.csv']:
            with mock.patch.object(store.subprocess, 'run', return_value=mock.Mock(stdout=json.dumps(state).encode())) as run:
                self.assertEqual(len(store.load_yield_split_adjustments(base)), 1)
                self.assertEqual(run.call_args.args[0][-1], base.replace('database.csv', 'split_adjustments.json'))
        for state in [{}, {'adjustments': [{'active': 'true'}]},
                      {'adjustments': [{'active': True, 'code': '1234', 'ratio': 0}]}]:
            with mock.patch.object(store.subprocess, 'run', return_value=mock.Mock(stdout=json.dumps(state).encode())):
                self.assertIsNone(store.load_yield_split_adjustments('file:///tmp/database.csv'))
        with mock.patch.object(store.subprocess, 'run', side_effect=OSError()):
            self.assertIsNone(store.load_yield_split_adjustments('file:///tmp/database.csv'))

    def test_database_wires_factor_without_changing_series_or_eps(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'stocks.sqlite'
            with (mock.patch.object(store, 'load_daily_prices', return_value=({'1234': 100}, {'1234': 10}, '')),
                  mock.patch.object(store, 'load_price_session_meta', return_value=None),
                  mock.patch.object(store, 'load_yield_split_adjustments', return_value=[dict(code='1234', execution_date='2026-04-01', ratio=2)]),
                  mock.patch.object(store, 'load_yield_source_year', return_value=2026)):
                store.create_database(path, [dict(code='1234', name='Example', eps={'2026': 20})], {}, {}, {},
                    [Path('fixture')] * 4, 'fixture.csv', {'1234': [event()]},
                    fiscal_by_code={'1234': dict(fiscalMonth=3, series={2026: 20}, externalSource=None, externalYears=[], connectionStatus='connected', connectionReason='fixture')}, today=date(2026, 10, 4))
            with sqlite3.connect(path) as conn:
                payload = json.loads(conn.execute('SELECT payload FROM stocks').fetchone()[0])
            self.assertEqual(payload['dividendYield'], 10)
            self.assertEqual(payload['eps']['2026'], 10)
            self.assertEqual(payload['annual']['2026'], 10)

    def test_audit_reference_respects_applied_actions(self):
        with (mock.patch.object(store, 'load_daily_prices', return_value=({'1234': 100}, {'1234': 10}, 'fixture')),
              mock.patch.object(store, 'load_fiscal_dividends', return_value={'1234': dict(fiscalMonth=3, series={2026: 10}, appliedActions=[{'effectiveDate': '2026-04-01'}])}),
              mock.patch.object(store, 'load_stock_actions', return_value={'1234': [event()]}),
              mock.patch.object(store, 'load_yield_split_adjustments', return_value=[dict(code='1234', execution_date='2026-04-01', ratio=2)]),
              mock.patch.object(store, 'load_yield_source_year', return_value=2026)):
            report = audit_tool.audit('fixture', Path('fixture'), [], Path('fixture'), today=date(2026, 10, 4))
        self.assertEqual(report['before_bad'], ['1234'])
        self.assertEqual(report['after_bad'], [])
        self.assertEqual(report['rows'][0]['reference'], 10)


class YieldNumeratorOverrideIntegrationTest(unittest.TestCase):
    """明示指定ファイルが分子の計算へ接続されていることを確認する。"""

    def build(self, financial, events, *, fiscal=None, calendar=None, forecast=None,
              code='1234', dividend=42):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'stocks.sqlite'
            with (mock.patch.object(store, 'load_daily_prices', return_value=({code: 1000}, {code: dividend}, 'fixture')),
                  mock.patch.object(store, 'load_price_session_meta', return_value=None),
                  mock.patch.object(store, 'load_yield_split_adjustments', return_value=[]),
                  mock.patch.object(store, 'load_yield_source_year', return_value=2026)):
                store.create_database(
                    path, [dict(code=code, name='Example', **financial)], {}, {},
                    {code: forecast} if forecast else {}, [Path('fixture')] * 4,
                    'fixture.csv', {code: events}, fiscal_by_code={code: fiscal} if fiscal else {},
                    calendar_by_code={code: calendar} if calendar else {}, today=date(2026, 10, 4))
            with sqlite3.connect(path) as conn:
                return json.loads(conn.execute('SELECT payload FROM stocks').fetchone()[0])

    @staticmethod
    def fiscal(series, applied=None):
        return dict(fiscalMonth=3, series=series, appliedActions=applied or [],
                    externalSource=None, externalYears=[], connectionStatus='connected',
                    connectionReason='fixture')


    def test_8227_override_is_loaded_by_database(self):
        e = event('2026-02-21', new=3)
        e.update(eventId='8227-2026-02-21-split-1-to-3', securityCode='8227',
                 epsAdjustedByIssuer=True)
        fiscal = self.fiscal({2026: 71.67}, [{'effectiveDate': '2026-02-21'}])
        fiscal['fiscalMonth'] = 2
        payload = self.build({}, [e], code='8227', dividend=215, fiscal=fiscal)
        self.assertEqual(payload['dividendYield'], 7.17)  # fixture株価1000円
        self.assertEqual(payload['annual']['2026'], 71.67)
