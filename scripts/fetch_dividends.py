"""
tickers.json の全銘柄について、yfinanceで配当履歴・現在株価・配当性向を取得し、
年別配当合計・連続増配年数・増配率などを計算してdividends.jsonを生成する。

再実行しても既に取得済みの銘柄はスキップする(レジューム可能)。
Yahoo Finance側への配慮のため、銘柄ごとに待機時間を挟む。

実行例:
  python3 fetch_dividends.py                # 全銘柄
  python3 fetch_dividends.py --limit 50      # 動作確認用に先頭50銘柄のみ
  python3 fetch_dividends.py --reset         # 取得済みキャッシュを無視して再取得
"""
import argparse
import json
import time
from pathlib import Path

import yfinance as yf

from common import annual_totals, compute_stats, eps_by_year, payout_ratio_by_year

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
TICKERS_PATH = DATA_DIR / "tickers.json"
OUT_PATH = DATA_DIR / "dividends.json"

SLEEP_SECONDS = 0.6  # Yahoo Financeへの配慮(1銘柄あたりの待機)


def load_existing():
    if OUT_PATH.exists():
        with open(OUT_PATH, encoding="utf-8") as f:
            records = json.load(f)
        return {r["code"]: r for r in records}
    return {}


def fetch_one(code):
    ticker = yf.Ticker(f"{code}.T")
    dividends = ticker.dividends
    totals = annual_totals(dividends)
    stats = compute_stats(totals)
    payout_ratio = payout_ratio_by_year(totals, eps_by_year(ticker))

    price = None
    try:
        price = ticker.fast_info.get("lastPrice")
    except Exception:
        pass

    latest_year_div = totals[max(totals.keys())] if totals else None
    dividend_yield = (
        round(latest_year_div / price * 100, 2)
        if latest_year_div and price
        else None
    )

    return {
        "annual": totals,
        "payoutRatio": payout_ratio,
        "price": price,
        "dividendYield": dividend_yield,
        **stats,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--reset", action="store_true")
    parser.add_argument("--codes", type=str, default=None, help="カンマ区切りで銘柄コードを指定")
    args = parser.parse_args()

    with open(TICKERS_PATH, encoding="utf-8") as f:
        tickers = json.load(f)

    if args.codes:
        wanted = set(args.codes.split(","))
        tickers = [t for t in tickers if t["code"] in wanted]

    if args.limit:
        tickers = tickers[: args.limit]

    existing = {} if args.reset else load_existing()
    results = dict(existing)

    total = len(tickers)
    for i, t in enumerate(tickers, 1):
        code = t["code"]
        if code in existing:
            continue
        try:
            div_data = fetch_one(code)
            results[code] = {
                "code": code,
                "name": t["name"],
                "market": t["market"],
                "sector": t.get("sector"),
                **div_data,
            }
            print(f"[{i}/{total}] {code} {t['name']} OK ({len(div_data['annual'])}年分)")
        except Exception as e:
            print(f"[{i}/{total}] {code} {t['name']} ERROR: {e}")
        time.sleep(SLEEP_SECONDS)

        # 100件ごとに中間保存(途中終了しても再開できるように)
        if i % 100 == 0:
            save(results)

    save(results)


def save(results):
    records = list(results.values())
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False)
    print(f"--- 保存: {len(records)}銘柄 -> {OUT_PATH}")


if __name__ == "__main__":
    main()
