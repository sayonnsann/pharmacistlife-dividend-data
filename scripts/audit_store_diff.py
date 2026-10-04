#!/usr/bin/env python3
"""Build two isolated stores from identical local inputs and compare every payload field.

The default baseline is git HEAD:scripts/build_store.py. No repository data is
written. Supply --before-code/--after-code and separate ledgers for stage two.
Missing forecasts must be explicit: without --forecasts the report is partial.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import shutil
import sqlite3
import subprocess
import tempfile
from datetime import date
from pathlib import Path

import audit_yield_numerator

ROOT = Path(__file__).resolve().parents[1]
REQUIRED_FIELDS = (
    'dividendYield', 'dividendPerShare', 'annual', 'annualPending', 'annualPartial',
    'streakIncrease', 'streakNonDecrease', 'streakBase', 'streakNoDecreaseBase',
    'eps', 'bps', 'forecastDividend', 'forecastYield', 'forecastBasis',
    'confirmedDividend', 'earnings',
)


def compare_payloads(before, after):
    """Retain complete before/after values, distinguishing absent keys from null."""
    fields = {key: [] for key in REQUIRED_FIELDS}
    for code in sorted(before.keys() | after.keys()):
        left, right = before.get(code, {}), after.get(code, {})
        for key in sorted(left.keys() | right.keys()):
            fields.setdefault(key, [])
            if (key in left) == (key in right) and left.get(key) == right.get(key):
                continue
            fields[key].append(dict(code=code, before_present=key in left,
                                    after_present=key in right,
                                    before=left.get(key), after=right.get(key)))
    annual_basis = {}
    for basis in ('fiscal', 'calendar'):
        annual_basis[basis] = [row['code'] for row in fields['annual'] if
            (after.get(row['code'], before.get(row['code'], {})).get('dividendSeries') or {}).get('basis') == basis]
    return dict(before_count=len(before), after_count=len(after),
                added_codes=sorted(after.keys() - before.keys()),
                removed_codes=sorted(before.keys() - after.keys()),
                changed_counts={key: len(value) for key, value in sorted(fields.items())},
                annual_changed_by_basis=annual_basis, changes=fields)


def load_module(path, name, root):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # An extracted HEAD file still reads the same repository inputs as the candidate.
    module.REPOSITORY_ROOT = root
    if hasattr(module, 'DEFAULT_YIELD_NUMERATOR_OVERRIDES'):
        module.DEFAULT_YIELD_NUMERATOR_OVERRIDES = root / 'data/yield_numerator_overrides.json'
    return module


def fingerprint(path):
    if path is None or not path.exists():
        return dict(path=str(path) if path else None, available=False)
    return dict(path=str(path.resolve()), available=True,
                sha256=hashlib.sha256(path.read_bytes()).hexdigest())


def read_payloads(path):
    with sqlite3.connect(f'{path.as_uri()}?mode=ro', uri=True) as conn:
        return {code: json.loads(raw) for code, raw in conn.execute('SELECT code,payload FROM stocks')}


def compare_stock_columns(before_path, after_path):
    """一覧の全列も検査する。payloadはcompare_payloadsで項目ごとに比較する。"""
    snapshots = []
    for path in (before_path, after_path):
        with sqlite3.connect(f'{path.resolve().as_uri()}?mode=ro', uri=True) as conn:
            conn.row_factory = sqlite3.Row
            snapshots.append({row['code']: {key: row[key] for key in row.keys() if key != 'payload'}
                              for row in conn.execute('SELECT * FROM stocks')})
    return compare_payloads(*snapshots)


def run(args):
    args.output_dir.mkdir(parents=True, exist_ok=False)
    root = args.repo.resolve()
    with tempfile.TemporaryDirectory(prefix='audit-store-inputs-') as temporary:
        temp = Path(temporary)
        before_path = args.before_code
        head = None
        if before_path is None:
            head = subprocess.check_output(['git', '-C', str(root), 'rev-parse', args.before_ref], text=True).strip()
            before_path = temp / 'build_store_head.py'
            before_path.write_bytes(subprocess.check_output(
                ['git', '-C', str(root), 'show', f'{head}:scripts/build_store.py']))
        after_path = args.after_code or root / 'scripts/build_store.py'
        before = load_module(before_path, 'store_before', root)
        after = load_module(after_path, 'store_after', root)
        shutil.copyfile(args.prices, temp / 'database.csv')
        shutil.copyfile(args.split_adjustments, temp / 'split_adjustments.json')
        if args.price_meta:
            shutil.copyfile(args.price_meta, temp / 'price_update_meta.json')
        prices_url = (temp / 'database.csv').as_uri()
        before_actions = [args.before_actions, args.before_extracted]
        after_actions = [args.after_actions, args.after_extracted]
        coverage = {}
        for name, module, actions in [('before', before, before_actions), ('after', after, after_actions)]:
            forecasts = module.load_forecasts(args.forecasts) if args.forecasts else {}
            fiscal = module.load_fiscal_dividends(args.fiscal_dividends)
            calendar = module.load_calendar_dividends(args.calendar_dividends)
            module.create_database(
                (args.output_dir / f'{name}.sqlite').resolve(),
                module.load_json(args.financials, list), module.load_json(args.sectors, dict),
                module.load_tickers(args.tickers), forecasts,
                [args.financials, args.sectors, args.tickers, args.forecasts or Path('unavailable')],
                prices_url, module.load_stock_actions(actions, as_of=args.as_of), actions,
                fiscal, args.fiscal_dividends, calendar, args.calendar_dividends,
                today=args.as_of, forecast_overrides=module.load_forecast_overrides(root / 'data/forecast_overrides.json'),
            )
            coverage[name] = dict(forecast_records=len(forecasts), fiscal_records=len(fiscal),
                                  calendar_records=len(calendar))
        numerator = audit_yield_numerator.audit(
            prices_url, args.fiscal_dividends, after_actions, root / 'edinet',
            today=args.as_of, baseline_action_paths=before_actions,
            overrides_path=root / 'data/yield_numerator_overrides.json')
        payloads = {label: read_payloads((args.output_dir / f'{label}.sqlite').resolve())
                    for label in ('before', 'after')}
        numerator['rows'] = [row for row in numerator['rows'] if row['code'] in payloads['after']]
        # Evaluate each selected implementation, including a modern baseline in stage two.
        fiscal = after.load_fiscal_dividends(args.fiscal_dividends)
        for label, module, paths in [('before', before, before_actions), ('after', after, after_actions)]:
            events = module.load_stock_actions(paths, as_of=args.as_of)
            active = (module.load_yield_split_adjustments(prices_url)
                      if hasattr(module, 'yield_numerator_factor') else None)
            overrides = (module.load_yield_numerator_overrides(root / 'data/yield_numerator_overrides.json')
                         if hasattr(module, 'yield_numerator_factor') else frozenset())
            for row in numerator['rows']:
                code = row['code']
                payload = payloads[label][code]
                basis = payload.get('dividendYieldBasis')
                if basis is not None:
                    row[label] = basis['annualDividend']
                else:
                    adjustment = module.split_adjustment(events.get(code, []))
                    factor = adjustment['dividendFactor'] if adjustment else 1.0
                    if hasattr(module, 'yield_numerator_factor'):
                        factor = module.yield_numerator_factor(
                            code, events.get(code, []), adjustment,
                            source_year=module.load_yield_source_year(code, root / 'edinet'),
                            fiscal_month=fiscal.get(code, {}).get('fiscalMonth'),
                            active_adjustments=active, today=args.as_of, override_event_ids=overrides)
                    row[label] = row['raw'] * factor
                row[label + '_basis'] = basis
                # 丸めや無配・欠損の扱いも、完成した画面payloadを報告する。
                row[label + '_yield'] = payload.get('dividendYield')
                reference = row['reference']
                ratio = row[label] / reference if (row[label] is not None
                                                  and reference is not None and reference > 0) else None
                row[label + '_ratio'] = ratio
                row[label + '_bad'] = ratio is not None and not 0.6 <= ratio <= 1.6
            numerator[label + '_bad'] = [row['code'] for row in numerator['rows'] if row[label + '_bad']]
        numerator['newly_bad'] = sorted(set(numerator['after_bad']) - set(numerator['before_bad']))
        numerator['total'] = len(numerator['rows'])
        numerator['compared'] = sum(row['reference'] is not None and row['reference'] > 0
                                   for row in numerator['rows'])
        numerator['fiscal_series_audit'] = audit_yield_numerator.audit_fiscal_payloads(
            payloads['after'], after.parse_yield_split_adjustments(
                after.load_json(args.split_adjustments, dict), allow_missing_active=True))
        comparison = compare_payloads(payloads['before'], payloads['after'])
        comparison['stock_columns'] = compare_stock_columns(args.output_dir / 'before.sqlite',
                                                            args.output_dir / 'after.sqlite')
        comparison.update(as_of=args.as_of.isoformat(), baseline_head=head, coverage=coverage,
                          missing_inputs=[key for key, value in [('forecasts', args.forecasts),
                                                               ('price_session_meta', args.price_meta)] if value is None or not value.exists()],
                          inputs={key: fingerprint(getattr(args, key)) for key in (
                              'financials', 'sectors', 'tickers', 'fiscal_dividends', 'calendar_dividends',
                              'forecasts', 'prices', 'split_adjustments', 'before_actions', 'after_actions',
                              'before_extracted', 'after_extracted')},
                          before_code_sha256=fingerprint(before_path)['sha256'],
                          after_code_sha256=fingerprint(after_path)['sha256'])
        comparison['inputs'].update({name: fingerprint(root / 'data' / name) for name in
                                     ('yield_numerator_overrides.json', 'forecast_overrides.json', 'dividend_breakdown.json')})
        shutil.copyfile(before_path, args.output_dir / 'before_build_store.py')
        shutil.copyfile(after_path, args.output_dir / 'after_build_store.py')
        for name, document in [('payload_diff', comparison), ('numerator', numerator)]:
            (args.output_dir / f'{name}.json').write_text(json.dumps(document, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(dict(payload_counts=comparison['changed_counts'],
                          stock_column_counts=comparison['stock_columns']['changed_counts'],
                          fiscal_series_audit=numerator['fiscal_series_audit'],
                          before_bad=len(numerator['before_bad']), after_bad=len(numerator['after_bad']),
                          newly_bad=numerator['newly_bad']), ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, default=ROOT)
    parser.add_argument('--before-ref', default='HEAD', help='比較基準のGit ref（既定HEAD）')
    for option in ('before-code', 'after-code', 'forecasts', 'price-meta'):
        parser.add_argument('--' + option, type=Path)
    for option, default in [('financials', ROOT / 'data/all_financials.json'),
                            ('sectors', ROOT / 'data/sector_stats.json'),
                            ('tickers', ROOT / 'data/tickers.json'),
                            ('before-actions', ROOT / 'data/stock_actions_manual.json'),
                            ('after-actions', ROOT / 'data/stock_actions_manual.json'),
                            ('before-extracted', ROOT / 'data/stock_actions_extracted.json'),
                            ('after-extracted', ROOT / 'data/stock_actions_extracted.json')]:
        parser.add_argument('--' + option, type=Path, default=default)
    for option in ('prices', 'split-adjustments', 'fiscal-dividends', 'calendar-dividends', 'output-dir'):
        parser.add_argument('--' + option, type=Path, required=True)
    parser.add_argument('--as-of', type=date.fromisoformat, required=True)
    run(parser.parse_args())


if __name__ == '__main__':
    main()
