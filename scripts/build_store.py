#!/usr/bin/env python3
"""配当チェッカー用の非公開SQLiteストアを実データから構築する。"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import sqlite3
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FINANCIALS = REPOSITORY_ROOT / "data" / "all_financials.json"
DEFAULT_SECTORS = REPOSITORY_ROOT / "data" / "sector_stats.json"
DEFAULT_DIVIDENDS = REPOSITORY_ROOT / "data" / "dividends.json"
DEFAULT_FORECASTS = REPOSITORY_ROOT / "forecasts_state.json"
DEFAULT_OUTPUT = REPOSITORY_ROOT / "stocks.sqlite"
DAILY_PRICE_CSV_URL = (
    "https://cdn.jsdelivr.net/gh/sayonnsann/"
    "kouhaitou-db@main/data/database.csv"
)
JST = ZoneInfo("Asia/Tokyo")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--financials", type=Path, default=DEFAULT_FINANCIALS)
    parser.add_argument("--sectors", type=Path, default=DEFAULT_SECTORS)
    parser.add_argument("--dividends", type=Path, default=DEFAULT_DIVIDENDS)
    parser.add_argument("--forecasts", type=Path, default=DEFAULT_FORECASTS)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--prices-url",
        default=DAILY_PRICE_CSV_URL,
        help="日次株価CSV URL（file://を含むcurl対応URLも可）",
    )
    return parser.parse_args()


def load_json(path: Path, expected_type: type) -> Any:
    with path.open("r", encoding="utf-8") as source:
        value = json.load(source)
    if not isinstance(value, expected_type):
        raise ValueError(
            f"{path}: JSONの最上位が{expected_type.__name__}ではありません"
        )
    return value


def load_forecasts(path: Path) -> dict[str, Any]:
    if not path.exists():
        print(f"予想stateなし（全銘柄NULLで構築）: {path}")
        return {}
    state = load_json(path, dict)
    stocks = state.get("stocks")
    if not isinstance(stocks, dict):
        raise ValueError(f"{path}: stocksがobjectではありません")
    return stocks


def normalized_code(value: Any) -> str:
    code = str(value).strip().upper()
    if len(code) != 4 or not code.isalnum() or not code.isascii():
        raise ValueError(f"不正な銘柄コードです: {value!r}")
    return code


def index_by_code(
    records: list[dict[str, Any]], label: str, *, skip_nonstandard: bool = False
) -> tuple[dict[str, dict[str, Any]], int]:
    result: dict[str, dict[str, Any]] = {}
    skipped = 0
    for record in records:
        if not isinstance(record, dict):
            raise ValueError(f"{label}: レコードがobjectではありません")
        try:
            code = normalized_code(record.get("code", ""))
        except ValueError:
            if skip_nonstandard:
                skipped += 1
                continue
            raise
        if code in result:
            raise ValueError(f"{label}: 銘柄コード {code} が重複しています")
        result[code] = record
    return result, skipped


def finite_number(value: Any) -> float | int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value != value or value in (float("inf"), float("-inf")):
        return None
    return value


def bounded(value: Any, low: float, high: float) -> float | int | None:
    """妥当な範囲外の値を、ランキングを壊さないようNULLにする。"""
    number = finite_number(value)
    if number is None or not low <= number <= high:
        return None
    return number


def latest_number(series: Any) -> float | int | None:
    if not isinstance(series, dict):
        return None
    candidates: list[tuple[int, float | int]] = []
    for year, raw_value in series.items():
        try:
            numeric_year = int(year)
        except (TypeError, ValueError):
            continue
        value = finite_number(raw_value)
        if value is not None:
            candidates.append((numeric_year, value))
    return max(candidates, default=(0, None), key=lambda item: item[0])[1]


def payout_value(
    financial: dict[str, Any], dividend: dict[str, Any]
) -> float | int | None:
    for key in ("payoutRatioTotalBased", "payoutRatioConsolidated"):
        value = latest_number(financial.get(key))
        if value is not None:
            return value
    return latest_number(dividend.get("payoutRatio"))


def json_text(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )


def load_daily_prices(url: str) -> tuple[dict[str, float], str]:
    """kouhaitou-dbの19列CSVを取得し、コード別の前日終値を返す。"""
    try:
        completed = subprocess.run(
            [
                "curl",
                "--fail",
                "--silent",
                "--show-error",
                "--location",
                "--max-time",
                "60",
                url,
            ],
            capture_output=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        detail = ""
        if isinstance(error, subprocess.CalledProcessError):
            detail = error.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"日次株価の取得に失敗しました: {detail or error}") from error

    try:
        raw = completed.stdout.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise ValueError("日次株価CSVがUTF-8ではありません") from error
    prices: dict[str, float] = {}
    updated = ""
    for index, row in enumerate(csv.reader(io.StringIO(raw))):
        if len(row) != 19:
            continue
        if index == 0:
            updated = row[18].strip()
            continue
        code = row[0].strip().upper()
        try:
            price = float(row[18])
        except ValueError:
            continue
        if len(code) == 4 and code.isascii() and code.isalnum() and price > 0:
            prices[code] = price
    if not prices:
        raise ValueError("日次株価CSVから有効な株価を1件も取得できませんでした")
    print(f"日次株価: {len(prices):,}銘柄（更新 {updated or '不明'}）")
    return prices, updated


def forecast_values(
    record: Any, price: float | int | None
) -> tuple[float | int | None, float | None, str | None, str | None]:
    if not isinstance(record, dict):
        return None, None, None, None
    dividend = bounded(record.get("forecastDividend"), 0, 1_000_000)
    forecast_yield = None
    if dividend is not None and price is not None and price > 0:
        forecast_yield = bounded(
            round(float(dividend) / float(price) * 100, 2), 0, 30
        )
    period = record.get("forecastPeriod")
    fetched_at = record.get("lastFetchedAt")
    return (
        dividend,
        forecast_yield,
        str(period)[:100] if period is not None else None,
        str(fetched_at)[:40] if fetched_at is not None else None,
    )


def create_database(
    database_path: Path,
    financials: list[dict[str, Any]],
    sectors: dict[str, Any],
    dividends: list[dict[str, Any]],
    forecasts: dict[str, Any],
    source_paths: list[Path],
    prices_url: str,
) -> tuple[int, int, int]:
    financial_by_code, skipped_financials = index_by_code(
        financials, "all_financials"
    )
    if skipped_financials:
        raise ValueError("all_financialsに4桁英数でないコードがあります")
    dividend_by_code, skipped_dividends = index_by_code(
        dividends, "dividends", skip_nonstandard=True
    )
    daily_prices, prices_updated = load_daily_prices(prices_url)

    database_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".stocks-", suffix=".sqlite", dir=database_path.parent
    )
    os.close(descriptor)
    temporary_path = Path(temporary_name)

    try:
        connection = sqlite3.connect(temporary_path)
        try:
            connection.executescript(
                """
                PRAGMA journal_mode=DELETE;
                PRAGMA synchronous=FULL;
                CREATE TABLE stocks (
                    code TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    industry TEXT,
                    yield REAL,
                    forecast_yield REAL,
                    streak INTEGER,
                    streak_nd INTEGER,
                    cagr3 REAL,
                    roe REAL,
                    equity_ratio REAL,
                    payout REAL,
                    price REAL,
                    payload TEXT NOT NULL
                );
                CREATE TABLE sectors (
                    payload TEXT NOT NULL
                );
                CREATE TABLE meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE hits (
                    ip TEXT NOT NULL,
                    date TEXT NOT NULL,
                    count INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (ip, date)
                );
                CREATE INDEX stocks_name_idx ON stocks(name);
                CREATE INDEX stocks_industry_idx ON stocks(industry);
                CREATE INDEX stocks_yield_idx ON stocks(yield);
                CREATE INDEX stocks_forecast_yield_idx
                    ON stocks(forecast_yield);
                CREATE INDEX stocks_screen_idx
                    ON stocks(industry, roe, equity_ratio, payout);
                """
            )

            rows = []
            matched_dividends = 0
            for code, financial in financial_by_code.items():
                dividend = dividend_by_code.get(code, {})
                if dividend:
                    matched_dividends += 1

                daily_price = daily_prices.get(code)
                effective_price = daily_price or bounded(
                    dividend.get("price"), 0.1, 10_000_000
                )
                daily_yield = None
                annual = dividend.get("annual") or {}
                if daily_price and isinstance(annual, dict) and annual:
                    latest_dividend = latest_number(annual)
                    if latest_dividend is not None and latest_dividend > 0:
                        daily_yield = round(
                            float(latest_dividend) / daily_price * 100, 2
                        )
                (
                    forecast_dividend,
                    forecast_yield,
                    forecast_period,
                    forecast_fetched_at,
                ) = forecast_values(forecasts.get(code), effective_price)

                payload = dict(financial)
                payload.update(dividend)
                payload["code"] = code
                payload["industry"] = financial.get("industry")
                if daily_price:
                    payload["price"] = daily_price
                if daily_yield is not None:
                    payload["dividendYield"] = daily_yield
                payload["forecastDividend"] = forecast_dividend
                payload["forecastYield"] = forecast_yield
                payload["forecastPeriod"] = forecast_period
                payload["forecastFetchedAt"] = forecast_fetched_at

                name = str(
                    dividend.get("name") or financial.get("name") or code
                ).strip()
                rows.append(
                    (
                        code,
                        name,
                        financial.get("industry"),
                        bounded(
                            daily_yield
                            if daily_yield is not None
                            else dividend.get("dividendYield"),
                            0,
                            30,
                        ),
                        forecast_yield,
                        finite_number(dividend.get("streakIncrease")),
                        finite_number(dividend.get("streakNonDecrease")),
                        finite_number(dividend.get("cagr3")),
                        latest_number(financial.get("roe")),
                        latest_number(financial.get("equityRatio")),
                        bounded(payout_value(financial, dividend), 0, 1000),
                        bounded(effective_price, 0.1, 10_000_000),
                        json_text(payload),
                    )
                )

            connection.executemany(
                """
                INSERT INTO stocks (
                    code, name, industry, yield, forecast_yield, streak,
                    streak_nd, cagr3, roe, equity_ratio, payout, price, payload
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            connection.execute(
                "INSERT INTO sectors(payload) VALUES (?)", (json_text(sectors),)
            )
            updated = datetime.now(JST).replace(microsecond=0).isoformat()
            metadata = {
                "updated": updated,
                "stock_count": str(len(rows)),
                "dividend_match_count": str(matched_dividends),
                "dividend_skipped_nonstandard_count": str(skipped_dividends),
                "financials_source": str(source_paths[0]),
                "sectors_source": str(source_paths[1]),
                "dividends_source": str(source_paths[2]),
                "forecasts_source": str(source_paths[3]),
                "daily_prices_source": prices_url,
                "daily_prices_updated": prices_updated,
            }
            connection.executemany(
                "INSERT INTO meta(key, value) VALUES (?, ?)", metadata.items()
            )
            connection.execute("ANALYZE")
            connection.commit()
        finally:
            connection.close()

        os.chmod(temporary_path, 0o664)
        os.replace(temporary_path, database_path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise

    return len(rows), matched_dividends, skipped_dividends


def main() -> None:
    args = parse_args()
    financials = load_json(args.financials, list)
    sectors = load_json(args.sectors, dict)
    dividends = load_json(args.dividends, list)
    forecasts = load_forecasts(args.forecasts)
    count, matched, skipped = create_database(
        args.output,
        financials,
        sectors,
        dividends,
        forecasts,
        [
            args.financials,
            args.sectors,
            args.dividends,
            args.forecasts,
        ],
        args.prices_url,
    )
    size = args.output.stat().st_size
    print(f"生成完了: {args.output}")
    print(f"銘柄数: {count:,}（配当データ突合: {matched:,}）")
    print(f"4桁英数でない配当側コードの除外: {skipped:,}")
    print(f"業種数: {len(sectors):,}")
    print(f"ファイルサイズ: {size:,} bytes ({size / 1024 / 1024:.2f} MiB)")


if __name__ == "__main__":
    main()
