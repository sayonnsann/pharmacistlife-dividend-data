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
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FINANCIALS = REPOSITORY_ROOT / "data" / "all_financials.json"
DEFAULT_SECTORS = REPOSITORY_ROOT / "data" / "sector_stats.json"
DEFAULT_DIVIDENDS = REPOSITORY_ROOT / "data" / "dividends.json"
DEFAULT_STOCK_ACTIONS = REPOSITORY_ROOT / "data" / "stock_actions_manual.json"
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
    parser.add_argument(
        "--stock-actions", type=Path, default=DEFAULT_STOCK_ACTIONS
    )
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


def load_stock_actions(
    path: Path, *, as_of: date | None = None
) -> dict[str, list[dict[str, Any]]]:
    """有効日を迎えた、確認済みの株式分割を銘柄コード別に返す。"""
    document = load_json(path, dict)
    events = document.get("events")
    if not isinstance(events, list):
        raise ValueError(f"{path}: eventsがarrayではありません")

    effective_as_of = as_of or datetime.now(JST).date()
    result: dict[str, list[dict[str, Any]]] = {}
    event_ids: set[str] = set()
    for index, event in enumerate(events):
        label = f"{path}: events[{index}]"
        if not isinstance(event, dict):
            raise ValueError(f"{label}がobjectではありません")

        event_id = event.get("eventId")
        if not isinstance(event_id, str) or not event_id.strip():
            raise ValueError(f"{label}.eventIdが空です")
        if event_id in event_ids:
            raise ValueError(f"{path}: eventId {event_id!r} が重複しています")
        event_ids.add(event_id)

        if event.get("action") != "split":
            raise ValueError(f"{label}.actionがsplitではありません")
        if event.get("status") != "confirmed":
            continue

        code = normalized_code(event.get("securityCode", ""))
        try:
            effective_date = date.fromisoformat(str(event.get("effectiveDate", "")))
        except ValueError as error:
            raise ValueError(f"{label}.effectiveDateがISO日付ではありません") from error

        old_shares = finite_number(event.get("oldShares"))
        new_shares = finite_number(event.get("newShares"))
        if old_shares is None or old_shares <= 0:
            raise ValueError(f"{label}.oldSharesが正の数ではありません")
        if new_shares is None or new_shares <= 0:
            raise ValueError(f"{label}.newSharesが正の数ではありません")
        if not isinstance(event.get("epsAdjusted"), bool):
            raise ValueError(f"{label}.epsAdjustedがbooleanではありません")

        source = event.get("source")
        if not isinstance(source, dict):
            raise ValueError(f"{label}.sourceがobjectではありません")
        source_url = source.get("url")
        if not isinstance(source_url, str) or not source_url.strip():
            raise ValueError(f"{label}.source.urlが空です")

        # 将来の分割は、現在株価がまだ旧株式数基準なので適用しない。
        if effective_date > effective_as_of:
            continue
        result.setdefault(code, []).append(event)

    for code_events in result.values():
        code_events.sort(key=lambda event: (event["effectiveDate"], event["eventId"]))
    return result


