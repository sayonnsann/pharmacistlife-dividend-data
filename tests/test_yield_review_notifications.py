from fiscal_fixtures import annual_report_fixture
import json
import io
import sqlite3
import sys
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import audit_yield_numerator as audit
import yield_review_notifications as notify


class FakeGitHub:
    repository = "owner/repo"

    def __init__(self, prs=(), issues=(), labels=()):
        self.prs, self.issues, self.labels = list(prs), list(issues), list(labels)
        self.calls = []

    def pages(self, path, **query):
        return {"pulls": self.prs, "issues": self.issues, "labels": self.labels}[path]

    def request(self, method, path, data=None):
        self.calls.append((method, path, data))
        if method == "POST" and path == "issues":
            self.issues.append({"number": 91, "title": data["title"]})
            return self.issues[-1]


NOW = datetime(2026, 10, 5, 23, 15, tzinfo=timezone.utc)


def pr(age, **kwargs):
    return {"number": 17, "title": notify.PR_PREFIX + " 2026-10-01", "state": "open",
            "created_at": (NOW - age).isoformat(), **kwargs}


@pytest.mark.parametrize("age,expected", [(timedelta(hours=72) - timedelta(seconds=1), 0),
                                          (timedelta(hours=72), 1),
                                          (timedelta(hours=72, seconds=1), 1)])
def test_exact_72_hour_boundary(age, expected):
    assert len(notify.aged_prs([pr(age)], NOW)) == expected


def test_age_ignores_business_days_and_timezone_and_other_prs():
    # 金曜から月曜も暦日の72時間。+09:00表記でも同じ時点。
    row = pr(timedelta(hours=72))
    row["created_at"] = (NOW - timedelta(hours=72)).astimezone(timezone(timedelta(hours=9))).isoformat()
    rows = [row, {**row, "title": "別のPR"}, {**row, "state": "closed"}]
    assert len(notify.aged_prs(rows, NOW)) == 1


def test_create_once_then_comment_existing_issue_and_close_on_zero():
    api = FakeGitHub(prs=[pr(timedelta(hours=80))])
    notify.notify_stale(api, NOW)
    create = [call for call in api.calls if call[:2] == ("POST", "issues")]
    assert len(create) == 1
    assert create[0][2]["labels"] == ["needs-review"]
    body = create[0][2]["body"]
    assert "#17" in body and "3.33日" in body and "annual" in body and "streak" in body
    notify.notify_stale(api, NOW)
    assert api.calls[-1][:2] == ("POST", "issues/91/comments")
    assert len([call for call in api.calls if call[:2] == ("POST", "issues")]) == 1
    api.prs = []
    notify.notify_stale(api, NOW)
    assert api.calls[-1] == ("PATCH", "issues/91", {"state": "closed"})


def test_issue_search_excludes_prs_and_closes_old_duplicates():
    api = FakeGitHub(issues=[{"number": 3, "title": notify.STALE_TITLE, "pull_request": {}},
                            {"number": 4, "title": notify.STALE_TITLE},
                            {"number": 5, "title": notify.STALE_TITLE}])
    notify.sync_issue(api, notify.STALE_TITLE, "needs-review", "点検結果")
    assert api.calls == [("POST", "issues/4/comments", {"body": "点検結果"}),
                         ("PATCH", "issues/5", {"state": "closed"})]


def guard_report(reason="kouhaitou_split_unreflected"):
    payload = {"name": "テスト社", "dividendSeries": {"basis": "fiscal"}, "annual": {"2026": 40},
               "dividendYieldBasis": {"source": "daily_csv_split_guard", "guardReason": reason,
                                      "annualDividend": 30, "seriesLatestDividend": 40}}
    return audit.audit_fiscal_payloads({"1234": payload},
                                      [{"code": "1234", "execution_date": "2026-09-29", "ratio": 2}])


def test_unrefflected_guard_create_comment_and_close_with_requested_values(tmp_path):
    api = FakeGitHub()
    report = guard_report()
    summary = tmp_path / "summary.md"
    notify.notify_guards(api, report, summary)
    body = api.calls[-1][2]["body"]
    assert api.calls[-1][2]["labels"] == ["split-unreflected"]
    for text in ("1234", "テスト社", "2026-09-29", "2倍", "40", "30"):
        assert text in body
    notify.notify_guards(api, report, summary)
    assert api.calls[-1][:2] == ("POST", "issues/91/comments")
    notify.notify_guards(api, audit.audit_fiscal_payloads({"1234": {"dividendSeries": {"basis": "calendar"}}}, []), summary)
    assert api.calls[-1] == ("PATCH", "issues/91", {"state": "closed"})


def test_ratio_only_goes_to_step_summary_and_closes_unreflected_issue(tmp_path):
    api = FakeGitHub(issues=[{"number": 10, "title": notify.SPLIT_TITLE}])
    summary = tmp_path / "summary.md"
    notify.notify_guards(api, guard_report("split_like_ratio"), summary)
    assert "1234" in summary.read_text() and "比率からの推定" in summary.read_text()
    assert api.calls == [("PATCH", "issues/10", {"state": "closed"})]


