import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from audit_store_diff import compare_payloads
from summarize_yield_review import summarize


def report():
    before = {"1234": {"annual": {"2025": 987654.321, "PRIVATE_SENTINEL": "SECRET"},
                       "earnings": {"PRIVATE_FINANCIAL": 1234567},
                       "streakIncrease": 5, "streakNonDecrease": 8, "streakBase": 3,
                       "streakIncreaseCapped": True, "streakNoDecreaseBase": 4,
                       "dividendYieldBasis": {"source": "daily_csv_split_guard", "annualDividend": 98765}},
              "5678": {"streakIncrease": 2, "dividendYieldBasis": {"source": "fiscal_series"}}}
    after = {"1234": {"annual": {"2025": 456789.123}, "earnings": {"PRIVATE_FINANCIAL": 999999},
                      "streakIncrease": 3, "streakNonDecrease": 9, "streakBase": 2,
                      "streakIncreaseCapped": False, "streakNoDecreaseBase": None,
                      "dividendYieldBasis": {"source": "fiscal_series", "annualDividend": 54321}},
             "5678": {"streakIncrease": 2, "dividendYieldBasis": {"source": "daily_csv_split_guard"}}}
    result = compare_payloads(before, after)
    result["stock_columns"] = compare_payloads({"1234": {"price": 100}}, {"1234": {"price": 101}})
    result["missing_inputs"] = ["forecasts", "price_session_meta"]
    result["inputs"] = {"fiscal_dividends": {"secret": "DO_NOT_PRINT_INPUTS"}}
    result["audit_counts"] = {"mismatch_count": 0, "guard_mismatch_count": 0}
    return result


def test_counts_all_fields_without_serializing_private_values():
    document = report()
    unchanged = copy.deepcopy(document)
    body = "\n".join(summarize(document))
    for forbidden in ["PRIVATE_SENTINEL", "PRIVATE_FINANCIAL", "SECRET", "987654", "456789", "1234567", "999999", "98765", "54321", "DO_NOT_PRINT_INPUTS"]:
        assert forbidden not in body
    assert "| 画面 | 配当グラフ | annual | 1 |" in body
    assert "| 画面 | 業績の推移 | earnings | 1 |" in body
    assert "| 一覧 | 株価 | price | 1 |" in body
    assert "| 1234 | streakIncrease | 5 | 3 |" in body
    assert "| 1234 | streakBase | 3 | 2 |" in body
    assert "| 1234 | streakNoDecreaseBase | 4 | 算出不可" in body
    assert "streakNonDecrease | 8 | 9" not in body
    assert "streakIncreaseCapped | True" not in body
    assert "| 1234 | 外れる |" in body and "| 5678 | 付く |" in body
    assert "未確認" in body
    assert document == unchanged


def test_no_changes_and_added_removed_and_unknown_fields_are_reported():
    result = compare_payloads({"1234": {}}, {"5678": {"futureField": {"private": 999999}}})
    result["stock_columns"] = compare_payloads({}, {})
    body = "\n".join(summarize(result))
    assert "futureField" in body and "999999" not in body
    assert "| 追加 | 1 |" in body and "| 削除 | 1 |" in body
    assert "| なし |" in body


def test_large_report_keeps_all_codes_and_complete_markdown_tables():
    result = report()
    result["changes"]["streakIncrease"] = [{"code": f"{i:04}", "before": 20, "after": 10} for i in range(4000)]
    parts = summarize(result)
    assert len(parts) > 1
    assert all(len(part) < 51000 for part in parts)
    assert "| 0000 | streakIncrease | 20 | 10 |" in "\n".join(parts)
    assert "| 3999 | streakIncrease | 20 | 10 |" in "\n".join(parts)
    assert all("|---|---|---|---|" in part for part in parts)
