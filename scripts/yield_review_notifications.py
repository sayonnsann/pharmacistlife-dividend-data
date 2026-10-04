#!/usr/bin/env python3
"""利回り台帳の放置・分割未反映を、重複しないissueで知らせる。"""
from __future__ import annotations

import argparse
import html
import json
import math
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

PR_PREFIX = "レビュー待ち: 利回り用台帳"
STALE_TITLE = "利回り用台帳: 3日以上レビュー待ちのPRがあります"
SPLIT_TITLE = "利回り用台帳: 分割が画面データに未反映の銘柄があります"
REVIEW_GUIDE = """取り込む前に、PRの「画面データの全項目比較」コメントを確認してください。コメントがない・比較できなかった場合は取り込まず、Actionsを再実行してください。

| 見る表 | 確かめること |
|---|---|
| 変わった項目と銘柄数 | 想定した銘柄・項目の変更か。画面と一覧の全項目を点検しています |
| annualが変わる銘柄 | 配当グラフが変わる銘柄です。分割を二重に補正していないか確認してください |
| streakの年数が減る銘柄 | 連続増配・非減配の年数が短くなる理由を確認してください |
| 安全装置が外れる・付く銘柄 | 外れる銘柄は台帳に分割が正しく入ったか、付く銘柄は新しい食い違いがないか確認してください |
| 未確認の入力・検査 | 未確認や検査不一致が残る場合は、先に原因を確かめてください |

この通知から自動で取り込むことはありません。"""


class GitHub:
    """APIをここだけに隔離。本文・認証情報を例外やログへ出さない。"""
    def __init__(self, repository, token):
        if not re.fullmatch(r"[\w.-]+/[\w.-]+", repository) or not token:
            raise ValueError("GitHubの設定が不足しています")
        self.repository = repository
        self.token = token

    def request(self, method, path, data=None):
        request = Request(
            f"https://api.github.com/repos/{self.repository}/{path}",
            data=json.dumps(data).encode() if data is not None else None,
            method=method,
            headers={"Authorization": f"Bearer {self.token}",
                     "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28", "Content-Type": "application/json"},
        )
        try:
            with urlopen(request, timeout=60) as response:
                raw = response.read()
            return json.loads(raw) if raw else None
        except HTTPError as error:
            raise RuntimeError(f"GitHub API失敗（HTTP {error.code}）") from None
        except (URLError, OSError, ValueError):
            raise RuntimeError("GitHub APIの通信・応答に失敗しました") from None

    def pages(self, path, **query):
        result = []
        page = 1
        while True:
            rows = self.request("GET", path + "?" + urlencode({**query, "per_page": 100, "page": page}))
            if not isinstance(rows, list):
                raise RuntimeError("GitHub APIの一覧形式が不正です")
            result.extend(rows)
            if len(rows) < 100:
                return result
            page += 1


def cell(value):
    return html.escape(str(value), quote=False).replace("|", "&#124;").replace("\r", " ").replace("\n", " ").replace("@", "＠").replace("`", "&#96;")


def code(value):
    return value if isinstance(value, str) and re.fullmatch(r"[0-9A-Z]{4}", value) else "不明"


def number(value):
    return str(value) if type(value) in (int, float) and math.isfinite(value) else "不明"


def table(headers, rows):
    lines = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    lines.extend("| " + " | ".join(cell(value) for value in row) + " |" for row in rows)
    return "\n".join(lines) + "\n"


def split_tables(intro, sections, limit=50000):
    """GitHubの本文上限を超える場合も、表を繰り返して全行を載せる。"""
    parts = []
    current = intro
    for title, headers, rows in sections:
        heading = f"\n### {title}\n\n" + table(headers, [])
        if len(current) + len(heading) > limit:
            parts.append(current)
            current = intro
        current += heading
        for row in rows or [["なし"] + ["—"] * (len(headers) - 1)]:
            line = table(headers, [row]).splitlines()[-1] + "\n"
            if len(current) + len(line) > limit:
                parts.append(current)
                current = intro + heading
            current += line
    parts.append(current)
    return parts


def aged_prs(prs, now):
    return [(pr, now - datetime.fromisoformat(pr["created_at"].replace("Z", "+00:00")))
            for pr in prs if pr.get("state") == "open" and pr.get("title", "").startswith(PR_PREFIX)
            and now - datetime.fromisoformat(pr["created_at"].replace("Z", "+00:00")) >= timedelta(hours=72)]


def stale_body(aged, now):
    rows = [(f"#{int(pr['number'])}", pr["title"], f"{age.total_seconds() / 86400:.2f}日（{age.total_seconds() / 3600:.1f}時間）")
            for pr, age in aged]
    return split_tables(
        f"{now.astimezone(ZoneInfo('Asia/Tokyo')).date().isoformat()}（日本時間）の点検: 作成から72時間以上たったPRが{len(rows)}件あります。\n\n" + REVIEW_GUIDE,
        [("レビュー待ちのPR", ["PR番号", "題名", "経過日数"], rows)])


