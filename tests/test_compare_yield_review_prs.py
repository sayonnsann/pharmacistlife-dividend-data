import json
import csv
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import compare_yield_review_prs as review
import store_review_inputs as inputs
from test_summarize_yield_review import report

SHA = "a" * 40


def pr(**kwargs):
    return {"number": 12, "title": review.PR_PREFIX + " テスト", "state": "open",
            "head": {"sha": SHA, "repo": {"full_name": "owner/repo"}}, **kwargs}


class FakeGitHub:
    repository = "owner/repo"

    def __init__(self):
        self.prs = [pr()]
        self.comments = []
        self.calls = []
        self.current = pr()

    def pages(self, path, **query):
        return self.prs if path == "pulls" else self.comments

    def request(self, method, path, data=None):
        self.calls.append((method, path, data))
        if path.startswith("pulls/"):
            return self.current
        if method == "POST":
            self.comments.append({"id": 100 + len(self.comments), "body": data["body"], "user": {"type": "Bot"}})


def test_only_same_repository_review_prs_are_selected():
    api = FakeGitHub()
    api.prs += [pr(title="他のPR"), pr(head={"sha": SHA, "repo": {"full_name": "fork/repo"}}), pr(head={"sha": SHA, "repo": None})]
    assert review.review_prs(api) == [api.prs[0]]


def test_comments_are_updated_and_surplus_parts_removed_without_touching_human():
    api = FakeGitHub()
    review.publish_comparison(api, 12, ["表1", "表2"])
    assert len(api.comments) == 2
    api.comments.insert(0, {"id": 10, "body": review.MARKER + "人のコメント", "user": {"type": "User"}})
    api.calls = []
    review.publish_comparison(api, 12, ["更新した表"])
    assert api.calls[0][:2] == ("PATCH", "issues/comments/100")
    assert api.calls[1][:2] == ("DELETE", "issues/comments/101")
    assert all("10" != path.rsplit("/", 1)[-1] for _, path, _ in api.calls)


def test_no_pr_does_not_download_private_inputs_or_modify_comments(tmp_path):
    api = FakeGitHub()
    api.prs = []
    prepare = Mock(side_effect=AssertionError("must not download"))
    assert review.run(api, tmp_path, tmp_path / "summary", prepare_inputs=prepare)
    prepare.assert_not_called()
    assert not api.calls


@pytest.mark.parametrize("failure_stage", ["prepare", "compare"])
def test_failure_reports_unconfirmed_without_private_exception(tmp_path, monkeypatch, failure_stage, capsys):
    api = FakeGitHub()
    monkeypatch.setattr(review.subprocess, "check_output", lambda *args, **kwargs: SHA)
    def fail(*args):
        raise ValueError("PRIVATE_SENTINEL 987654.321")
    prepare = fail if failure_stage == "prepare" else lambda path: None
    compare = fail if failure_stage == "compare" else lambda *args: ["成功"]
    assert not review.run(api, tmp_path, tmp_path / "summary", prepare_inputs=prepare, compare=compare)
    body = api.comments[0]["body"]
    assert "未確認" in body and "取り込まず" in body
    assert "PRIVATE_SENTINEL" not in body + (tmp_path / "summary").read_text() + capsys.readouterr().out


@pytest.mark.parametrize("change", [{"state": "closed"}, {"head": {"sha": "b" * 40}}])
def test_changed_or_closed_pr_does_not_get_obsolete_comparison(tmp_path, monkeypatch, change):
    api = FakeGitHub()
    api.current = {**pr(), **change}
    monkeypatch.setattr(review.subprocess, "check_output", lambda *args, **kwargs: SHA)
    review.run(api, tmp_path, tmp_path / "summary", prepare_inputs=lambda path: None, compare=lambda *args: ["比較表"])
    assert not api.comments


