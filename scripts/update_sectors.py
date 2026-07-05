"""
tickers.json の最新の業種区分(33業種)を dividends.json の各銘柄に反映する。
build_master.py で業種分類を変更した後に実行して、既存の配当データを作り直さずに
業種欄だけを更新する用途。

実行: python3 update_sectors.py
"""
import json
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
TICKERS_PATH = DATA_DIR / "tickers.json"
DIVIDENDS_PATH = DATA_DIR / "dividends.json"


def main():
    tickers = json.load(open(TICKERS_PATH, encoding="utf-8"))
    sector_by_code = {t["code"]: t.get("sector") for t in tickers}
    sector17_by_code = {t["code"]: t.get("sector17") for t in tickers}

    dividends = json.load(open(DIVIDENDS_PATH, encoding="utf-8"))

    updated = 0
    missing = 0
    for r in dividends:
        code = r["code"]
        if code in sector_by_code:
            r["sector"] = sector_by_code[code]
            r["sector17"] = sector17_by_code.get(code)
            updated += 1
        else:
            missing += 1

    with open(DIVIDENDS_PATH, "w", encoding="utf-8") as f:
        json.dump(dividends, f, ensure_ascii=False)

    print(f"業種更新: {updated}銘柄")
    if missing:
        print(f"マスタに無くスキップ: {missing}銘柄")


if __name__ == "__main__":
    main()