def sync_issue(api, title, label, body):
    """body=Noneは解消。API失敗を0件とは扱わず、一覧取得後だけ変更する。"""
    issues = [issue for issue in api.pages("issues", state="open")
              if "pull_request" not in issue and issue.get("title") == title]
    if body is None:
        for issue in issues:
            api.request("PATCH", f"issues/{int(issue['number'])}", {"state": "closed"})
        return
    parts = [body] if isinstance(body, str) else body
    if issues:
        # 既存issueを使い続ける。以前の重複があれば1件へ集約する。
        issues.sort(key=lambda issue: issue["number"])
        for part in parts:
            api.request("POST", f"issues/{int(issues[0]['number'])}/comments", {"body": part})
        for issue in issues[1:]:
            api.request("PATCH", f"issues/{int(issue['number'])}", {"state": "closed"})
    else:
        labels = api.pages("labels")
        if not any(row.get("name") == label for row in labels):
            api.request("POST", "labels", {"name": label, "color": "d93f0b"})
        created = api.request("POST", "issues", {"title": title, "labels": [label], "body": parts[0]})
        for part in parts[1:]:
            api.request("POST", f"issues/{int(created['number'])}/comments", {"body": part})


def notify_stale(api, now):
    aged = aged_prs(api.pages("pulls", state="open"), now)
    sync_issue(api, STALE_TITLE, "needs-review", stale_body(aged, now) if aged else None)


GUARD_HEADERS = ["銘柄コード", "名称", "kouhaitou-dbの検出日 / 比率", "グラフの最新値（円）", "旧分子（円）"]


def guard_rows(rows):
    def records(row):
        def day(item):
            value = item.get("execution_date")
            return value if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) else "不明"
        return "; ".join(f"{day(item)} / {number(item.get('ratio'))}倍"
                         for item in row.get("kouhaitou_splits", [])) or "記録なし"
    return [(code(row.get("code")), str(row.get("name") or "不明")[:120], records(row),
             number(row.get("series_latest_dividend")), number(row.get("old_numerator"))) for row in rows]


def notify_guards(api, report, summary_path):
    unreflected = [row for row in report["guarded"] if row["guard_reason"] == "kouhaitou_split_unreflected"]
    ratio = [row for row in report["guarded"] if row["guard_reason"] == "split_like_ratio"]
    summary = ("## 利回りの安全装置の日次点検\n\n"
               f"分割未反映: {len(unreflected)}銘柄 / 比率からの推定: {len(ratio)}銘柄\n\n"
               "### 分割未反映（issueで通知）\n\n" + table(GUARD_HEADERS, guard_rows(unreflected))
               + "\n### 比率からの推定（issueの対象外）\n\n" + table(GUARD_HEADERS, guard_rows(ratio)))
    with summary_path.open("a", encoding="utf-8") as output:
        output.write(summary)
    if (not report.get("total") or not report.get("split_records_available")
            or any(row.get("guard_reason") not in ("kouhaitou_split_unreflected", "split_like_ratio") for row in report["guarded"])):
        raise ValueError("点検に必要な分割記録がないか、レポートが不正です。既存issueを維持します")
    audit_failed = report.get("guard_mismatch_count") != 0 or report.get("mismatch_count") != 0
    if audit_failed and not unreflected:
        raise ValueError("画面データの検査が不一致です。既存issueを維持します")
    body = split_tables(
        f"{datetime.now(ZoneInfo('Asia/Tokyo')).date().isoformat()}の点検: 分割が台帳に入っていない疑いがあり、利回りの安全装置が働いています。\n\n"
        + "検出日はkouhaitou-dbの execution_date（記録上の日付）です。旧分子は安全装置が選んだCSVの年間配当です。\n\n"
        + ("画面データの検査にも不一致があります。取り込む前に点検してください。\n\n" if audit_failed else "") + REVIEW_GUIDE,
        [("分割未反映の銘柄", GUARD_HEADERS, guard_rows(unreflected))]) if unreflected else None
    sync_issue(api, SPLIT_TITLE, "split-unreflected", body)
    if audit_failed:
        raise ValueError("分割未反映は通知済みですが、画面データの検査に不一致があります")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=["stale", "guards"])
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    try:
        api = GitHub(os.environ["GITHUB_REPOSITORY"], os.environ["GH_TOKEN"])
        if args.kind == "stale":
            notify_stale(api, datetime.now(timezone.utc))
        else:
            notify_guards(api, json.loads(args.report.read_text()), Path(os.environ["GITHUB_STEP_SUMMARY"]))
    except Exception:
        # JSONやネットワーク例外の本文には入力の一部が含まれ得る。
        raise SystemExit("通知の点検・API操作に失敗しました。既存の通知は解消扱いにせず、再実行してください") from None


if __name__ == "__main__":
    main()
