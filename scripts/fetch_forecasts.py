#!/usr/bin/env python3
"""edinetdb.jp の最新配当予想を、日次上限内で待ち行列から取得する。"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FISCAL_DIVIDENDS = REPOSITORY_ROOT / "data" / "fiscal_dividends.json"
DEFAULT_CALENDAR_DIVIDENDS = (
    REPOSITORY_ROOT / "data" / "calendar_dividends_frozen.json"
)
DEFAULT_EDINET_DIR = REPOSITORY_ROOT / "edinet"
DEFAULT_STATE = REPOSITORY_ROOT / "forecasts_state.json"
DAILY_PRICE_CSV_URL = (
    "https://cdn.jsdelivr.net/gh/sayonnsann/"
    "kouhaitou-db@main/data/database.csv"
)
EDINET_FEED_BASE_URL = (
    "https://cdn.jsdelivr.net/gh/sayonnsann/"
    "pharmacistlife-dividend-data@main/edinet"
)
EDINETDB_BASE_URL = "https://edinetdb.jp/v1/companies"
STATE_VERSION = 1
CODE_PATTERN = re.compile(r"^[0-9A-Z]{4}$")
EDINET_CODE_PATTERN = re.compile(r"^E[0-9]{5}$")
# 認証・権限の失敗は「その銘柄が悪い」のではなく設定の問題なので、
# 次の銘柄へ進んでも全部失敗する。ここだけは全体障害として止める。
FATAL_HTTP_STATUSES = frozenset({401, 403})
# 途中保存の間隔。全部終わってから1回だけ書くと、後半で落ちたときに
# その日に取った分がまるごと消える。
SAVE_INTERVAL = 20
# 連続でこの件数だけ失敗したら、個別銘柄の問題ではないとみなして止める。
CONSECUTIVE_FAILURE_LIMIT = 10


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
    parser.add_argument(
        "--fiscal-dividends", type=Path, default=DEFAULT_FISCAL_DIVIDENDS
    )
    parser.add_argument(
        "--calendar-dividends", type=Path, default=DEFAULT_CALENDAR_DIVIDENDS
    )
    parser.add_argument(
        "--prices-url",
        default=DAILY_PRICE_CSV_URL,
        help="利回り順の並べ替えに使う日次株価CSV（file://も可）",
    )
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


def has_positive_series(series: Any) -> bool:
    """1年でも正の配当がある系列か（＝配当履歴のある銘柄か）。"""
    if not isinstance(series, dict):
        return False
    for raw_value in series.values():
        value = finite_number(raw_value)
        if value is not None and value > 0:
            return True
    return False


def load_dividend_codes(fiscal_path: Path, calendar_path: Path) -> list[str]:
    """配当履歴のある銘柄コードを返す。

    以前は dividends.json（Yahoo由来の暦年系列）を対象定義に使っていたが、
    あちらは廃止した。事業年度の配当系列（EDINET＋haitoukin-checker）と、
    事業年度の系列を作れなかった銘柄用の暦年の凍結スナップショットを足して
    同じ対象を作る。
    """
    codes: list[str] = []
    seen: set[str] = set()

    document = load_json(fiscal_path, dict)
    for raw_code, record in document.items():
        code = normalize_code(raw_code)
        if not code or code in seen or not isinstance(record, dict):
            continue
        if has_positive_series(record.get("series")):
            seen.add(code)
            codes.append(code)

    if calendar_path.exists():
        frozen = load_json(calendar_path, dict).get("stocks")
        if isinstance(frozen, dict):
            for raw_code, record in frozen.items():
                code = normalize_code(raw_code)
                if not code or code in seen or not isinstance(record, dict):
                    continue
                if has_positive_series(record.get("annual")):
                    seen.add(code)
                    codes.append(code)

    return sorted(codes)


def load_dividend_yields(url: str) -> dict[str, float]:
    """kouhaitou-dbの日次CSVから利回りを出す（待ち行列の並べ替え用）。

    以前は dividends.json の dividendYield を使っていた。同じCSVを
    build_store.py も株価と年間配当に使っているので、出どころが1つに揃う。
    取得できなくても待ち行列は作れる（利回りは全銘柄0として扱う）ので、
    ここで止めはしない。
    """
    try:
        completed = subprocess.run(
            [
                "curl", "--fail", "--silent", "--show-error", "--location",
                "--max-time", "60", url,
            ],
            capture_output=True,
            check=True,
        )
        raw = completed.stdout.decode("utf-8-sig")
    except (OSError, subprocess.CalledProcessError, UnicodeDecodeError) as error:
        print(f"日次株価CSVを取得できませんでした（利回り順は無効）: {error}")
        return {}

    yields: dict[str, float] = {}
    for index, row in enumerate(csv.reader(io.StringIO(raw))):
        if index == 0 or len(row) != 19:
            continue
        code = normalize_code(row[0])
        if not code:
            continue
        try:
            dividend = float(row[4])
            price = float(row[18])
        except ValueError:
            continue
        if price > 0 and dividend > 0:
            value = round(dividend / price * 100, 2)
            if 0 <= value <= 30:
                yields[code] = value
    print(f"利回り: {len(yields):,}銘柄（{url}）")
    return yields


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
    codes: list[str],
    dividend_yields: dict[str, float],
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
    for code in codes:
        if not code or code in seen:
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
        dividend_yield = dividend_yields.get(code, 0.0)
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


def iso_date_text(value: Any) -> str | None:
    """YYYY-MM-DDの10文字に正規化する。日付として読めない値はNone。

    "2026-10-01T00:00:00+09:00" のような時刻つきも、"20261001" のような
    区切り無しも同じ10文字に揃える（Python 3.11以降の fromisoformat は
    どちらも読める）。
    """
    if value is None:
        return None
    text = str(value).strip()[:10]
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        return None


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

    # 実応答は fiscal_year_end="2026-03-31" 形式。開示日が期末より後なら本決算で
    # 来期予想、期中なら当期予想とみなして「YYYY年M月期(予)」を組み立てる。
    fye = first_present(record, "fiscal_year_end", "fiscalYearEnd")
    try:
        fye_date = date.fromisoformat(str(fye)[:10])
    except (TypeError, ValueError):
        fye_date = None
    if fye_date is not None:
        disclosure_raw = first_present(record, "disclosure_date", "disclosureDate")
        disclosed_after_close = False
        if disclosure_raw:
            from email.utils import parsedate_to_datetime
            try:
                disclosed = parsedate_to_datetime(str(disclosure_raw)).date()
            except (TypeError, ValueError):
                try:
                    disclosed = date.fromisoformat(str(disclosure_raw)[:10])
                except (TypeError, ValueError):
                    disclosed = None
            if disclosed is not None and disclosed > fye_date:
                disclosed_after_close = True
        target_year = fye_date.year + 1 if disclosed_after_close else fye_date.year
        return f"{target_year}年{fye_date.month}月期(予)"

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
        data = body.get("data")
        if isinstance(data, dict):
            earnings = data.get("earnings")
        elif isinstance(data, list):
            earnings = data
    if not isinstance(earnings, list):
        raise ValueError("API応答にearnings/data配列がありません")
    latest = earnings[0] if earnings else {}
    if not isinstance(latest, dict):
        raise ValueError("API応答の最新決算がobjectではありません")

    annual = optional_number(
        latest, "forecast_dividend_per_share", "forecastDividendPerShare"
    )
    # 実応答の内訳は interim_dividend_per_share / yearend_dividend_per_share。
    # 以前は forecast_ を頭に付けた名前を探していたが、その名前の項目は応答に
    # 無いので中間・期末が常にNoneになっていた。
    interim = optional_number(
        latest, "interim_dividend_per_share", "interimDividendPerShare"
    )
    final = optional_number(
        latest, "yearend_dividend_per_share", "yearendDividendPerShare"
    )
    # 分割をまたぐ予想の付帯情報。表示側（build_store.py）が株価と同じ
    # 株数基準に配当を揃えるのに使う。
    annual_adjusted = optional_number(
        latest,
        "adjusted_forecast_dividend_per_share",
        "adjustedForecastDividendPerShare",
    )
    split_factor = optional_number(
        latest,
        "forecast_split_adjustment_factor",
        "forecastSplitAdjustmentFactor",
    )
    split_effective_date = iso_date_text(
        first_present(
            latest,
            "forecast_split_effective_date",
            "forecastSplitEffectiveDate",
        )
    )
    share_basis = first_present(
        latest, "forecast_share_basis", "forecastShareBasis"
    )
    share_basis_text = (
        str(share_basis).strip()[:40]
        if share_basis is not None and str(share_basis).strip()
        else None
    )
    # 同じ応答に入っている「確定した年度実績」も保存する（追加のAPI消費なし）。
    # 権利落ちベースのYahoo集計と違い、会社発表の確定値なので表示の裏付けに使える。
    confirmed = optional_number(
        latest, "dividend_per_share", "dividendPerShare"
    )
    confirmed_adjusted = optional_number(
        latest,
        "adjusted_annual_dividend_per_share",
        "adjustedAnnualDividendPerShare",
    )
    fiscal_year_end = first_present(latest, "fiscal_year_end", "fiscalYearEnd")
    period = forecast_period(latest, fiscal_month)
    return {
        "forecastDividend": annual,
        "forecastInterimDividend": interim,
        "forecastFinalDividend": final,
        # 分割後の株数に揃えた年間予想（API側の計算値）。
        "forecastDividendAdjusted": annual_adjusted,
        "forecastSplitFactor": split_factor,
        "forecastSplitEffectiveDate": split_effective_date,
        "forecastShareBasis": share_basis_text,
        "forecastPeriod": period,
        # 予想が「どの事業年度のものか」を数値でも残す。配当グラフは事業年度で
        # 並んでいるので、表示文字列を読み直さずに棒の位置を決められる。
        "forecastFiscalYear": forecast_fiscal_year(period),
        "confirmedDividend": confirmed_adjusted if confirmed_adjusted is not None else confirmed,
        "confirmedFiscalYearEnd": str(fiscal_year_end)[:10] if fiscal_year_end else None,
    }


def forecast_fiscal_year(period: str | None) -> int | None:
    """「2027年3月期(予)」「FY2027」などから対象の事業年度（決算期末の暦年）を拾う。"""
    if not isinstance(period, str):
        return None
    match = re.search(r"(\d{4})\s*年", period) or re.search(
        r"FY\s*(\d{4})", period, re.IGNORECASE
    )
    if not match:
        return None
    year = int(match.group(1))
    return year if 1990 <= year <= 2100 else None


class FetchError(RuntimeError):
    """1銘柄の取得失敗。全体を止めるべきかを status / kind で判断する。"""

    def __init__(self, message: str, *, kind: str, status: int | None = None):
        super().__init__(message)
        self.kind = kind
        self.status = status

    @property
    def is_fatal(self) -> bool:
        """認証・権限の失敗は銘柄固有ではないので、続けても全滅する。"""
        return self.status in FATAL_HTTP_STATUSES


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
        raise FetchError(
            f"{candidate.code}: edinetdb HTTP {error.code}",
            kind="http",
            status=error.code,
        ) from error
    except (URLError, TimeoutError) as error:
        raise FetchError(
            f"{candidate.code}: edinetdb通信失敗", kind="network"
        ) from error
    except json.JSONDecodeError as error:
        raise FetchError(
            f"{candidate.code}: edinetdb応答が不正なJSONです", kind="invalid_json"
        ) from error
    try:
        remaining = int(remaining_header) if remaining_header is not None else None
    except ValueError:
        remaining = None
    try:
        parsed = parse_forecast_response(body, candidate.fiscal_month)
    except ValueError as error:
        raise FetchError(f"{candidate.code}: {error}", kind="parse") from error
    return parsed, remaining


def record_failure(
    state: dict[str, Any], code: str, attempted_at: str, error: FetchError
) -> None:
    """失敗を状態に書き留める。前回までに取れている予想は消さない。

    次に成功したときは parsed で丸ごと置き換わるので、ここで足した項目も
    一緒に消える（＝失敗の痕跡が残るのは失敗している間だけ）。
    """
    previous = state["stocks"].get(code)
    record = dict(previous) if isinstance(previous, dict) else {}
    record["lastFailedAt"] = attempted_at
    record["lastFailureKind"] = error.kind
    record["lastFailureDetail"] = str(error)[:200]
    count = record.get("failureCount")
    record["failureCount"] = (
        count + 1
        if isinstance(count, int) and not isinstance(count, bool) and count >= 0
        else 1
    )
    state["stocks"][code] = record


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
    codes = load_dividend_codes(args.fiscal_dividends, args.calendar_dividends)
    dividend_yields = load_dividend_yields(args.prices_url)
    state = load_state(args.state)
    priority_codes = read_priority_codes()
    candidates, missing_feed = build_candidates(
        codes,
        dividend_yields,
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
    normal_count = len(candidates) - due_count
    normal_processed = 0
    no_forecast = 0
    last_remaining: int | None = None
    processed = 0
    failed = 0
    consecutive_failures = 0
    fatal: FetchError | None = None

    def save_progress() -> None:
        if normal_count:
            state["queuePosition"] = (
                normal_position + normal_processed
            ) % normal_count
        write_state(args.state, state)

    for index, candidate in enumerate(selected, start=1):
        try:
            parsed, last_remaining = fetch_one(candidate, api_key)
        except FetchError as error:
            # 1銘柄の失敗でその日の取得を全部捨てない。既存の保存値は
            # そのまま残し、失敗の記録だけ足して次の銘柄へ進む。
            failed += 1
            consecutive_failures += 1
            record_failure(state, candidate.code, fetched_at, error)
            print(f"取得失敗（続行）: {error}", file=sys.stderr)
            # 失敗した銘柄でも待ち行列は進める（次回は次の銘柄から始める）。
            if not candidate.is_due:
                normal_processed += 1
            if error.is_fatal:
                fatal = error
                break
            if consecutive_failures >= CONSECUTIVE_FAILURE_LIMIT:
                fatal = FetchError(
                    f"{consecutive_failures}件続けて失敗しました（最後: {error}）",
                    kind="consecutive",
                )
                break
        else:
            consecutive_failures = 0
            parsed["lastFetchedAt"] = fetched_at
            state["stocks"][candidate.code] = parsed
            if parsed["forecastDividend"] is None:
                no_forecast += 1
            if not candidate.is_due:
                normal_processed += 1
            processed += 1
        if index % SAVE_INTERVAL == 0:
            save_progress()
        # 既定95件なら通常は残量5を残す。サーバー側残量が想定より少ない時も
        # 最低5件を温存して停止する。
        if last_remaining is not None and last_remaining <= 5:
            break

    save_progress()
    print(
        f"予想取得完了: {processed:,}件 "
        f"（予想なし {no_forecast:,}件、失敗 {failed:,}件、"
        f"対象 {len(candidates):,}件）"
    )
    if last_remaining is not None:
        print(f"edinetdb日次残量: {last_remaining:,}")
    print(f"状態保存: {args.state}")
    if fatal is not None:
        # ここまでの取得結果は保存済み。設定・APIキーの問題は気づけるように
        # 非0で終わる（個別銘柄の失敗では0で終わり、後続の処理を止めない）。
        raise SystemExit(f"取得を中断しました: {fatal}")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError) as error:
        print(f"エラー: {error}", file=sys.stderr)
        raise SystemExit(1) from error
