#!/usr/bin/env python3
"""レビュー待ちPRの台帳だけを読み、隔離した比較結果をコメントする。"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from store_review_inputs import download, prepare
from summarize_yield_review import summarize
from yield_review_notifications import GitHub, PR_PREFIX, REVIEW_GUIDE, table

MARKER = "<!-- yield-store-review:"
FAILURE = ("## 画面データの全項目比較: 未確認\n\n"
           + table(["結果", "オーナーの次の操作"],
                   [["入力取得・ビルド・比較・検証のいずれかに失敗", "取り込まず、台帳とActionsを確認して再実行してください"]])
           + "\n非公開の入力やエラー本文は掲載しません。\n\n" + REVIEW_GUIDE)


def review_prs(api):
    return [pr for pr in api.pages("pulls", state="open", base="main")
            if pr.get("title", "").startswith(PR_PREFIX)
            and (pr.get("head", {}).get("repo") or {}).get("full_name") == api.repository]


def publish_comparison(api, pr_number, bodies):
    """同じPRでは比較コメントを更新する。2回/日でもコメントを増やさない。"""
    existing = [row for row in api.pages(f"issues/{pr_number}/comments")
                if row.get("user", {}).get("type") == "Bot" and row.get("body", "").startswith(MARKER)]
    for index, body in enumerate(bodies):
        body = f"{MARKER}{index + 1} -->\n" + body
        if index < len(existing):
            api.request("PATCH", f"issues/comments/{int(existing[index]['id'])}", {"body": body})
        else:
            api.request("POST", f"issues/{pr_number}/comments", {"body": body})
    for comment in existing[len(bodies):]:
        api.request("DELETE", f"issues/comments/{int(comment['id'])}")


def compare_one(api, root, inputs, pr, baseline_sha, today, output):
    head_sha = pr["head"]["sha"]
    if not re.fullmatch(r"[0-9a-f]{40}", head_sha):
        raise ValueError("PRの参照形式が不正です")
    paths = {}
    for kind, filename in [("actions", "stock_actions_manual.json"), ("extracted", "stock_actions_extracted.json")]:
        baseline = output / f"before-{filename}"
        candidate = output / f"after-{filename}"
        baseline.write_bytes(subprocess.check_output(
            ["git", "-C", str(root), "show", f"{baseline_sha}:data/{filename}"], stderr=subprocess.DEVNULL))
        # 抽出台帳は約20MBあり、Contents APIのJSON本文には入らない。
        # Publicリポジトリのrawファイルを不変のコミット指定で取得する。
        if download(f"https://raw.githubusercontent.com/{api.repository}/{head_sha}/data/{filename}", candidate):
            raise ValueError("PRの台帳を取得できません")
        paths[kind] = (baseline, candidate)
    report_dir = output / "result"
    # PR由来のPythonやワークフローを実行しない。台帳以外は全てmainの入力。
    args = [sys.executable, str(root / "scripts/audit_store_diff.py"), "--repo", str(root),
            "--before-code", str(root / "scripts/build_store.py"), "--after-code", str(root / "scripts/build_store.py"),
            "--before-actions", str(paths["actions"][0]), "--after-actions", str(paths["actions"][1]),
            "--before-extracted", str(paths["extracted"][0]), "--after-extracted", str(paths["extracted"][1]),
            "--prices", str(inputs / "database.csv"), "--split-adjustments", str(inputs / "split_adjustments.json"),
            "--forecasts", str(inputs / "forecasts_state.json"), "--fiscal-dividends", str(inputs / "fiscal_dividends.json"),
            "--calendar-dividends", str(inputs / "calendar_dividends_frozen.json"),
            "--as-of", today.isoformat(), "--output-dir", str(report_dir)]
    if (inputs / "price_update_meta.json").exists():
        args.extend(["--price-meta", str(inputs / "price_update_meta.json")])
    # audit_store_diffの標準出力には非公開配当が含まれる。失敗時のtracebackも破棄。
    subprocess.run(args, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    report = json.loads((report_dir / "payload_diff.json").read_text())
    numerator = json.loads((report_dir / "numerator.json").read_text())
    if not report["before_count"] or report["before_count"] != report["after_count"]:
        raise ValueError("比較対象の銘柄数が不一致です")
    report["audit_counts"] = {key: numerator["fiscal_series_audit"][key]
                              for key in ("mismatch_count", "guard_mismatch_count")}
    bodies = summarize(report)
    refs = "\n" + table(["比較基準", "コミット / 日付"], [["main", baseline_sha], ["PR", head_sha], ["計算日（日本時間）", today.isoformat()]])
    return [body + refs for body in bodies]


def run(api, root, summary_path, *, prepare_inputs=prepare, compare=compare_one):
    prs = review_prs(api)
    if not prs:
        return True
    baseline_sha = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
    today = datetime.now(ZoneInfo("Asia/Tokyo")).date()
    success = True
    # 非公開入力・SQLite・生の比較JSONは一時領域だけ。artifactにも保存しない。
    with tempfile.TemporaryDirectory(prefix="yield-review-", dir=os.environ.get("RUNNER_TEMP")) as directory:
        temp = Path(directory)
        inputs = temp / "inputs"
        ready = True
        try:
            prepare_inputs(inputs)
        except Exception:
            ready = False
        for pr in prs:
            bodies = [FAILURE]
            try:
                if not ready:
                    raise ValueError("入力を取得できません")
                with tempfile.TemporaryDirectory(prefix=f"pr-{int(pr['number'])}-", dir=temp) as output:
                    bodies = compare(api, root, inputs, pr, baseline_sha, today, Path(output))
            except Exception:
                success = False
            # 計算中に閉じられたPR・台帳が更新されたPRへ古い結果を貼らない。
            current = api.request("GET", f"pulls/{int(pr['number'])}")
            if current["state"] != "open":
                continue
            if current["head"]["sha"] != pr["head"]["sha"]:
                success = False
                continue
            publish_comparison(api, int(pr["number"]), bodies)
            with summary_path.open("a", encoding="utf-8") as summary:
                summary.write(f"\nPR #{int(pr['number'])}\n\n" + "\n".join(bodies))
    return success


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    try:
        api = GitHub(os.environ["GITHUB_REPOSITORY"], os.environ["GH_TOKEN"])
        success = run(api, args.repo.resolve(), Path(os.environ["GITHUB_STEP_SUMMARY"]))
    except Exception:
        raise SystemExit("比較・コメント処理に失敗しました（非公開の応答内容は表示しません）") from None
    if not success:
        raise SystemExit("比較に未確認が残りました。PRコメントを確認してください")


if __name__ == "__main__":
    main()
