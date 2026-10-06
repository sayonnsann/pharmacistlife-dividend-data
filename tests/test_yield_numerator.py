from fiscal_fixtures import annual_report_fixture
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

    def test_database_uses_adjusted_series_without_changing_eps(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'stocks.sqlite'
            with (mock.patch.object(store, 'load_daily_prices', return_value=({'1234': 100}, {'1234': 12}, '')),
                  mock.patch.object(store, 'load_price_session_meta', return_value=None),
                  mock.patch.object(store, 'load_yield_split_adjustments', return_value=[dict(code='1234', execution_date='2026-04-01', ratio=2)]),
                  mock.patch.object(store, 'load_yield_source_year', return_value=2026)):
                store.create_database(path, [dict(code='1234', name='Example', eps={'2026': 20})], {}, {}, {},
                    [Path('fixture')] * 4, 'fixture.csv', {'1234': [event()]},
                    fiscal_by_code=annual_report_fixture({'1234': dict(fiscalMonth=3, series={2026: 20}, externalSource=None, externalYears=[], connectionStatus='connected', connectionReason='fixture')}), today=date(2026, 10, 4))
            with sqlite3.connect(path) as conn:
                payload = json.loads(conn.execute('SELECT payload FROM stocks').fetchone()[0])
            self.assertEqual(payload['dividendYield'], 10)
            self.assertEqual(payload['eps']['2026'], 10)
            self.assertEqual(payload['annual']['2026'], 10)

    def test_audit_reference_respects_applied_actions(self):
        with (mock.patch.object(store, 'load_daily_prices', return_value=({'1234': 100}, {'1234': 10}, 'fixture')),
              mock.patch.object(store, 'load_fiscal_dividends', return_value={'1234': dict(fiscalMonth=3, series={2026: 10}, displaySeries={2026: 10}, appliedActions=[{'effectiveDate': '2026-04-01'}])}),
              mock.patch.object(store, 'load_stock_actions', return_value={'1234': [event()]}),
              mock.patch.object(store, 'load_yield_split_adjustments', return_value=[dict(code='1234', execution_date='2026-04-01', ratio=2)]),
              mock.patch.object(store, 'load_yield_source_year', return_value=2026)):
            report = audit_tool.audit('fixture', Path('fixture'), [], Path('fixture'), today=date(2026, 10, 4))
        self.assertEqual(report['before_bad'], ['1234'])
        self.assertEqual(report['after_bad'], [])
        self.assertEqual(report['rows'][0]['reference'], 10)

    def test_audit_uses_calculation_period_before_selecting_display_year(self):
        record = dict(fiscalMonth=3, series={2026: 10, 2027: 12},
                      streakSeries={2026: 10, 2027: 12}, displaySeries={2026: 10})
        with (mock.patch.object(store, 'load_daily_prices', return_value=({'1234': 100}, {'1234': 10}, 'fixture')),
              mock.patch.object(store, 'load_fiscal_dividends', return_value={'1234': record}),
              mock.patch.object(store, 'load_stock_actions', return_value={'1234': [event()]}),
              mock.patch.object(store, 'load_yield_split_adjustments', return_value=[]),
              mock.patch.object(store, 'load_yield_source_year', return_value=2026)):
            report = audit_tool.audit('fixture', Path('fixture'), [], Path('fixture'), today=date(2026, 10, 4))
        # The full series already covers the split; its hidden latest year must
        # not cause the displayed older year to be adjusted a second time.
        self.assertEqual(report['rows'][0]['reference'], 10)


class YieldNumeratorOverrideIntegrationTest(unittest.TestCase):
    """明示指定ファイルが分子の計算へ接続されていることを確認する。"""

    def build(self, financial, events, *, fiscal=None, calendar=None, forecast=None,
              code='1234', dividend=42, source_year=2026, price=1000, active_adjustments=None):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'stocks.sqlite'
            with (mock.patch.object(store, 'load_daily_prices', return_value=(
                    {code: price} if price else {}, {code: dividend}, 'fixture')),
                  mock.patch.object(store, 'load_price_session_meta', return_value=None),
                  mock.patch.object(store, 'load_yield_split_adjustments', return_value=active_adjustments or []),
                  mock.patch.object(store, 'load_yield_source_year', return_value=source_year)):
                store.create_database(
                    path, [dict(code=code, name='Example', **financial)], {}, {},
                    {code: forecast} if forecast else {}, [Path('fixture')] * 4,
                    'fixture.csv', {code: events}, fiscal_by_code=annual_report_fixture({code: fiscal} if fiscal else {}),
                    calendar_by_code={code: calendar} if calendar else {}, today=date(2026, 10, 4))
            with sqlite3.connect(path) as conn:
                list_yield, raw = conn.execute('SELECT yield,payload FROM stocks').fetchone()
                payload = json.loads(raw)
            if fiscal:
                self.assertEqual(list_yield, store.bounded(payload['dividendYield'], 0, 30))
            return payload

    @staticmethod
    def fiscal(series, applied=None):
        return dict(fiscalMonth=3, series=series, appliedActions=applied or [],
                    externalSource=None, externalYears=[], connectionStatus='connected',
                    connectionReason='fixture')


    def test_8227_uses_series_despite_csv_override(self):
        e = event('2026-02-21', new=3)
        e.update(eventId='8227-2026-02-21-split-1-to-3', securityCode='8227',
                 epsAdjustedByIssuer=True)
        fiscal = self.fiscal({2026: 71.67}, [{'effectiveDate': '2026-02-21'}])
        fiscal['fiscalMonth'] = 2
        payload = self.build({}, [e], code='8227', dividend=215, fiscal=fiscal)
        self.assertEqual(payload['dividendYield'], 7.17)  # fixture株価1000円
        self.assertEqual(payload['annual']['2026'], 71.67)

    def test_split_series_bypasses_csv_factor_and_selects_max_year(self):
        e = event(new=3)
        fiscal = self.fiscal({2026: 90, 2024: 12, 2025: 60})
        original = copy.deepcopy(fiscal)
        with mock.patch.object(store, 'yield_numerator_factor', return_value=.75) as factor:
            payload = self.build({}, [e], fiscal=fiscal, dividend=60)
        factor.assert_called_once()  # 旧分子の45円だけを計算。棒の30円には掛けない。
        self.assertEqual(fiscal, original)
        self.assertEqual(payload['annual']['2026'], 30)
        self.assertEqual(payload['dividendYield'], 3)
        self.assertEqual(payload['dividendYieldBasis'], dict(
            source='fiscal_series', fiscalYear=2026, annualDividend=30))

    def test_latest_zero_does_not_use_previous_year_or_csv(self):
        payload = self.build({'dividendYield': 88}, [], dividend=123,
                             fiscal=self.fiscal({2026: 0, 2024: 100, 2025: 200}))
        self.assertEqual(payload['dividendYield'], 0)
        self.assertEqual(payload['dividendYieldBasis']['annualDividend'], 0)
        self.assertEqual(payload['dividendYieldBasis']['fiscalYear'], 2026)

    def test_csv_zero_does_not_hide_positive_fiscal_series(self):
        payload = self.build({}, [], dividend=0, fiscal=self.fiscal({2026: 40}))
        self.assertEqual(payload['dividendYield'], 4)

    def test_pending_and_partial_are_not_yield_sources(self):
        forecast = dict(confirmedDividend=80, confirmedFiscalYearEnd='2026-03-31',
                        forecastDividend=90, forecastFiscalYear=2027,
                        forecastPeriod='2027年3月期(予)')
        payload = self.build({}, [], fiscal=self.fiscal({2025: 30}), forecast=forecast)
        self.assertEqual(payload['annualPartial'], {'2026': 80, '2027': 90})
        self.assertEqual(payload['dividendYield'], 3)
        self.assertEqual(payload['dividendYieldBasis']['fiscalYear'], 2025)

    def test_missing_series_never_displays_csv_or_calendar_values(self):
        with mock.patch.object(store, 'yield_numerator_factor', wraps=store.yield_numerator_factor) as factor:
            payload = self.build({}, [event()], dividend=42,
                                 calendar={'series': {2026: 900}})
        factor.assert_not_called()
        self.assertIsNone(payload['dividendYield'])
        self.assertEqual(payload['dividendYieldBasis'], dict(
            source='no_display_dividend', fiscalYear=None, annualDividend=None))
        self.assertEqual(payload['annual'], {})

    def test_missing_csv_year_is_null_and_unpaid_display_is_preserved(self):
        payload = self.build({'dividendYield': 99}, [], dividend=0, source_year=None)
        self.assertIsNone(payload['dividendYield'])
        self.assertEqual(payload['dividendYieldBasis'], dict(
            source='no_display_dividend', fiscalYear=None, annualDividend=None))

    def test_missing_price_does_not_leave_an_unrelated_legacy_yield(self):
        payload = self.build({'dividendYield': 99}, [], price=None,
                             fiscal=self.fiscal({2026: 40}))
        self.assertIsNone(payload['dividendYield'])
        self.assertEqual(payload['dividendYieldBasis']['annualDividend'], 40)

    def test_guard_reports_unmatched_split_but_keeps_display_numerator(self):
        active = [dict(code='1234', execution_date='2026-03-30', ratio=3)]
        payload = self.build({}, [], dividend=30, source_year=2025,
                             fiscal=self.fiscal({2026: 40}), active_adjustments=active)
        self.assertEqual(payload['annual']['2026'], 40)
        self.assertEqual(payload['dividendYield'], 4)
        self.assertEqual(payload['dividendYieldBasis'], dict(
            source='fiscal_series', fiscalYear=2026, annualDividend=40,
            guardReason='kouhaitou_split_unreflected'))

    def test_guard_ratio_and_reciprocal_use_actual_legacy_numerator(self):
        for latest, raw, events, expected, guarded in [
            (60, 30, [], 30, True), (30, 60, [], 60, True),
            (120, 90, [event()], 60, False),
        ]:
            # 最後は棒120/2=60と旧分子90/2=45。整数比でないため発動しない。
            payload = self.build({}, events, dividend=raw, fiscal=self.fiscal({2026: latest}))
            self.assertEqual(payload['dividendYieldBasis']['source'],
                             'fiscal_series')
            self.assertEqual(payload['dividendYield'], payload['annual']['2026'] / 10)
            if guarded:
                self.assertEqual(payload['dividendYieldBasis']['guardReason'], 'split_like_ratio')

    def test_zero_or_missing_previous_does_not_guard_even_with_split_record(self):
        active = [dict(code='1234', execution_date='2026-03-30', ratio=3)]
        for previous in (0, None):
            payload = self.build({}, [], dividend=previous,
                                 fiscal=self.fiscal({2026: 60}), active_adjustments=active)
            self.assertEqual(payload['dividendYield'], 6)
            self.assertEqual(payload['dividendYieldBasis']['source'], 'fiscal_series')

    def test_matched_ledger_event_and_equal_numerators_do_not_guard(self):
        e = event()
        active = [dict(code='1234', execution_date='2026-03-30', ratio=2, active=True)]
        payload = self.build({}, [e], dividend=40, active_adjustments=active,
                             fiscal=self.fiscal({2026: 40}, [{'effectiveDate': e['effectiveDate']}]))
        self.assertEqual(payload['annual']['2026'], 40)
        self.assertEqual(payload['dividendYield'], 4)
        self.assertEqual(payload['dividendYieldBasis']['source'], 'fiscal_series')


class SplitGuardRulesTest(unittest.TestCase):
    def test_ratio_boundaries_and_non_integer_increases(self):
        for ratio, expected in [(1.8, False), (1.9399, False), (1.94, True),
                                (2, True), (2.06, True), (2.0601, False),
                                (1.3, False), (1.5, False), (1.7, False), (15, True)]:
            for value in (ratio, 1 / ratio):
                with self.subTest(ratio=value):
                    result = store.yield_split_guard_reason('1234', value * 100, 100, [], [])
                    self.assertEqual(result, 'split_like_ratio' if expected else None)
        self.assertIsNone(store.yield_split_guard_reason('1234', 0, 100, [], []))

    def test_record_matches_code_ratio_and_zero_to_seven_day_window(self):
        record = dict(code='1234', execution_date='2026-03-30', ratio=2)
        for day, ratio, code, reflected in [
            ('2026-03-29', 2, '1234', False), ('2026-03-30', 2, '1234', True),
            ('2026-04-06', 2, '1234', True), ('2026-04-07', 2, '1234', False),
            ('2026-04-01', 1.998, '1234', True), ('2026-04-01', 2.002, '1234', True),
            ('2026-04-01', 1.9979, '1234', False), ('2026-04-01', 2.0021, '1234', False),
            ('2026-04-01', 2, '5678', False),
        ]:
            e = event(day, new=ratio)
            e['securityCode'] = code
            result = store.yield_split_guard_reason('1234', 40, 30, [e], [record])
            self.assertEqual(result, None if reflected else 'kouhaitou_split_unreflected')
        self.assertIsNone(store.yield_split_guard_reason('1234', 40, 30, [], [{**record, 'active': False}]))
        # 台帳一致は記録条件を解除する。整数比条件は独立したOR条件。
        self.assertEqual(store.yield_split_guard_reason('1234', 60, 30, [event()], [record]),
                         'split_like_ratio')

    def test_active_omission_is_only_allowed_for_guard_view(self):
        document = {'adjustments': [dict(code='1234', execution_date='2026-03-30', ratio=2)]}
        with self.assertRaises(ValueError):
            store.parse_yield_split_adjustments(document)
        self.assertEqual(store.parse_yield_split_adjustments(document, allow_missing_active=True),
                         document['adjustments'])
        for value in (None, 1, 'true'):
            with self.assertRaises(ValueError):
                store.parse_yield_split_adjustments(
                    {'adjustments': [{**document['adjustments'][0], 'active': value}]}, allow_missing_active=True)


class FiscalNumeratorAuditTest(unittest.TestCase):
    def test_guards_are_excluded_and_exported_with_split_records(self):
        def payload(reason, value=60):
            return dict(name='Example', dividendSeries={'basis': 'fiscal'}, annual={'2026': 60},
                        dividendYieldBasis=dict(source='daily_csv_split_guard', fiscalYear=2025,
                                                annualDividend=30, seriesLatestDividend=value, guardReason=reason))
        report = audit_tool.audit_fiscal_payloads(
            {'1234': payload('kouhaitou_split_unreflected'), '5678': payload('split_like_ratio'),
             '9012': payload('split_like_ratio', 59)},
            [dict(code='1234', execution_date='2026-03-30', ratio=2)])
        self.assertEqual(report['fiscal_series_count'], 3)
        self.assertEqual(report['excluded_guard_count'], 3)
        self.assertEqual(report['matched_count'], 0)
        self.assertEqual(report['mismatch_count'], 0)
        self.assertEqual(report['guard_mismatch_count'], 1)
        self.assertEqual(report['ratio_guard_without_kouhaitou_record_count'], 2)
        self.assertEqual(report['guarded'][0]['kouhaitou_splits'],
                         [dict(execution_date='2026-03-30', ratio=2)])
        self.assertEqual(report['guarded'][1]['ratio'], 2)

    def test_exact_match_including_zero_and_unsorted_years(self):
        report = audit_tool.audit_fiscal_payloads({
            '1234': dict(dividendSeries={'basis': 'fiscal'}, annual={'2026': 0, '2027': None, '2025': 90},
                         dividendYieldBasis=dict(source='fiscal_series', fiscalYear=2026, annualDividend=0)),
            '414A': dict(dividendSeries={'basis': 'calendar'}, annual={'2026': 100}),
        })
        self.assertEqual(report['matched_count'], 1)
        self.assertEqual(report['mismatch_count'], 0)
        self.assertEqual(report['daily_csv_count'], 1)

    def test_reports_missing_basis_wrong_year_and_small_numeric_mismatch(self):
        payload = dict(dividendSeries={'basis': 'fiscal'}, annual={'2025': 30, '2026': 40})
        report = audit_tool.audit_fiscal_payloads({
            '1234': payload,
            '5678': dict(payload, dividendYieldBasis=dict(source='fiscal_series', fiscalYear=2025, annualDividend=40)),
            '9012': dict(payload, dividendYieldBasis=dict(source='fiscal_series', fiscalYear=2026, annualDividend=40.0001)),
        })
        self.assertEqual(report['matched_count'], 0)
        self.assertEqual(report['mismatch_count'], 3)
        self.assertEqual([row['code'] for row in report['mismatches']], ['1234', '5678', '9012'])
