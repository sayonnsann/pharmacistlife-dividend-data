"""The audit must expose schema changes and count stocks rather than changed years."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from audit_store_diff import compare_payloads


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