def test_comparison_runs_main_code_with_two_ledgers_and_discards_all_child_logs(tmp_path, monkeypatch):
    api = FakeGitHub()
    commands = []
    monkeypatch.setattr(review.subprocess, "check_output", lambda *args, **kwargs: b'{"events": []}')
    urls = []
    def download(url, path):
        urls.append(url)
        path.write_bytes(b'{"events": []}')
        return 0
    monkeypatch.setattr(review, "download", download)
    def run(args, **kwargs):
        commands.append((args, kwargs))
        output = Path(args[args.index("--output-dir") + 1])
        output.mkdir()
        (output / "payload_diff.json").write_text(json.dumps(report()))
        (output / "numerator.json").write_text(json.dumps({"fiscal_series_audit": {
            "mismatch_count": 0, "guard_mismatch_count": 0, "guarded": [{"PRIVATE_SENTINEL": 987654321}]}}))
    monkeypatch.setattr(review.subprocess, "run", run)
    from datetime import date
    bodies = review.compare_one(api, tmp_path, tmp_path, pr(), SHA, date(2026, 10, 5), tmp_path)
    args, options = commands[0]
    assert options == {"check": True, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    assert args[args.index("--before-code") + 1] == args[args.index("--after-code") + 1]
    assert "before-stock_actions_manual.json" in args[args.index("--before-actions") + 1]
    assert "after-stock_actions_manual.json" in args[args.index("--after-actions") + 1]
    assert "after-stock_actions_extracted.json" in args[args.index("--after-extracted") + 1]
    assert len(urls) == 2 and all(f"/owner/repo/{SHA}/data/" in url for url in urls)
    assert not api.calls
    assert "PRIVATE_SENTINEL" not in "\n".join(bodies)


def test_ftps_private_inputs_and_public_prices_use_existing_workflow_sources(tmp_path, monkeypatch):
    for key, value in [("FTP_HOST", "private.example"), ("FTP_USER", "user"), ("FTP_PASS", "TOKEN_SENTINEL"), ("FTP_REMOTE_DIR", "/private/data")]:
        monkeypatch.setenv(key, value)
    calls = []
    def download(url, path, credentials=None):
        calls.append((url, path, credentials))
        documents = {"fiscal_dividends.json": {str(i): {} for i in range(3000)},
                     "forecasts_state.json": {"stocks": {}}, "calendar_dividends_frozen.json": {"stocks": {"1234": {}}},
                     "split_adjustments.json": {"adjustments": []}, "price_update_meta.json": {}}
        path.write_text(json.dumps(documents.get(path.name, {})))
        return 0
    monkeypatch.setattr(inputs, "download", download)
    inputs.prepare(tmp_path)
    assert all(url.startswith("ftp://private.example/private/data/") and auth == "user:TOKEN_SENTINEL" for url, _, auth in calls[:3])
    assert calls[3][0] == inputs.store.DAILY_PRICE_CSV_URL_NO_CACHE
    assert all(auth is None for _, _, auth in calls[3:])


def test_downloader_suppresses_output_and_requires_ftps_encryption(tmp_path, monkeypatch):
    run = Mock(return_value=Mock(returncode=78))
    monkeypatch.setattr(inputs.subprocess, "run", run)
    output = tmp_path / "private.json"
    output.write_text("PRIVATE_SENTINEL")
    assert inputs.download("ftp://private.example/file", output, "user:TOKEN_SENTINEL") == 78
    args, kwargs = run.call_args
    assert "--ssl-reqd" in args[0]
    assert kwargs["stdout"] == subprocess.DEVNULL and kwargs["stderr"] == subprocess.DEVNULL
    assert not output.exists()


def test_real_audit_builds_two_synthetic_stores_and_summary_is_safe(tmp_path, monkeypatch, capsys):
    """実データを再生成せず、隔離した架空の入力で子プロセスまで接続する。"""
    root = tmp_path / "main"
    source = Path(__file__).resolve().parents[1]
    for folder in ("scripts", "data", "edinet"):
        (root / folder).mkdir(parents=True)
    for script in ("build_store.py", "audit_store_diff.py", "audit_yield_numerator.py"):
        shutil.copyfile(source / "scripts" / script, root / "scripts" / script)
    for filename, document in [("all_financials.json", [{"code": "1234", "name": "テスト社"}]),
                               ("tickers.json", []), ("sector_stats.json", {})]:
        (root / "data" / filename).write_text(json.dumps(document))
    (root / "edinet" / "1234.json").write_text('{"dps": {"2025": 10}}')
    fixtures = tmp_path / "inputs"
    fixtures.mkdir()
    for filename, document in [("fiscal_dividends.json", {"1234": {"fiscalMonth": 3, "series": {"2026": 98765.4321},
                                "connection": {"status": "connected", "reason": "PRIVATE_SENTINEL"}}}),
                               ("forecasts_state.json", {"stocks": {}}),
                               ("calendar_dividends_frozen.json", {"stocks": {}}),
                               ("split_adjustments.json", {"adjustments": [{"code": "1234", "execution_date": "2026-09-29", "ratio": 3, "active": True}]})]:
        (fixtures / filename).write_text(json.dumps(document))
    with (fixtures / "database.csv").open("w") as output:
        writer = csv.writer(output)
        writer.writerow([""] * 18 + ["2026/10/05 07:00:00"])
        row = [""] * 19
        row[0], row[4], row[18] = "1234", "12345", "100000"
        writer.writerow(row)
    event = {"eventId": "synthetic-split", "securityCode": "1234", "action": "split", "effectiveDate": "2026-10-01",
             "oldShares": 1, "newShares": 3, "status": "confirmed", "applyDividendAdjustment": True,
             "epsAdjustedByIssuer": False, "source": {"url": "https://example.com/synthetic"}}
    monkeypatch.setattr(review.subprocess, "check_output", lambda *args, **kwargs: b'{"events": []}')
    def download(url, path):
        path.write_text(json.dumps({"events": [event] if path.name == "after-stock_actions_manual.json" else []}))
        return 0
    monkeypatch.setattr(review, "download", download)
    result = tmp_path / "comparison"
    result.mkdir()
    from datetime import date
    body = "\n".join(review.compare_one(FakeGitHub(), root, fixtures, pr(), SHA, date(2026, 10, 5), result))
    assert "| 画面 | 配当グラフ | annual | 1 |" in body
    assert "| 1234 | 外れる |" in body
    assert (result / "result" / "before.sqlite").exists() and (result / "result" / "after.sqlite").exists()
    assert "PRIVATE_SENTINEL" not in body + capsys.readouterr().out
    assert "98765.4321" not in body and "12345" not in body


def test_missing_forecasts_and_optional_calendar_follow_production_fallback(tmp_path, monkeypatch):
    for key in ("FTP_HOST", "FTP_USER", "FTP_PASS", "FTP_REMOTE_DIR"):
        monkeypatch.setenv(key, "/private" if key == "FTP_REMOTE_DIR" else "example")
    def download(url, path, credentials=None):
        if path.name in ("forecasts_state.json", "calendar_dividends_frozen.json"):
            return 78
        document = {str(i): {} for i in range(3000)} if path.name == "fiscal_dividends.json" else {"adjustments": []}
        path.write_text(json.dumps(document))
        return 0
    monkeypatch.setattr(inputs, "download", download)
    inputs.prepare(tmp_path)
    assert not (tmp_path / "forecasts_state.json").exists()
    assert not (tmp_path / "calendar_dividends_frozen.json").exists()
    assert inputs.store.load_forecasts(tmp_path / "forecasts_state.json") == {}
