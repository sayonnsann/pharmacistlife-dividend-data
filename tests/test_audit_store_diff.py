"""The audit must expose schema changes and count stocks rather than changed years."""
import sys
import sqlite3
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from audit_store_diff import compare_payloads, compare_stock_columns


def test_counts_nested_changes_once_and_preserves_values():
    before = {'1': {'annual': {'2024': 1, '2025': 2}, 'dividendSeries': {'basis': 'fiscal'}}}
    after = {'1': {'annual': {'2024': 3, '2025': 4}, 'dividendSeries': {'basis': 'fiscal'}}}
    result = compare_payloads(before, after)
    assert result['changed_counts']['annual'] == 1
    assert result['annual_changed_by_basis'] == {'fiscal': ['1'], 'calendar': []}
    assert result['changes']['annual'][0]['before'] == before['1']['annual']


def test_missing_is_not_null_and_added_removed_are_visible():
    result = compare_payloads({'1': {'eps': None}, '2': {'code': '2'}},
                              {'1': {}, '3': {'code': '3'}})
    assert result['changes']['eps'][0]['before_present'] is True
    assert result['changes']['eps'][0]['after_present'] is False
    assert result['added_codes'] == ['3']
    assert result['removed_codes'] == ['2']


def test_every_list_column_is_compared_separately_from_payload(tmp_path):
    paths = [tmp_path / 'before.sqlite', tmp_path / 'after.sqlite']
    for path, yield_, price, payload in zip(paths, [3, 4], [100, 101], ['{}', '{"annual": {}}']):
        with sqlite3.connect(path) as conn:
            conn.execute('CREATE TABLE stocks (code TEXT, yield REAL, price REAL, payload TEXT)')
            conn.execute('INSERT INTO stocks VALUES (?, ?, ?, ?)', ('1234', yield_, price, payload))
    result = compare_stock_columns(*paths)
    assert result['changed_counts']['yield'] == 1
    assert result['changed_counts']['price'] == 1
    assert 'payload' not in result['changed_counts']