def split_adjustment(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    """複数イベントの補正係数と、画面注記用の根拠を組み立てる。"""
    if not events:
        return None

    dividend_factor = 1.0
    eps_bps_factor = 1.0
    payload_events = []
    for event in events:
        old_shares = float(event["oldShares"])
        new_shares = float(event["newShares"])
        factor = old_shares / new_shares
        dividend_factor *= factor
        if not event["epsAdjusted"]:
            eps_bps_factor *= factor
        source = event["source"]
        payload_events.append(
            {
                "eventId": event["eventId"],
                "effectiveDate": event["effectiveDate"],
                "oldShares": event["oldShares"],
                "newShares": event["newShares"],
                "adjustmentFactor": factor,
                "epsAdjusted": event["epsAdjusted"],
                "sourceUrl": source["url"],
                "sourceType": source.get("type"),
            }
        )
    return {
        "dividendFactor": dividend_factor,
        "epsBpsFactor": eps_bps_factor,
        "events": payload_events,
    }


def adjust_per_share_series(series: Any, factor: float) -> Any:
    """時系列の数値だけを補正し、欠損などの既存表現は維持する。"""
    if not isinstance(series, dict):
        return series
    return {
        period: (
            float(value) * factor
            if finite_number(value) is not None
            else value
        )
        for period, value in series.items()
    }


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


def load_daily_prices(url: str) -> tuple[dict[str, float], dict[str, float], str]:
    """kouhaitou-dbの19列CSVを取得し、前日終値と年間配当(分割調整済み)を返す。"""
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
    daily_dividends: dict[str, float] = {}
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
            try:
                annual_dividend = float(row[4])
            except (ValueError, IndexError):
                annual_dividend = 0.0
            # 年間配当が空欄/0でも「kouhaitou-dbが把握している銘柄」として記録する。
            # 0を残さないと、下流で「現在無配」と「そもそも情報がない」を
            # 区別できず、何年も前にやめた配当で利回りを出してしまう。
            daily_dividends[code] = annual_dividend if annual_dividend > 0 else 0.0
    if not prices:
        raise ValueError("日次株価CSVから有効な株価を1件も取得できませんでした")
    print(f"日次株価: {len(prices):,}銘柄（更新 {updated or '不明'}）")
    return prices, daily_dividends, updated


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
    stock_actions_by_code: dict[str, list[dict[str, Any]]] | None = None,
    stock_actions_path: Path | None = None,
) -> tuple[int, int, int]:
    financial_by_code, skipped_financials = index_by_code(
        financials, "all_financials"
    )
    if skipped_financials:
        raise ValueError("all_financialsに4桁英数でないコードがあります")
    dividend_by_code, skipped_dividends = index_by_code(
        dividends, "dividends", skip_nonstandard=True
    )
    daily_prices, daily_dividends, prices_updated = load_daily_prices(prices_url)
    stock_actions_by_code = stock_actions_by_code or {}
    breakdown_path = REPOSITORY_ROOT / "data" / "dividend_breakdown.json"
    dividend_breakdown: dict = {}
    if breakdown_path.exists():
        with breakdown_path.open(encoding="utf-8") as handle:
            dividend_breakdown = json.load(handle)
        print(f"配当内訳(記念・特別): {len(dividend_breakdown)}銘柄")

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
            adjusted_stocks = 0
            applied_events = 0
            for code, financial in financial_by_code.items():
                dividend = dividend_by_code.get(code, {})
                if dividend:
                    matched_dividends += 1

                daily_price = daily_prices.get(code)
                adjustment = split_adjustment(stock_actions_by_code.get(code, []))
                if adjustment is not None:
                    adjusted_stocks += 1
                    applied_events += len(adjustment["events"])
                effective_price = daily_price or bounded(
                    dividend.get("price"), 0.1, 10_000_000
                )
                daily_yield = None
                # 分子はkouhaitou-dbの年間配当を最優先で使い、既知の分割は
                # stock_actions_manual.jsonの係数でこの後に現在株価基準へ揃える。
                numerator = daily_dividends.get(code)
                # kouhaitou-dbが把握していて年間配当0＝現在無配。
                # この場合は過去の配当履歴に遡らない（東電の2010年60円のような
                # 十数年前の金額で利回りを出してしまうため）。
                currently_unpaid = numerator is not None and float(numerator) <= 0
                if numerator is None:
                    annual = dividend.get("annual") or {}
                    if isinstance(annual, dict) and annual:
                        numerator = latest_number(annual)
                if adjustment is not None and finite_number(numerator) is not None:
                    numerator = (
                        float(numerator) * adjustment["dividendFactor"]
                    )
                if daily_price and numerator is not None and float(numerator) > 0:
                    daily_yield = round(float(numerator) / daily_price * 100, 2)
                (
                    forecast_dividend,
                    forecast_yield,
                    forecast_period,
                    forecast_fetched_at,
                ) = forecast_values(forecasts.get(code), effective_price)

                payload = dict(financial)
                payload.update(dividend)
                if adjustment is not None:
                    dividend_factor = adjustment["dividendFactor"]
                    eps_bps_factor = adjustment["epsBpsFactor"]
                    if "dividendPerShare" in financial:
                        payload["dividendPerShare"] = adjust_per_share_series(
                            financial["dividendPerShare"], dividend_factor
                        )
                    # PER/PBR自体は価格と1株当たり値の比なので分割で不変。
                    # 現在価格との計算に使うEPS/BPSだけを現在基準へ揃える。
                    if eps_bps_factor != 1.0:
                        for key in ("eps", "bps"):
                            if key in financial:
                                payload[key] = adjust_per_share_series(
                                    financial[key], eps_bps_factor
                                )
                    payload["splitAdjustment"] = adjustment
                payload["code"] = code
                payload["industry"] = financial.get("industry")
                breakdown_entry = dividend_breakdown.get(code)
                if breakdown_entry:
                    payload["dividendBreakdown"] = breakdown_entry
                if daily_price:
                    payload["price"] = daily_price
                if daily_yield is not None:
                    payload["dividendYield"] = daily_yield
                elif currently_unpaid:
                    # dividends.json由来の古い利回りが銘柄詳細に残らないようにする
                    payload["dividendYield"] = None
                payload["forecastDividend"] = forecast_dividend
                payload["forecastYield"] = forecast_yield
                payload["forecastPeriod"] = forecast_period
                payload["forecastFetchedAt"] = forecast_fetched_at
                # 会社発表の確定年度配当（edinetdb由来）。集計中のYahoo値の裏付けに使う
                forecast_record = forecasts.get(code) or {}
                confirmed = bounded(forecast_record.get("confirmedDividend"), 0, 1_000_000)
                if confirmed is not None:
                    payload["confirmedDividend"] = confirmed
                    payload["confirmedFiscalYearEnd"] = forecast_record.get("confirmedFiscalYearEnd")

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
                            else (
                                None
                                if currently_unpaid
                                else dividend.get("dividendYield")
                            ),
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
                "stock_actions_source": str(stock_actions_path or ""),
                "split_adjusted_stock_count": str(adjusted_stocks),
                "split_adjustment_event_count": str(applied_events),
                "daily_prices_source": prices_url,
                "daily_prices_updated": prices_updated,
            }
            connection.executemany(
                "INSERT INTO meta(key, value) VALUES (?, ?)", metadata.items()
            )
            connection.execute("ANALYZE")
            connection.commit()
            print(
                f"株式分割補正: {adjusted_stocks:,}銘柄"
                f"（{applied_events:,}イベント）"
            )
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
    stock_actions = load_stock_actions(args.stock_actions)
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
        stock_actions,
        args.stock_actions,
    )
    size = args.output.stat().st_size
    print(f"生成完了: {args.output}")
    print(f"銘柄数: {count:,}（配当データ突合: {matched:,}）")
    print(f"4桁英数でない配当側コードの除外: {skipped:,}")
    print(f"業種数: {len(sectors):,}")
    print(f"ファイルサイズ: {size:,} bytes ({size / 1024 / 1024:.2f} MiB)")


if __name__ == "__main__":
    main()
