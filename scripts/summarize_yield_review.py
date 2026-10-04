#!/usr/bin/env python3
"""比較JSONから、公開可能な項目だけを選んで日本語の表を作る。"""
from __future__ import annotations

import re

from yield_review_notifications import REVIEW_GUIDE, code, number, split_tables

FIELD_NAMES = {
    "annual": "配当グラフ", "annualPending": "進行中年度の配当", "annualPartial": "一部期間の配当",
    "dividendYield": "配当利回り", "dividendPerShare": "利回り用の年間配当",
    "streakIncrease": "連続増配の年数", "streakNonDecrease": "連続非減配の年数",
    "streakBase": "普通配当の連続増配年数", "streakNoDecreaseBase": "普通配当の連続非減配年数",
    "dividendYieldBasis": "利回りの計算根拠・安全装置",
    "eps": "1株あたり利益", "bps": "1株あたり純資産", "earnings": "業績の推移",
    "forecastDividend": "予想配当", "forecastYield": "予想利回り", "forecastBasis": "予想配当の根拠",
    "confirmedDividend": "直近の確定配当", "dividendSeries": "配当系列の根拠", "splitAdjustment": "分割・併合の補正",
    "payoutRatio": "配当性向", "dividendBreakdown": "普通・記念・特別配当の内訳", "price": "株価",
    "yield": "一覧の配当利回り", "forecast_yield": "一覧の予想利回り",
    "streak": "一覧の連続増配年数", "streak_nd": "一覧の連続非減配年数",
    "streakIncreaseCapped": "連続増配年数の上限表示", "streakNonDecreaseCapped": "連続非減配年数の上限表示",
    "streakUnreliable": "分割の基準ずれによる年数の算出不可",
}


def field_name(value):
    # 任意のJSONキーをそのまま貼らない。配当系列の値・自由文は一切読まない。
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,79}", value) else "項目名不明"


def summarize(report):
    """annual等のbefore/after全体を文字列化しないことが公開境界。"""
    changes = report["changes"]
    fields = []
    for origin, document in [("画面", report), ("一覧", report["stock_columns"])]:
        for key, count in sorted(document["changed_counts"].items()):
            if type(count) is int and count > 0:
                key = field_name(key)
                fields.append([origin, FIELD_NAMES.get(key, "項目の変更"), key, count])
    annual = [[code(row.get("code"))] for row in changes.get("annual", [])]
    streaks = []
    for key, rows in sorted(changes.items()):
        if not key.startswith("streak"):
            continue
        for row in rows:
            before, after = row.get("before"), row.get("after")
            if type(before) in (int, float) and type(after) in (int, float) and after < before:
                streaks.append([code(row.get("code")), field_name(key), number(before), number(after)])
            elif type(before) in (int, float) and after is None:
                streaks.append([code(row.get("code")), field_name(key), number(before), "算出不可（要確認）"])
    guards = []
    for row in changes.get("dividendYieldBasis", []):
        left, right = row.get("before") or {}, row.get("after") or {}
        before = isinstance(left, dict) and left.get("source") == "daily_csv_split_guard"
        after = isinstance(right, dict) and right.get("source") == "daily_csv_split_guard"
        if before != after:
            guards.append([code(row.get("code")), "付く" if after else "外れる"])
    # 入力名を自由文として公開せず、既知の不足だけを説明する。
    missing = report.get("missing_inputs", [])
    checks = [["配当予想の入力", "未確認" if "forecasts" in missing else "取得済み"],
              ["株価の時点を示す補助ファイル", "未確認（通常の終値扱い）" if "price_session_meta" in missing else "取得済み"],
              ["そのほかの入力不足", "未確認" if set(missing) - {"forecasts", "price_session_meta"} else "なし"]]
    for label, coverage in sorted(report.get("coverage", {}).items()):
        for key, text in [("forecast_records", "配当予想"), ("fiscal_records", "事業年度の系列"), ("calendar_records", "暦年の凍結系列")]:
            checks.append([f"{'main' if label == 'before' else 'PR'}の{text}", f"{number(coverage.get(key))}銘柄（0件なら未確認）"])
    for key, text in [("mismatch_count", "グラフ最新値と利回り分子の不一致"), ("guard_mismatch_count", "安全装置の検査不一致")]:
        checks.append([text, f"{number(report.get('audit_counts', {}).get(key))}銘柄（1件以上なら取り込む前に要確認）"])
    intro = ("## 画面データの全項目比較\n\n"
             "同じ入力・同じ日付・mainの計算プログラムで、mainとPRの台帳だけを入れ替えました。"
             "配当系列の値そのものは掲載しません。変更が0銘柄の項目は表から省いています。\n\n"
             + REVIEW_GUIDE + "\n")
    sections = [("比較した銘柄数", ["対象", "銘柄数"],
                 [["main", number(report["before_count"])], ["PR", number(report["after_count"])],
                  ["追加", len(report["added_codes"])], ["削除", len(report["removed_codes"])]]),
                ("変わった項目と銘柄数", ["表示場所", "意味", "項目名", "変わる銘柄数"], fields),
                ("annualが変わる銘柄", ["銘柄コード"], annual),
                ("streakの年数が減る銘柄", ["銘柄コード", "項目名", "mainの年数", "PRの年数"], streaks),
                ("安全装置が外れる・付く銘柄", ["銘柄コード", "PRを取り込んだ場合"], guards),
                ("未確認の入力・検査", ["点検項目", "結果"], checks)]
    return split_tables(intro, sections)
