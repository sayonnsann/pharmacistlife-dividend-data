"""
JPXの東証上場銘柄一覧(data_j.xls)をダウンロードし、
配当チェッカー対象となる内国普通株式のみを抽出してtickers.jsonを生成する。

sector には証券コード協議会の33業種区分を入れる(ページの業種フィルタ用)。
17業種区分(集約版)も sector17 として保持し、将来切り替えたくなった時に使えるようにする。

実行: python3 build_master.py
出力: ../data/tickers.json  [{code, name, market, sector, sector17}, ...]
"""
import json
import subprocess
from pathlib import Path

import pandas as pd

JPX_URL = "https://www.jpx.co.jp/markets/statistics-equities/misc/tvdivq0000001vg2-att/data_j.xls"
OUT_DIR = Path(__file__).resolve().parent.parent / "data"
XLS_PATH = OUT_DIR / "data_j.xls"

# 対象とする市場区分(内国普通株式のみ。ETF/REIT/PRO Market等は除外)
TARGET_MARKETS = {
    "プライム（内国株式）",
    "スタンダード（内国株式）",
    "グロース（内国株式）",
}


def download_master():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["curl", "-sL", "-o", str(XLS_PATH), JPX_URL],
        check=True,
    )


def build_tickers_json():
    df = pd.read_excel(XLS_PATH)
    df = df[df["市場・商品区分"].isin(TARGET_MARKETS)]

    records = []
    for _, row in df.iterrows():
        code = str(row["コード"]).strip()
        records.append(
            {
                "code": code,
                "name": str(row["銘柄名"]).strip(),
                "market": str(row["市場・商品区分"]).strip(),
                "sector": str(row["33業種区分"]).strip(),
                "sector17": str(row["17業種区分"]).strip(),
            }
        )

    out_path = OUT_DIR / "tickers.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False)

    print(f"銘柄マスタ件数: {len(records)}")
    print(f"出力: {out_path}")


if __name__ == "__main__":
    download_master()
    build_tickers_json()
