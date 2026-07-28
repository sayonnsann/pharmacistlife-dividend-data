#!/usr/bin/env python3
"""edinetdb.jp の最新配当予想を、日次上限内で待ち行列から取得する。"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DIVIDENDS = REPOSITORY_ROOT / "data" / "dividends.json"
DEFAULT_EDINET_DIR = REPOSITORY_ROOT / "edinet"
DEFAULT_STATE = REPOSITORY_ROOT / "forecasts_state.json"
EDINET_FEED_BASE_URL = (
    "https://cdn.jsdelivr.net/gh/sayonnsann/"
    "pharmacistlife-dividend-data@main/edinet"
)
EDINETDB_BASE_URL = "https://edinetdb.jp/v1/companies"
STATE_VERSION = 1
CODE_PATTERN = re.compile(r"^[0-9A-Z]{4}$")
EDINET_CODE_PATTERN = re.compile(r"^E[0-9]{5}$")


@dataclass(frozen=True)
class Event:
    announced_on: date
    kind: str
    period: str


@dataclass(frozen=True)
class Candidate:
    code: str
    fiscal_month: int
    edinet_code: str
    dividend_yield: float
    priority_rank: int | None
    event: Event | None
    last_fetched: date | None

    @property
    def is_due(self) -> bool:
        return self.event is not None and (
            self.last_fetched is None
            or self.last_fetched < self.event.announced_on
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dividends", type=Path, default=DEFAULT_DIVIDENDS)
    parser.add_argument("--edinet-dir", type=Path, default=DEFAULT_EDINET_DIR)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument(
        "--today",
        type=date.fromisoformat,
        default=date.today(),
        metavar="YYYY-MM-DD",
        help="待ち行列計算に使う日付（テスト用）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="APIを呼ばず、状態も書き換えずに待ち行列だけ表示する",
    )
    parser.add_argument(
        "--print-limit",
        type=int,
        default=20,
        help="dry-runで表示する先頭件数（既定: 20）",
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


def empty_state() -> dict[str, Any]:
    return {"version": STATE_VERSION, "queuePosition": 0, "stocks": {}}


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return empty_state()
    state = load_json(path, dict)
    if state.get("version") != STATE_VERSION:
        raise ValueError(f"{path}: 未対応のstate versionです")
    if not isinstance(state.get("stocks"), dict):
        raise ValueError(f"{path}: stocksがobjectではありません")
    position = state.get("queuePosition", 0)
    if isinstance(position, bool) or not isinstance(position, int) or position < 0:
        raise ValueError(f"{path}: queuePositionが不正です")
    return state


def write_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("w", encoding="utf-8") as destination:
        json.dump(
            state,
            destination,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
        destination.write("\n")
        destination.flush()
        os.fsync(destination.fileno())
    os.replace(temporary, path)


def normalize_code(value: Any) -> str:
    code = str(value or "").strip().upper()
    return code if CODE_PATTERN.fullmatch(code) else ""


def finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if number == number and abs(number) != float("inf") else None


def has_recent_dividend(record: dict[str, Any]) -> bool:
    annual = record.get("annual")
    if not isinstance(annual, dict):
        return False
    values: list[tuple[int, float]] = []
    for raw_year, raw_value in annual.items():
        try:
            year = int(raw_year)
        except (TypeError, ValueError):
            continue
        value = finite_number(raw_value)
        if value is not None:
            values.append((year, value))
    # dividends.jsonには上場廃止・特殊銘柄などで最新年だけ0のレコードもある。
    # 「配当履歴がある銘柄」という対象定義に合わせ、少なくとも1年の正値を条件にする。
    return any(value > 0 for _, value in values)


def read_priority_codes() -> dict[str, int]:
    result: dict[str, int] = {}
    for raw_code in os.environ.get("PRIORITY_CODES", "").split(","):
        code = normalize_code(raw_code)
        if code and code not in result:
            result[code] = len(result)
    return result


def parse_fetched_date(record: Any) -> date | None:
    if not isinstance(record, dict):
        return None
    raw = record.get("lastFetchedAt")
    if not isinstance(raw, str):
        return None
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        return None


def load_feed(
    code: str, edinet_dir: Path, *, allow_network: bool
) -> dict[str, Any] | None:
    local_path = edinet_dir / f"{code}.json"
    if local_path.exists():
        return load_json(local_path, dict)
    if not allow_network:
        return None
    url = f"{EDINET_FEED_BASE_URL}/{quote(code)}.json"
    request = Request(url, headers={"User-Agent": "dividend-store-updater/1.0"})
    try:
        with urlopen(request, timeout=30) as response:
            value = json.load(response)
    except HTTPError as error:
        if error.code == 404:
            return None
        raise RuntimeError(f"{code}: EDINET feed HTTP {error.code}") from error
    except (URLError, TimeoutError) as error:
        raise RuntimeError(f"{code}: EDINET feed取得失敗") from error
    if not isinstance(value, dict):
        raise ValueError(f"{code}: EDINET feedの最上位がobjectではありません")
    return value


def shifted_month(year: int, month: int, offset: int) -> tuple[int, int]:
    absolute = year * 12 + month - 1 + offset
    return absolute // 12, absolute % 12 + 1


def fiscal_events(fiscal_month: int, today: date) -> list[Event]:
    """今日以前の近似決算発表日を生成する。

    本決算は決算月の2か月後の15日、Q1-Q3は各四半期末から約1.5か月後
    （翌々月15日）とする。実日付ではなく、優先キュー用の近似値である。
    """
    events: list[Event] = []
    for fiscal_year in range(today.year - 2, today.year + 2):
        for quarter, month_offset in ((1, -9), (2, -6), (3, -3), (4, 0)):
            end_year, end_month = shifted_month(
                fiscal_year, fiscal_month, month_offset
            )
            announce_year, announce_month = shifted_month(
                end_year, end_month, 2
            )
            announced_on = date(announce_year, announce_month, 15)
            if announced_on <= today:
                kind = "annual" if quarter == 4 else f"q{quarter}"
                events.append(
                    Event(
                        announced_on=announced_on,
                        kind=kind,
                        period=f"FY{fiscal_year} {kind.upper()}",
                    )
                )
    return events


def latest_event(fiscal_month: int, today: date) -> Event | None:
    return max(
        fiscal_events(fiscal_month, today),
        key=lambda event: event.announced_on,
        default=None,
    )


def build_candidates(
    dividends: list[dict[str, Any]],
    state: dict[str, Any],
    edinet_dir: Path,
    today: date,
    priority_codes: dict[str, int],
    *,
    allow_feed_network: bool,
) -> tuple[list[Candidate], int]:
    candidates: list[Candidate] = []
    missing_feed = 0
    seen: set[str] = set()
    stock_state = state["stocks"]
    for record in dividends:
        if not isinstance(record, dict) or not has_recent_dividend(record):
            continue
        code = normalize_code(record.get("code"))
        if not code:
            missing_feed += 1
            continue
        if code in seen:
            continue
        seen.add(code)
        feed = load_feed(code, edinet_dir, allow_network=allow_feed_network)
        if feed is None:
            missing_feed += 1
            continue
        fiscal_month = feed.get("fiscalMonth")
        edinet_code = str(feed.get("edinetCode") or "").strip().upper()
        if (
            isinstance(fiscal_month, bool)
            or not isinstance(fiscal_month, int)
            or not 1 <= fiscal_month <= 12
            or not EDINET_CODE_PATTERN.fullmatch(edinet_code)
        ):
            missing_feed += 1
            continue
        raw_yield = finite_number(record.get("dividendYield"))
        dividend_yield = (
            raw_yield if raw_yield is not None and 0 <= raw_yield <= 30 else 0.0
        )
        candidates.append(
            Candidate(
                code=code,
                fiscal_month=fiscal_month,
                edinet_code=edinet_code,
                dividend_yield=dividend_yield,
                priority_rank=priority_codes.get(code),
                event=latest_event(fiscal_month, today),
                last_fetched=parse_fetched_date(stock_state.get(code)),
            )
        )
    return candidates, missing_feed


def due_sort_key(candidate: Candidate) -> tuple[Any, ...]:
    assert candidate.event is not None
    return (
        candidate.priority_rank is None,
        candidate.priority_rank if candidate.priority_rank is not None else 0,
        -candidate.event.announced_on.toordinal(),
        0 if candidate.event.kind == "annual" else 1,
        -candidate.dividend_yield,
        candidate.code,
    )


def normal_sort_key(candidate: Candidate) -> tuple[Any, ...]:
    return (
        candidate.priority_rank is None,
        candidate.priority_rank if candidate.priority_rank is not None else 0,
        -candidate.dividend_yield,
        candidate.code,
    )


def ordered_queue(
    candidates: list[Candidate], state: dict[str, Any]
) -> tuple[list[Candidate], int, int]:
    due = sorted((item for item in candidates if item.is_due), key=due_sort_key)
    normal = sorted(
        (item for item in candidates if not item.is_due), key=normal_sort_key
    )
    if not normal:
        return due, len(due), 0
    position = state["queuePosition"] % len(normal)
    return due + normal[position:] + normal[:position], len(due), position


def env_daily_limit() -> int:
    raw = os.environ.get("DVC_FORECAST_DAILY", "95")
    try:
        value = int(raw)
    except ValueError as error:
        raise ValueError("DVC_FORECAST_DAILYは整数で指定してください") from error
    if not 1 <= value <= 100:
        raise ValueError("DVC_FORECAST_DAILYは1〜100で指定してください")
    return value


def first_present(record: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in record and record[key] is not None:
            return record[key]
    return None


def optional_number(record: dict[str, Any], *keys: str) -> float | int | None:
    value = finite_number(first_present(record, *keys))
    if value is None or not 0 <= value <= 1_000_000:
        return None
    return int(value) if value.is_integer() else value


def forecast_period(
    record: dict[str, Any], fiscal_month: int | None = None
) -> str | None:
    explicit = first_present(
        record,
        "forecast_period",
        "forecastPeriod",
        "forecast_fiscal_period",
        "forecastFiscalPeriod",
        "target_period",
        "targetPeriod",
    )
    if explicit is not None and str(explicit).strip():
        return str(explicit).strip()[:100]

    year = first_present(record, "fiscal_year", "fiscalYear")
    quarter = first_present(record, "quarter")
    try:
        numeric_year = int(year)
    except (TypeError, ValueError):
        numeric_year = 0
    quarter_text = str(quarter or "").strip().upper().replace("Q", "")
    try:
        numeric_quarter = int(quarter_text)
    except ValueError:
        numeric_quarter = 0
    if numeric_year:
        target_year = numeric_year + 1 if numeric_quarter == 4 else numeric_year
        return f"FY{target_year}"

    # 参照GASで使われている応答にはfiscalYearがない場合があるため、
    # 開示日・四半期・会社の決算月から予想対象の通期を復元する。
    disclosure = first_present(record, "disclosure_date", "disclosureDate")
    try:
        disclosure_date = date.fromisoformat(str(disclosure)[:10])
    except ValueError:
        return None
    if fiscal_month is None or not 1 <= fiscal_month <= 12:
        return None
    if numeric_quarter not in (1, 2, 3, 4):
        return None
    quarter_end_offset = -3 * (4 - numeric_quarter)
    _, quarter_end_month = shifted_month(
        disclosure_date.year, fiscal_month, quarter_end_offset
    )
    quarter_end_year = disclosure_date.year
    if quarter_end_month > disclosure_date.month:
        quarter_end_year -= 1
    actual_fiscal_year = quarter_end_year
    if fiscal_month < quarter_end_month:
        actual_fiscal_year += 1
    target_year = (
        actual_fiscal_year + 1
        if numeric_quarter == 4
        else actual_fiscal_year
    )
    return f"FY{target_year}"


def parse_forecast_response(
    body: Any, fiscal_month: int | None = None
) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise ValueError("API応答の最上位がobjectではありません")
    earnings = body.get("earnings")
    if not isinstance(earnings, list):
        earnings = body.get("data")
    if not isinstance(earnings, list):
        raise ValueError("API応答にearnings/data配列がありません")
    latest = earnings[0] if earnings else {}
    if not isinstance(latest, dict):
        raise ValueError("API応答の最新決算がobjectではありません")

    annual = optional_number(
        latest, "forecast_dividend_per_share", "forecastDividendPerShare"
    )
    interim = optional_number(
        latest,
        "forecast_interim_dividend_per_share",
        "forecastInterimDividendPerShare",
        "interim_forecast_dividend_per_share",
        "interimForecastDividendPerShare",
    )
    final = optional_number(
        latest,
        "forecast_yearend_dividend_per_share",
        "forecastYearendDividendPerShare",
        "yearend_forecast_dividend_per_share",
        "yearendForecastDividendPerShare",
    )
    return {
        "forecastDividend": annual,
        "forecastInterimDividend": interim,
        "forecastFinalDividend": final,
        "forecastPeriod": forecast_period(latest, fiscal_month),
    }


def fetch_one(candidate: Candidate, api_key: str) -> tuple[dict[str, Any], int | None]:
    query = urlencode({"limit": 1})
    url = (
        f"{EDINETDB_BASE_URL}/{quote(candidate.edinet_code)}/earnings?{query}"
    )
    request = Request(
        url,
        headers={
            "X-API-Key": api_key,
            "Accept": "application/json",
            "User-Agent": "dividend-store-updater/1.0",
        },
    )
    try:
        with urlopen(request, timeout=45) as response:
            body = json.load(response)
            remaining_header = response.headers.get("X-RateLimit-Remaining")
    except HTTPError as error:
        raise RuntimeError(
            f"{candidate.code}: edinetdb HTTP {error.code}"
        ) from error
    except (URLError, TimeoutError) as error:
        raise RuntimeError(f"{candidate.code}: edinetdb通信失敗") from error
    except json.JSONDecodeError as error:
        raise RuntimeError(f"{candidate.code}: edinetdb応答が不正なJSONです") from error
    try:
        remaining = int(remaining_header) if remaining_header is not None else None
    except ValueError:
        remaining = None
    return parse_forecast_response(body, candidate.fiscal_month), remaining


def dry_run_output(
    queue: list[Candidate],
    due_count: int,
    normal_position: int,
    queued_count: int,
    missing_feed: int,
    today: date,
    print_limit: int,
) -> None:
    print(
        "dry-run: API呼び出しなし / 状態更新なし "
        f"(today={today.isoformat()})"
    )
    print(
        f"配当対象={queued_count + missing_feed:,} queue={queued_count:,} "
        f"due={due_count:,} queuePosition={normal_position:,} "
        f"feed/コード除外={missing_feed:,}"
    )
    print("rank code fiscalMonth due event announced priority yield lastFetched")
    for rank, candidate in enumerate(queue[: max(print_limit, 0)], start=1):
        event = candidate.event
        print(
            f"{rank:>4} {candidate.code} {candidate.fiscal_month:>11} "
            f"{str(candidate.is_due).lower():>4} "
            f"{event.kind if event else '-':>6} "
            f"{event.announced_on.isoformat() if event else '-':>10} "
            f"{candidate.priority_rank + 1 if candidate.priority_rank is not None else '-':>8} "
            f"{candidate.dividend_yield:>5.2f} "
            f"{candidate.last_fetched.isoformat() if candidate.last_fetched else '-'}"
        )


def main() -> None:
    args = parse_args()
    api_key = os.environ.get("EDINETDB_API_KEY", "").strip()
    if not args.dry_run and not api_key:
        raise SystemExit(
            "EDINETDB_API_KEYがありません。APIを呼ばない確認は--dry-runを指定してください。"
        )
    dividends = load_json(args.dividends, list)
    state = load_state(args.state)
    priority_codes = read_priority_codes()
    candidates, missing_feed = build_candidates(
        dividends,
        state,
        args.edinet_dir,
        args.today,
        priority_codes,
        allow_feed_network=not args.dry_run,
    )
    queue, due_count, normal_position = ordered_queue(candidates, state)

    if args.dry_run:
        dry_run_output(
            queue,
            due_count,
            normal_position,
            len(candidates),
            missing_feed,
            args.today,
            args.print_limit,
        )
        return

    limit = min(env_daily_limit(), len(queue))
    selected = queue[:limit]
    fetched_at = args.today.isoformat()
    normal_processed = 0
    no_forecast = 0
    last_remaining: int | None = None
    processed = 0
    for candidate in selected:
        parsed, last_remaining = fetch_one(candidate, api_key)
        parsed["lastFetchedAt"] = fetched_at
        state["stocks"][candidate.code] = parsed
        if parsed["forecastDividend"] is None:
            no_forecast += 1
        if not candidate.is_due:
            normal_processed += 1
        processed += 1
        # 既定95件なら通常は残量5を残す。サーバー側残量が想定より少ない時も
        # 最低5件を温存して停止する。
        if last_remaining is not None and last_remaining <= 5:
            break

    normal_count = len(candidates) - due_count
    if normal_count:
        state["queuePosition"] = (
            normal_position + normal_processed
        ) % normal_count
    write_state(args.state, state)
    print(
        f"予想取得完了: {processed:,}件 "
        f"（予想なし {no_forecast:,}件、対象 {len(candidates):,}件）"
    )
    if last_remaining is not None:
        print(f"edinetdb日次残量: {last_remaining:,}")
    print(f"状態保存: {args.state}")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError) as error:
        print(f"エラー: {error}", file=sys.stderr)
        raise SystemExit(1) from error
