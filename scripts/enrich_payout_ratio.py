"""
既存のdividends.json(配当履歴は取得済み)に対して、配当性向(payoutRatio)だけを
追加で取得・付与するバックフィル用スクリプト。配当履歴の再取得(重い処理)は行わない。

fetch_dividends.py が新しく取得する銘柄には最初からpayoutRatioが含まれるため、
このスクリプトは主に「配当性向カラムを後から追加した」ような移行時に使う。

実行例:
  python3 enrich_payout_ratio.py            # payoutRatio未設定の銘柄のみ処理
  python3 enrich_payout_ratio.py --reset     # 全銘柄を再取得
"""
import argparse
import json
import time
from pathlib import Path

import yfinance as yf

from common import eps_by_year, payout_ratio_by_year

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
OUT_PATH = DATA_DIR / "dividends.json"

SLEEP_SECONDS = 0.5


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--reset", action="store_true")
    args = parser.parse_args()

    with open(OUT_PATH, encoding="utf-8") as f:
        records = json.load(f)

    targets = [
        r for r in records if args.reset or "payoutRatio" not in r
    ]
    total = len(targets)
    print(f"対象: {total}銘柄")

    for i, r in enumerate(targets, 1):
        code = r["code"]
        try:
            ticker = yf.Ticker(f"{code}.T")
            r["payoutRatio"] = payout_ratio_by_year(r.get("annual", {}), eps_by_year(ticker))
            print(f"[{i}/{total}] {code} {r['name']} OK ({len(r['payoutRatio'])}年分)")
        except Exception as e:
            r["payoutRatio"] = {}
            print(f"[{i}/{total}] {code} {r['name']} ERROR: {e}")
        time.sleep(SLEEP_SECONDS)

        if i % 100 == 0:
            save(records)

    save(records)


def save(records):
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False)
    print(f"--- 保存: {len(records)}銘柄 -> {OUT_PATH}")


if __name__ == "__main__":
    main()