@pytest.mark.parametrize("patch", [{"split_records_available": False}, {"guard_mismatch_count": 1},
                                   {"mismatch_count": 1}])
def test_missing_split_input_or_failed_audit_never_closes_issue(tmp_path, patch):
    api = FakeGitHub(issues=[{"number": 10, "title": notify.SPLIT_TITLE}])
    with pytest.raises(ValueError):
        notify.notify_guards(api, {**guard_report("split_like_ratio"), **patch}, tmp_path / "summary.md")
    assert not api.calls


def test_unreflected_is_notified_even_when_another_audit_has_a_mismatch(tmp_path):
    api = FakeGitHub()
    with pytest.raises(ValueError):
        notify.notify_guards(api, {**guard_report(), "mismatch_count": 1}, tmp_path / "summary.md")
    assert api.calls[-1][:2] == ("POST", "issues")
    assert "検査にも不一致" in api.calls[-1][2]["body"]


def test_large_reminder_still_creates_only_one_issue():
    api = FakeGitHub(prs=[pr(timedelta(days=4), number=i, title=notify.PR_PREFIX + "x" * 200) for i in range(500)])
    notify.notify_stale(api, NOW)
    bodies = [data["body"] for method, path, data in api.calls if path == "issues" or path.endswith("/comments")]
    assert len(bodies) > 1 and all(len(body) <= 50000 for body in bodies)
    assert len([call for call in api.calls if call[:2] == ("POST", "issues")]) == 1
    assert "#499" in "\n".join(bodies)


def test_api_list_failure_is_not_an_empty_result():
    api = FakeGitHub()
    def fail(*args, **kwargs):
        raise RuntimeError("API失敗")
    api.pages = fail
    with pytest.raises(RuntimeError):
        notify.notify_stale(api, NOW)
    assert not api.calls


def test_api_pagination_reads_past_first_hundred():
    api = notify.GitHub("owner/repo", "fake-token")
    paths = []
    def request(method, path, data=None):
        paths.append(path)
        return [{"number": i} for i in range(100)] if "&page=1" in path else [{"number": 101}]
    api.request = request
    assert len(api.pages("issues", state="open")) == 101
    assert len(paths) == 2 and "page=2" in paths[1]


def test_markdown_cells_cannot_create_rows_or_mentions():
    output = notify.table(["題名"], [["a|b\n@owner<script>"]])
    assert "a&#124;b ＠owner&lt;script&gt;" in output


def test_cli_error_does_not_expose_exception_or_private_json(tmp_path, monkeypatch, capsys):
    report = tmp_path / "private.json"
    report.write_text('{PRIVATE_SENTINEL')
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("GH_TOKEN", "TOKEN_SENTINEL")
    monkeypatch.setattr(sys, "argv", ["notify", "guards", "--report", str(report)])
    with pytest.raises(SystemExit) as error:
        notify.main()
    assert "SENTINEL" not in str(error.value)
    assert "SENTINEL" not in capsys.readouterr().out


def test_build_report_uses_exact_split_snapshot_and_finished_payload_without_logging_values(tmp_path):
    store = audit.store
    database = tmp_path / "stocks.sqlite"
    output = tmp_path / "report.json"
    snapshot = [{"code": "1234", "execution_date": "2026-09-29", "ratio": 3, "active": True}]
    fiscal = {"1234": {"fiscalMonth": 3, "series": {2026: 98765.4321}, "appliedActions": [],
                       "externalSource": None, "externalYears": [], "connectionStatus": "connected",
                       "connectionReason": "PRIVATE_SENTINEL"}}
    with (patch.object(store, "load_daily_prices", return_value=({"1234": 1000}, {"1234": 12345}, "fixture")),
          patch.object(store, "load_price_session_meta", return_value=None),
          patch.object(store, "load_yield_split_adjustments", return_value=snapshot) as fetch_splits,
          patch.object(store, "load_yield_source_year", return_value=2025),
          redirect_stdout(io.StringIO()) as log):
        store.create_database(database, [{"code": "1234", "name": "テスト社"}], {}, {}, {},
                              [Path("fixture")] * 4, "fixture.csv", {}, fiscal_by_code=annual_report_fixture(fiscal),
                              today=NOW.date(), yield_guard_report_path=output)
    report = json.loads(output.read_text())
    with sqlite3.connect(database) as conn:
        payload = json.loads(conn.execute("SELECT payload FROM stocks").fetchone()[0])
    assert report == audit.audit_fiscal_payloads({"1234": payload}, snapshot)
    assert report["guarded"][0]["kouhaitou_splits"] == [{"execution_date": "2026-09-29", "ratio": 3}]
    assert report["guarded"][0]["series_latest_dividend"] == 98765.4321
    fetch_splits.assert_called_once()  # レポート用の再取得をしない。
    assert "98765.4321" not in log.getvalue() and "PRIVATE_SENTINEL" not in log.getvalue()
