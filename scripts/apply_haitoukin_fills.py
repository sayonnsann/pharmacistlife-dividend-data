# -*- coding: utf-8 -*-
"""Yahoo再取得後のdividends.jsonに、haitoukin-checker由来の補完(欠損年のみ)を重ねる。

補完元 haitoukin_fills.json は照合検証済みの静的ファイル(440銘柄・1,660年分)。
fetch_dividends.py --reset は毎回ゼロから作り直すため、このマージを挟まないと
補完分が毎週消える。既存年は絶対に上書きしない。

補完元も出力先も、外部サイト由来の値を含むためリポジトリには置かない。
置き場所はConoHaの非公開dataディレクトリで、週次ワークフローがFTPSで
取ってきて --fills で渡す。
"""
import argparse
import json
import os

BASE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data")
DEFAULT_DIVIDENDS = os.path.join(BASE, "dividends.json")
DEFAULT_FILLS = os.path.join(BASE, "haitoukin_fills.json")


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dividends", default=DEFAULT_DIVIDENDS)
    parser.add_argument("--fills", default=DEFAULT_FILLS)
    return parser.parse_args()


def main():
    args = parse_args()
    div_path = args.dividends
    with open(div_path, encoding="utf-8") as f:
        db = json.load(f)
    with open(args.fills, encoding="utf-8") as f:
        fills = json.load(f)
    stocks = db["stocks"] if isinstance(db, dict) and "stocks" in db else db
    added = 0
    for s in stocks:
        fill = fills.get(s.get("code"))
        if not fill:
            continue
        annual = s.setdefault("annual", {})
        for year, value in fill.items():
            if year not in annual:
                annual[year] = value
                added += 1
    with open(div_path, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, separators=(",", ":"))
    print(f"haitoukin補完を適用: {added}年分を追加")


if __name__ == "__main__":
    main()
