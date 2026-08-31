#!/usr/bin/env python3
"""forecasts_state.json の取得状況を、値を表示せずに監査する。"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any


CODE_PATTERN = re.compile(r"^[0-9A-Z]{4}$")
TIMESTAMP_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}"
    r"(?:T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?)?$"
)
FAILURE_KIND_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument(
        "--codes",
        default="",
        help="監査する4桁英数字コード（カンマ区切り。空なら全体サマリのみ）",
    )
    return parser.parse_args()


def parse_codes(raw_codes: str) -> list[str]:
    """入力順を保ったままコードを正規化し、不正な要素を除外する。"""
    if not raw_codes.strip():
        return []

    codes: list[str] = []
    for raw_code in raw_codes.split(","):
        code = raw_code.strip().upper()
        if not CODE_PATTERN.fullmatch(code):
            # reprで改行等をログ制御文字として解釈させない。
            print(
                f"::warning::無効な銘柄コードをスキップします: {raw_code.strip()!r}",
                file=sys.stderr,
            )
            continue
        codes.append(code)
    return codes


def load_stocks(path: Path) -> dict[str, Any]:
    """stateのstocksだけを取り出す。欠損・未知の構造は空として扱う。"""
    try:
        with path.open("r", encoding="utf-8") as source:
            document = json.load(source)
    except (OSError, json.JSONDecodeError) as error:
        raise SystemExit(f"stateの読み込みに失敗しました: {path}: {error}") from error

    if not isinstance(document, dict):
        return {}
    stocks = document.get("stocks")
    return stocks if isinstance(stocks, dict) else {}


def record_dict(record: Any) -> dict[str, Any] | None:
    return record if isinstance(record, dict) else None


def has_forecast(record: Any) -> bool:
    """fetch_forecasts.pyの成功レコードにforecastDividendがあるかを判定する。"""
    value = record_dict(record)
    # Noneだけを「予想なし」とする。0はスキーマ上の数値なので予想あり。
    return value is not None and value.get("forecastDividend") is not None


def read_timestamp(record: Any) -> str:
    value = record_dict(record)
    raw = value.get("lastFetchedAt") if value is not None else None
    if not isinstance(raw, str):
        return "-"
    timestamp = raw.strip()
    return timestamp if TIMESTAMP_PATTERN.fullmatch(timestamp) else "-"


def read_failure_count(record: Any) -> int:
    value = record_dict(record)
    raw = value.get("failureCount") if value is not None else None
    if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0:
        return raw
    # 成功レコードにはfailureCountが存在しないため、監査上は0件として表示する。
    return 0


def read_failure_kind(record: Any) -> str:
    value = record_dict(record)
    raw = value.get("lastFailureKind") if value is not None else None
    if not isinstance(raw, str):
        return "-"
    kind = raw.strip()
    return kind if FAILURE_KIND_PATTERN.fullmatch(kind) else "-"


def has_failure_record(record: Any) -> bool:
    """record_failure()が残す失敗の印を持つレコードかを判定する。"""
    value = record_dict(record)
    if value is None:
        return False
    if read_failure_count(value) > 0:
        return True
    kind = value.get("lastFailureKind")
    if isinstance(kind, str) and kind.strip():
        return True
    failed_at = value.get("lastFailedAt")
    return isinstance(failed_at, str) and bool(failed_at.strip())


def print_code_table(codes: list[str], stocks: dict[str, Any]) -> None:
    print("code state forecast lastFetchedAt failureCount lastFailureKind")
    for code in codes:
        if code not in stocks:
            print(f"{code} not_in_state false - 0 -")
            continue

        record = stocks.get(code)
        forecast = "true" if has_forecast(record) else "false"
        print(
            f"{code} in_state {forecast} {read_timestamp(record)} "
            f"{read_failure_count(record)} {read_failure_kind(record)}"
        )


def print_summary(stocks: dict[str, Any]) -> None:
    stock_count = len(stocks)
    forecast_count = sum(1 for record in stocks.values() if has_forecast(record))
    failure_count = sum(
        1 for record in stocks.values() if has_failure_record(record)
    )
    print(
        "summary "
        f"stocks={stock_count} "
        f"forecast_present={forecast_count} "
        f"forecast_missing={stock_count - forecast_count} "
        f"failure_records={failure_count}"
    )


def main() -> None:
    args = parse_args()
    stocks = load_stocks(args.state)
    codes_were_supplied = bool(args.codes.strip())
    codes = parse_codes(args.codes)

    if codes_were_supplied:
        print_code_table(codes, stocks)
    print_summary(stocks)


if __name__ == "__main__":
    main()
