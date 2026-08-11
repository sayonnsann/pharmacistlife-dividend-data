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
from datetime import date, timedelta
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
EDINETDB_EVENTS_URL = "https://edinetdb.jp/v1/events"
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
# 429（取得枠切れ）が連続した場合は、取れた分を保存して正常終了する。
# 認証エラーや通信障害など、429以外の連続失敗は従来どおり異常終了させる。
RATE_LIMIT_LOW_THRESHOLD = 5

# ---------------------------------------------------------------------------
# 開示イベント（/v1/events）まわりの設定
#
# 決算月から作る近似発表日は最大2週間ずれる。東計電算(4746)は12月決算で
# 近似日が8/15だが、実際のQ2発表は8/3で、そこで分割と大幅増配を出した。
# 近似日を待つと12日間気づけないので、実際の開示イベントを毎日見て
# 「今日取り直すべき銘柄」を横から差し込む。
# ---------------------------------------------------------------------------
EVENT_PAGE_SIZE = 100
# 1日にイベント枠へ割り当てる会社数の上限。0にするとイベントAPIを一切
# 呼ばず、従来どおりの巡回だけになる（不具合時の緊急停止スイッチ。
# DVC_EVENT_SLOTS で上書きできる）。
EVENT_SLOT_DEFAULT = 20
# 取りに行く種別と、1日に許すページ数（＝リクエスト数）の上限。
# 配当修正は繁忙期でも1日31件、分割・併合は年168件しかないので取り切れる。
# 決算短信は繁忙期に1日780件あり、必ず打ち切られる（打ち切り件数はログに出す）。
EVENT_SOURCES: tuple[tuple[str, int], ...] = (
    ("dividend_revision", 2),
    ("stock_split", 1),
    ("reverse_split", 1),
    ("earnings_summary", 2),
)
# イベント枠の中の並び順。小さいほど先に取る。
EVENT_TYPE_RANK = {
    "dividend_revision": 0,
    "stock_split": 1,
    "reverse_split": 1,
    "earnings_summary": 2,
}
# 前回の記録が無いときに遡る日数。土日・祝日と、1〜2回の実行失敗を吸収する。
EVENT_LOOKBACK_DAYS = 3
# 長く止まっていた後でも、これ以上は遡らない（枠を食い潰さないため）。
EVENT_MAX_LOOKBACK_DAYS = 14
# 処理済みイベントIDの保持上限。実際は1日最大20件しか増えないので届かないが、
# 状態ファイルが無限に太らないための歯止め。
EVENT_SEEN_LIMIT = 2000
# イベント枠から溢れたり429で取れなかったイベントの持ち越し上限。
# 5回または14日試しても取れなければ、以後は自動再試行せずログに残す。
EVENT_PENDING_MAX_ATTEMPTS = 5
EVENT_PENDING_MAX_DAYS = 14
# metadata.dividend_direction がこれらのときは「配当に動きなし」とみなす。
EVENT_NO_DIVIDEND_SIGNAL = frozenset({"", "none", "unchanged", "flat", "-"})


@dataclass(frozen=True)
class Event:
    announced_on: date
    kind: str
    period: str


@dataclass(frozen=True)
class DisclosureEvent:
    """/v1/events の1レコード（実際に開示された事実）。

    近似日から作る Event と違い、こちらは実日付である。
    """

    event_id: str
    event_type: str
    event_date: date
    sec_code: str
    edinet_code: str
    is_earnings: bool
    has_dividend_signal: bool

    @property
    def type_rank(self) -> int:
        return EVENT_TYPE_RANK.get(self.event_type, len(EVENT_TYPE_RANK))


@dataclass(frozen=True)
class PendingEvent:
    """検索窓から外れても再試行するイベントと、その試行状況。"""

    event: DisclosureEvent
    first_seen_at: date
    attempts: int


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


def empty_event_state() -> dict[str, Any]:
    return {
        "lastCheckedAt": None,
        "lastEventDate": None,
        "seen": {},
        "pending": {},
    }


def empty_rate_limit_state() -> dict[str, Any]:
    return {"consecutiveDays": 0, "lastStoppedAt": None}


def empty_state() -> dict[str, Any]:
    return {
        "version": STATE_VERSION,
        "queuePosition": 0,
        "stocks": {},
        "events": empty_event_state(),
        "rateLimit": empty_rate_limit_state(),
    }


def event_state(state: dict[str, Any]) -> dict[str, Any]:
    """events ブロックを取り出す。壊れていたら黙って作り直す。

    stocks と違って、ここが壊れていても失うのは「どのイベントを見たか」の
    記憶だけである。日次更新そのものを止める理由にはならないので、
    例外にはせず空から作り直す（最悪、同じ銘柄をもう一度取り直すだけ）。
    """
    block = state.get("events")
    if not isinstance(block, dict):
        block = empty_event_state()
        state["events"] = block
    if not isinstance(block.get("seen"), dict):
        block["seen"] = {}
    if not isinstance(block.get("pending"), dict):
        block["pending"] = {}
    return block


def rate_limit_state(state: dict[str, Any]) -> dict[str, Any]:
    """429打ち切りの連続日数を保持する。古いstateには後付けする。"""
    block = state.get("rateLimit")
    if not isinstance(block, dict):
        block = empty_rate_limit_state()
        state["rateLimit"] = block
    days = block.get("consecutiveDays", 0)
    if isinstance(days, bool) or not isinstance(days, int) or days < 0:
        block["consecutiveDays"] = 0
    if block.get("lastStoppedAt") is not None and not isinstance(
        block.get("lastStoppedAt"), str
    ):
        block["lastStoppedAt"] = None
    return block


def update_rate_limit_streak(
    state: dict[str, Any], today: date, stopped: bool
) -> int:
    """今日の429打ち切り結果を記録し、連続日数を返す。"""
    block = rate_limit_state(state)
    if not stopped:
        block["consecutiveDays"] = 0
        block["lastStoppedAt"] = None
        return 0

    days = block["consecutiveDays"]
    last_text = block.get("lastStoppedAt")
    try:
        last = (
            date.fromisoformat(last_text[:10])
            if isinstance(last_text, str)
            else None
        )
    except ValueError:
        last = None
    if last == today:
        # 同じ日を再実行しても1日分として数える。
        days = max(days, 1)
    elif last == today - timedelta(days=1):
        days += 1
    else:
        days = 1
    block["consecutiveDays"] = days
    block["lastStoppedAt"] = today.isoformat()
    return days


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
    # events を持たない古い状態ファイル（本番で動いているのはこれ）も
    # そのまま読める。versionは上げない：上げると稼働中の日次更新が
    # 「未対応のstate version」で止まってしまう。
    event_state(state)
    rate_limit_state(state)
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


# ---------------------------------------------------------------------------
# 開示イベントを見て「今日取り直す銘柄」を決める
# ---------------------------------------------------------------------------


def event_slot_size() -> int:
    raw = os.environ.get("DVC_EVENT_SLOTS", str(EVENT_SLOT_DEFAULT))
    try:
        value = int(raw)
    except ValueError as error:
        raise ValueError("DVC_EVENT_SLOTSは整数で指定してください") from error
    if not 0 <= value <= 100:
        raise ValueError("DVC_EVENT_SLOTSは0〜100で指定してください")
    return value


def event_window(block: dict[str, Any], today: date) -> tuple[date, date]:
    """イベントを問い合わせる期間を決める。

    前回見た日が分かっていればそこから、分かっていなければ数日前から。
    どちらの場合も最低 EVENT_LOOKBACK_DAYS 日分は重ねて見る（枠に入り
    きらず持ち越したイベントを、翌日また拾えるようにするため）。
    長期間止まっていた後でも EVENT_MAX_LOOKBACK_DAYS より前は見ない。
    """
    default_start = today - timedelta(days=EVENT_LOOKBACK_DAYS)
    earliest = today - timedelta(days=EVENT_MAX_LOOKBACK_DAYS)
    raw = block.get("lastCheckedAt")
    last = None
    if isinstance(raw, str):
        try:
            last = date.fromisoformat(raw[:10])
        except ValueError:
            last = None
    start = default_start if last is None else min(last, default_start)
    return max(start, earliest), today


def parse_event_record(record: Any) -> DisclosureEvent | None:
    """/v1/events の1件を読む。読めない・関心のない種別はNone。"""
    if not isinstance(record, dict):
        return None
    event_type = str(
        first_present(record, "event_type", "eventType") or ""
    ).strip()
    if event_type not in EVENT_TYPE_RANK:
        return None
    day_text = iso_date_text(first_present(record, "event_date", "eventDate"))
    if day_text is None:
        return None
    event_date = date.fromisoformat(day_text)
    sec_code = str(first_present(record, "sec_code", "secCode") or "").strip().upper()
    edinet_code = (
        str(first_present(record, "edinet_code", "edinetCode") or "").strip().upper()
    )
    if not sec_code and not edinet_code:
        return None

    raw_metadata = record.get("metadata")
    metadata = raw_metadata if isinstance(raw_metadata, dict) else {}
    direction = str(metadata.get("dividend_direction") or "").strip().lower()

    raw_id = first_present(record, "event_id", "eventId", "id")
    if raw_id is None or not str(raw_id).strip():
        # 応答にIDが無いので、同じ開示を指す安定なキーを自分で組む。
        identity = f"{event_type}:{sec_code or edinet_code}:{day_text}"
    else:
        identity = str(raw_id).strip()[:120]

    return DisclosureEvent(
        event_id=identity,
        event_type=event_type,
        event_date=event_date,
        sec_code=sec_code,
        edinet_code=edinet_code,
        is_earnings=bool(metadata.get("is_earnings")),
        has_dividend_signal=direction not in EVENT_NO_DIVIDEND_SIGNAL,
    )


def fetch_event_page(
    event_type: str, since: date, until: date, offset: int, api_key: str
) -> tuple[list[Any], int | None, int | None]:
    """1ページ分のイベントを取る。(records, total, next_offset)"""
    query = urlencode(
        {
            "since": since.isoformat(),
            "until": until.isoformat(),
            "event_type": event_type,
            "limit": EVENT_PAGE_SIZE,
            "offset": offset,
        }
    )
    request = Request(
        f"{EDINETDB_EVENTS_URL}?{query}",
        headers={
            "X-API-Key": api_key,
            "Accept": "application/json",
            "User-Agent": "dividend-store-updater/1.0",
        },
    )
    try:
        with urlopen(request, timeout=45) as response:
            body = json.load(response)
    except HTTPError as error:
        raise FetchError(
            f"events {event_type}: HTTP {error.code}",
            kind="http",
            status=error.code,
        ) from error
    except (URLError, TimeoutError) as error:
        raise FetchError(
            f"events {event_type}: 通信失敗", kind="network"
        ) from error
    except json.JSONDecodeError as error:
        raise FetchError(
            f"events {event_type}: 不正なJSONです", kind="invalid_json"
        ) from error

    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, list):
        raise FetchError(f"events {event_type}: data配列がありません", kind="parse")

    total: int | None = None
    next_offset: int | None = None
    meta = body.get("meta") if isinstance(body, dict) else None
    pagination = meta.get("pagination") if isinstance(meta, dict) else None
    if isinstance(pagination, dict):
        raw_total = pagination.get("total")
        if isinstance(raw_total, int) and not isinstance(raw_total, bool):
            total = raw_total
        raw_next = pagination.get("next_offset")
        if isinstance(raw_next, int) and not isinstance(raw_next, bool):
            next_offset = raw_next
    return data, total, next_offset


def collect_events(
    since: date, until: date, api_key: str
) -> tuple[list[DisclosureEvent], int, int, int]:
    """全種別のイベントを集める。

    返り値は (events, 送ったリクエスト数, 打ち切り件数, 成功したページ数)。
    リクエスト数と成功数を分けているのは、用途が違うため:
      - リクエスト数 … 1日100件の枠から引く（失敗した分も枠は減っている）
      - 成功数       … 「この期間は確かに見た」と言えるかの判断に使う

    種別ごとに独立して取る。1種別が失敗しても他は使える（配当修正だけ
    取れれば、東計電算のようなケース以外はだいたい拾える）。
    """
    collected: list[DisclosureEvent] = []
    requests_used = 0
    ok_pages = 0
    truncated_total = 0
    for event_type, max_pages in EVENT_SOURCES:
        offset = 0
        fetched = 0
        reported_total: int | None = None
        try:
            for _ in range(max_pages):
                # 送った時点で数える。応答が壊れていてもサーバー側の枠は
                # 減っているので、成功した分だけ数えると1日100件を超える。
                requests_used += 1
                data, total, next_offset = fetch_event_page(
                    event_type, since, until, offset, api_key
                )
                ok_pages += 1
                fetched += len(data)
                if total is not None:
                    reported_total = total
                for record in data:
                    parsed = parse_event_record(record)
                    if parsed is not None:
                        collected.append(parsed)
                if not data or next_offset is None or next_offset <= offset:
                    break
                offset = next_offset
        except FetchError as error:
            # イベントが取れないのは「今日は近似日方式に戻る」だけの話で、
            # 予想取得そのものは止めない。
            print(f"イベント取得失敗（続行）: {error}", file=sys.stderr)
            continue

        left = 0 if reported_total is None else max(reported_total - fetched, 0)
        truncated_total += left
        summary = f"イベント取得: {event_type} {fetched}件"
        if reported_total is not None:
            summary += f" / 全{reported_total}件"
        if left:
            # 黙って切り捨てない。繁忙期の決算短信はここで必ず削れる。
            summary += f"（{left}件は上限{max_pages}ページで打ち切り）"
        print(summary)
    return collected, requests_used, truncated_total, ok_pages


def index_candidates(
    candidates: list[Candidate],
) -> tuple[dict[str, Candidate], dict[str, Candidate]]:
    by_code = {candidate.code: candidate for candidate in candidates}
    by_edinet = {candidate.edinet_code: candidate for candidate in candidates}
    return by_code, by_edinet


def match_candidate(
    event: DisclosureEvent,
    by_code: dict[str, Candidate],
    by_edinet: dict[str, Candidate],
) -> Candidate | None:
    """イベントの銘柄コード／EDINETコードから、待ち行列の候補を引く。"""
    code = normalize_code(event.sec_code)
    if code and code in by_code:
        return by_code[code]
    # EDINET側は5桁（末尾0）で持っていることがある。
    raw = event.sec_code
    if len(raw) == 5 and raw.endswith("0"):
        code = normalize_code(raw[:4])
        if code and code in by_code:
            return by_code[code]
    if event.edinet_code and event.edinet_code in by_edinet:
        return by_edinet[event.edinet_code]
    return None


def pending_record(
    event: DisclosureEvent, today: date, *, attempts: int = 0
) -> dict[str, Any]:
    """イベントをstateへ保存する形にする。APIの検索窓外でも復元できる情報を残す。"""
    return {
        "eventType": event.event_type,
        "eventDate": event.event_date.isoformat(),
        "secCode": event.sec_code,
        "edinetCode": event.edinet_code,
        "isEarnings": event.is_earnings,
        "hasDividendSignal": event.has_dividend_signal,
        "firstSeenAt": today.isoformat(),
        "attempts": attempts,
    }


def parse_pending_event(event_id: str, raw: Any) -> PendingEvent | None:
    """stateの持ち越し1件を検証して復元する。壊れた1件だけを捨てる。"""
    if not isinstance(raw, dict) or not isinstance(event_id, str) or not event_id:
        return None
    event_type = str(raw.get("eventType") or "").strip()
    event_date_text = str(raw.get("eventDate") or "")[:10]
    try:
        event_date = date.fromisoformat(event_date_text)
    except ValueError:
        return None
    sec_code = str(raw.get("secCode") or "").strip().upper()
    edinet_code = str(raw.get("edinetCode") or "").strip().upper()
    if event_type not in EVENT_TYPE_RANK or not sec_code and not edinet_code:
        return None
    first_seen_text = str(raw.get("firstSeenAt") or "")[:10]
    try:
        first_seen_at = date.fromisoformat(first_seen_text)
    except ValueError:
        return None
    attempts = raw.get("attempts", 0)
    if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts < 0:
        return None
    return PendingEvent(
        event=DisclosureEvent(
            event_id=event_id,
            event_type=event_type,
            event_date=event_date,
            sec_code=sec_code,
            edinet_code=edinet_code,
            is_earnings=bool(raw.get("isEarnings")),
            has_dividend_signal=bool(raw.get("hasDividendSignal")),
        ),
        first_seen_at=first_seen_at,
        attempts=attempts,
    )


def load_pending_events(
    block: dict[str, Any], today: date
) -> dict[str, PendingEvent]:
    """期限切れの持ち越しをstateから外し、外した理由をログに出す。"""
    pending = block["pending"]
    seen = block["seen"]
    result: dict[str, PendingEvent] = {}
    for event_id, raw in list(pending.items()):
        parsed = parse_pending_event(event_id, raw)
        if parsed is None:
            print(f"イベント持ち越しを破棄しました（状態が不正: {event_id}）")
            del pending[event_id]
            continue
        if event_id in seen:
            # 成功記録が残っている方を正とする。古いstateの重複を掃除する。
            del pending[event_id]
            continue
        age = (today - parsed.first_seen_at).days
        if (
            parsed.attempts >= EVENT_PENDING_MAX_ATTEMPTS
            or age > EVENT_PENDING_MAX_DAYS
        ):
            print(
                "イベント持ち越しを諦めました: "
                f"{parsed.event.sec_code or parsed.event.edinet_code} "
                f"（試行{parsed.attempts}回・{max(age, 0)}日経過）"
            )
            del pending[event_id]
            continue
        result[event_id] = parsed
    return result


def add_pending_events(
    block: dict[str, Any],
    events: list[DisclosureEvent],
    candidates: list[Candidate],
    today: date,
) -> None:
    """新しく見つけたがまだ取得成功していないイベントをstateへ積む。"""
    pending = block["pending"]
    seen = block["seen"]
    by_code, by_edinet = index_candidates(candidates)
    for event in events:
        if event.event_id in seen or event.event_id in pending:
            continue
        if event.event_type == "earnings_summary" and not event.is_earnings:
            continue
        candidate = match_candidate(event, by_code, by_edinet)
        if candidate is None:
            continue
        if (
            candidate.last_fetched is not None
            and candidate.last_fetched > event.event_date
        ):
            # 開示後に取得済みなら、新しいデータを持っているので再試行不要。
            continue
        pending[event.event_id] = pending_record(event, today)


def mark_pending_attempts(
    block: dict[str, Any],
    event_ids_by_code: dict[str, dict[str, str]],
    picked_codes: set[str],
    today: date,
) -> None:
    """実際に今日の予想取得へ回した持ち越しの試行回数を増やす。"""
    for code, event_ids in event_ids_by_code.items():
        if code not in picked_codes:
            continue
        for event_id in event_ids:
            raw = block["pending"].get(event_id)
            if not isinstance(raw, dict):
                continue
            attempts = raw.get("attempts", 0)
            if isinstance(attempts, bool) or not isinstance(attempts, int):
                attempts = 0
            raw["attempts"] = max(attempts, 0) + 1
            raw["lastAttemptAt"] = today.isoformat()


def pending_candidate_codes(
    block: dict[str, Any], candidates: list[Candidate], today: date
) -> set[str]:
    """持ち越し中の銘柄を通常巡回から外す（同日に二度叩かないため）。"""
    pending = load_pending_events(block, today)
    by_code, by_edinet = index_candidates(candidates)
    codes: set[str] = set()
    for item in pending.values():
        candidate = match_candidate(item.event, by_code, by_edinet)
        if candidate is None:
            continue
        if (
            candidate.last_fetched is None
            or candidate.last_fetched <= item.event.event_date
        ):
            codes.add(candidate.code)
    return codes


def event_sort_key(
    item: tuple[Candidate, DisclosureEvent],
    pending_ids: set[str] | frozenset[str] | None = None,
) -> tuple[Any, ...]:
    candidate, event = item
    pending_ids = pending_ids or set()
    return (
        # 既知の持ち越しは検索窓から消えると再発見できないため、新規発表
        # より先に処理する。新規発表は数日間はAPIの検索窓に残る。
        0 if event.event_id in pending_ids else 1,
        event.type_rank,
        # 決算短信の中で配当に触れているものを、触れていないものより先に。
        0 if event.has_dividend_signal else 1,
        -event.event_date.toordinal(),
        candidate.priority_rank is None,
        candidate.priority_rank if candidate.priority_rank is not None else 0,
        -candidate.dividend_yield,
        candidate.code,
    )


def plan_event_slots(
    events: list[DisclosureEvent],
    candidates: list[Candidate],
    seen: dict[str, Any],
    slots: int,
    pending_ids: set[str] | frozenset[str] | None = None,
) -> tuple[list[Candidate], dict[str, dict[str, str]], dict[str, int]]:
    """イベント枠に入れる銘柄を決める。

    返り値は (取り直す候補, 銘柄コード→{イベントID: 発生日}, 集計)。
    2つ目は「その銘柄を取れたら、この開示はもう見た」と記録するためのもの。
    枠は会社数で数えるので、同じ銘柄が複数のイベントに出ても1件である。
    """
    by_code, by_edinet = index_candidates(candidates)
    stats = {
        "matched": 0,
        "unmatched": 0,
        "already_seen": 0,
        "already_fetched": 0,
        "not_earnings": 0,
    }
    best: dict[str, tuple[Candidate, DisclosureEvent]] = {}
    ids_by_code: dict[str, dict[str, str]] = {}

    for event in events:
        if event.event_type == "earnings_summary" and not event.is_earnings:
            # 決算短信の枠に入っているが決算そのものではない開示。
            stats["not_earnings"] += 1
            continue
        candidate = match_candidate(event, by_code, by_edinet)
        if candidate is None:
            # 配当履歴が無い・feedが無いなど、そもそも待ち行列にいない銘柄。
            stats["unmatched"] += 1
            continue
        if event.event_id in seen:
            stats["already_seen"] += 1
            continue
        if (
            candidate.last_fetched is not None
            and candidate.last_fetched > event.event_date
        ):
            # 開示日より後に取得済み＝すでに新しい値を持っている。
            # 同日は「開示前に取った」可能性があるので取り直す側に倒す。
            stats["already_fetched"] += 1
            continue
        stats["matched"] += 1
        ids_by_code.setdefault(candidate.code, {})[event.event_id] = (
            event.event_date.isoformat()
        )
        current = best.get(candidate.code)
        if current is None or event_sort_key(
            (candidate, event), pending_ids
        ) < event_sort_key(current, pending_ids):
            best[candidate.code] = (candidate, event)

    ordered = sorted(
        best.values(), key=lambda item: event_sort_key(item, pending_ids)
    )
    picks = [candidate for candidate, _ in ordered[: max(slots, 0)]]
    picked_codes = {candidate.code for candidate in picks}
    ids_by_code = {
        code: ids for code, ids in ids_by_code.items() if code in picked_codes
    }
    stats["companies"] = len(best)
    stats["picked"] = len(picks)
    return picks, ids_by_code, stats


def prune_seen(seen: dict[str, Any], today: date) -> None:
    """古い処理済みイベントを捨てる。問い合わせ窓から外れた分はもう来ない。"""
    earliest = (today - timedelta(days=EVENT_MAX_LOOKBACK_DAYS)).isoformat()
    stale = [
        key
        for key, value in seen.items()
        if not isinstance(value, str) or value < earliest
    ]
    for key in stale:
        del seen[key]
    if len(seen) > EVENT_SEEN_LIMIT:
        newest = sorted(seen.items(), key=lambda item: item[1], reverse=True)
        seen.clear()
        seen.update(newest[:EVENT_SEEN_LIMIT])


def run_event_stage(
    state: dict[str, Any],
    candidates: list[Candidate],
    today: date,
    api_key: str,
) -> tuple[list[Candidate], dict[str, dict[str, str]], int]:
    """イベントを見て優先枠を組む。失敗しても従来の待ち行列は動かす。"""
    block = event_state(state)
    slots = event_slot_size()
    if slots <= 0:
        print("イベント枠: 無効（DVC_EVENT_SLOTS=0）")
        return [], {}, 0

    pending = load_pending_events(block, today)
    since, until = event_window(block, today)
    events: list[DisclosureEvent] = []
    requests_used = 0
    truncated = 0
    ok_pages = 0
    try:
        events, requests_used, truncated, ok_pages = collect_events(
            since, until, api_key
        )
    except (OSError, ValueError, RuntimeError) as error:
        # イベントAPIが落ちても、既にstateへ積んだ持ち越しは処理する。
        # イベントは「あれば早く気づける」ための仕組みで、日次更新の前提ではない。
        print(f"イベント取得を中止しました（続行）: {error}", file=sys.stderr)
        block["lastError"] = str(error)[:200]
        block["lastErrorAt"] = today.isoformat()

    if ok_pages:
        # 1ページでも応答があった日だけ「ここまで見た」を進める。全滅した日に
        # 進めると、その日の開示を二度と見ないことになる。
        # 一部の種別だけ落ちた日に進めてしまっても、問い合わせ期間は必ず
        # 3日重ねるので、翌日以降に拾い直せる。
        block["lastCheckedAt"] = today.isoformat()
        block.pop("lastError", None)
        block.pop("lastErrorAt", None)
    else:
        block["lastError"] = "イベントAPIから1件も応答がありませんでした"
        block["lastErrorAt"] = today.isoformat()

    if events:
        block["lastEventDate"] = max(
            event.event_date for event in events
        ).isoformat()

    add_pending_events(block, events, candidates, today)
    # 新規イベントと持ち越しを同じ枠で比較する。既知の持ち越しを先に
    # 並べるのは、検索窓から消えた後に救える経路が他にないため。
    pending = load_pending_events(block, today)
    pending_ids = set(pending)
    combined_events: list[DisclosureEvent] = [
        item.event for item in pending.values()
    ]
    pending_event_ids = set(pending)
    combined_events.extend(
        event for event in events if event.event_id not in pending_event_ids
    )
    picks, ids_by_code, stats = plan_event_slots(
        combined_events,
        candidates,
        block["seen"],
        slots,
        pending_ids,
    )
    print(
        f"イベント枠: {stats['picked']}件/{slots}枠 "
        f"（対象社数={stats['companies']} 期間={since.isoformat()}〜"
        f"{until.isoformat()} リクエスト={requests_used}）"
    )
    # noEarnings が急に events と同じ数になったら、metadata.is_earnings の
    # 意味か有無が変わった合図（決算短信経由の検知が黙って死ぬ形）。
    print(
        f"events requests={requests_used} events={len(events)} "
        f"companies={stats['companies']} picked={stats['picked']} "
        f"truncated={truncated} unmatched={stats['unmatched']} "
        f"seen={stats['already_seen']} fresh={stats['already_fetched']} "
        f"noEarnings={stats['not_earnings']} pending={len(pending)}"
    )
    if picks:
        print("イベント枠の銘柄: " + " ".join(pick.code for pick in picks))
    return picks, ids_by_code, requests_used


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


def optional_forecast_number(
    record: dict[str, Any], *keys: str
) -> float | int | None:
    """業績予想の数値を読む（赤字・EPSマイナスを許容）。

    配当用の optional_number は、利回りや配当額の異常値を弾くために
    0以上へ制限している。業績予想は営業損失・純損失・マイナスEPSが
    正常な値として返るため、ここでは符号を保持する。
    """
    value = finite_number(first_present(record, *keys))
    if value is None or abs(value) > 1_000_000_000_000_000:
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


def forecast_quarter(record: dict[str, Any]) -> int | None:
    """決算短信の四半期区分を1〜4へ正規化する。

    edinetdbの実応答は quarter を数値で返す。将来の表記揺れにも耐えるよう、
    Q2 / 2Q / 第2四半期も受け付ける。
    """
    raw = first_present(
        record,
        "quarter",
        "fiscal_quarter",
        "fiscalQuarter",
        "period_quarter",
        "periodQuarter",
    )
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)) and float(raw).is_integer():
        value = int(raw)
        return value if value in (1, 2, 3, 4) else None
    text = str(raw or "").strip().upper()
    match = re.fullmatch(r"(?:Q|第)?\s*([1-4])\s*(?:Q|四半期)?", text)
    return int(match.group(1)) if match else None


def forecast_period_type(quarter: int | None) -> str | None:
    """forecast_* が指す期の種類を返す。

    Q1〜Q3は当期通期予想、Q4は翌期通期予想というAPI仕様に対応する。
    """
    if quarter in (1, 2, 3):
        return "current"
    if quarter == 4:
        return "next"
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
    numeric_quarter = forecast_quarter(record)
    if fye_date is not None:
        # quarter が実応答にある場合はこちらを優先する。Q4短信の
        # forecast_* は翌期を指すため、開示日と期末日の前後だけで推定すると
        # 期ラベルを取り違える余地がある。
        if numeric_quarter in (1, 2, 3, 4):
            target_year = fye_date.year + (1 if numeric_quarter == 4 else 0)
            return f"{target_year}年{fye_date.month}月期(予)"
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
    try:
        numeric_year = int(year)
    except (TypeError, ValueError):
        numeric_year = 0
    numeric_quarter = numeric_quarter or 0
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
    quarter = forecast_quarter(latest)
    period = forecast_period(latest, fiscal_month)
    period_type = forecast_period_type(quarter)

    # 業績予想は配当予想と同じ決算短信の同じ行に入っている。ここで別APIを
    # 呼ばずに全項目と前年比を保存しておけば、Q4の翌期予想もQ1〜Q3の
    # 当期予想も、後段で期ラベル付きの同じデータ構造として表示できる。
    earnings_forecast = {
        "forecastRevenue": optional_forecast_number(
            latest, "forecast_revenue", "forecastRevenue"
        ),
        "forecastRevenueChange": optional_forecast_number(
            latest, "forecast_revenue_change", "forecastRevenueChange"
        ),
        "forecastOperatingIncome": optional_forecast_number(
            latest, "forecast_operating_income", "forecastOperatingIncome"
        ),
        "forecastOperatingIncomeChange": optional_forecast_number(
            latest,
            "forecast_operating_income_change",
            "forecastOperatingIncomeChange",
        ),
        "forecastOrdinaryIncome": optional_forecast_number(
            latest, "forecast_ordinary_income", "forecastOrdinaryIncome"
        ),
        "forecastOrdinaryIncomeChange": optional_forecast_number(
            latest,
            "forecast_ordinary_income_change",
            "forecastOrdinaryIncomeChange",
        ),
        "forecastNetIncome": optional_forecast_number(
            latest, "forecast_net_income", "forecastNetIncome"
        ),
        "forecastNetIncomeChange": optional_forecast_number(
            latest, "forecast_net_income_change", "forecastNetIncomeChange"
        ),
        "forecastEps": optional_forecast_number(
            latest, "forecast_eps", "forecastEps"
        ),
        "forecastEpsChange": optional_forecast_number(
            latest, "forecast_eps_change", "forecastEpsChange"
        ),
    }
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
        # quarter は「この予想を出した短信」の区分。forecastPeriodType は
        # forecast_* が当期か翌期かを機械的に判別するための区分。
        "forecastQuarter": quarter,
        "forecastQuarterLabel": f"Q{quarter}" if quarter else None,
        "forecastPeriodType": period_type,
        "forecastKind": "forecast",
        "confirmedDividend": confirmed_adjusted if confirmed_adjusted is not None else confirmed,
        "confirmedFiscalYearEnd": str(fiscal_year_end)[:10] if fiscal_year_end else None,
        **earnings_forecast,
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

    def __init__(
        self,
        message: str,
        *,
        kind: str,
        status: int | None = None,
        remaining: int | None = None,
    ):
        super().__init__(message)
        self.kind = kind
        self.status = status
        self.remaining = remaining

    @property
    def is_fatal(self) -> bool:
        """認証・権限の失敗は銘柄固有ではないので、続けても全滅する。"""
        return self.status in FATAL_HTTP_STATUSES

    @property
    def is_rate_limited(self) -> bool:
        return self.status == 429


def parse_rate_limit_remaining(value: Any) -> int | None:
    try:
        remaining = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return remaining if remaining >= 0 else None


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
            remaining = parse_rate_limit_remaining(
                response.headers.get("X-RateLimit-Remaining")
            )
    except HTTPError as error:
        raise FetchError(
            f"{candidate.code}: edinetdb HTTP {error.code}",
            kind="http",
            status=error.code,
            remaining=parse_rate_limit_remaining(
                getattr(error, "headers", {}).get("X-RateLimit-Remaining")
                if getattr(error, "headers", None) is not None
                else None
            ),
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
    if args.dry_run:
        queue, due_count, normal_position = ordered_queue(candidates, state)
        print("dry-run: イベントAPIは呼ばないのでイベント枠は空です")
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

    # イベント枠を先に決める。ここで選んだ銘柄は従来の待ち行列から抜いて
    # おく（同じ銘柄を1日に2回叩かないため、かつ巡回位置の数え方を
    # 壊さないため）。
    event_picks, event_ids_by_code, event_requests = run_event_stage(
        state, candidates, args.today, api_key
    )
    picked_codes = {candidate.code for candidate in event_picks}
    pending_codes = (
        pending_candidate_codes(event_state(state), candidates, args.today)
        if event_slot_size() > 0
        else set()
    )
    remaining = [
        candidate
        for candidate in candidates
        if candidate.code not in picked_codes
        and candidate.code not in pending_codes
    ]
    tail, due_count, normal_position = ordered_queue(remaining, state)
    queue = event_picks + tail

    # イベント取得に使ったリクエストも同じ1日100件の枠を食う。
    # 枠が足りなくならないよう、使った分だけ予想取得の上限を下げる。
    limit = min(max(env_daily_limit() - event_requests, 0), len(queue))
    selected = queue[:limit]
    fetched_at = args.today.isoformat()
    normal_count = len(remaining) - due_count
    normal_processed = 0
    no_forecast = 0
    last_remaining: int | None = None
    processed = 0
    failed = 0
    consecutive_failures = 0
    consecutive_rate_limit_failures = 0
    rate_limit_stopped = False
    fatal: FetchError | None = None

    def save_progress() -> None:
        if normal_count:
            state["queuePosition"] = (
                normal_position + normal_processed
            ) % normal_count
        prune_seen(event_state(state)["seen"], args.today)
        load_pending_events(event_state(state), args.today)
        write_state(args.state, state)

    for index, candidate in enumerate(selected, start=1):
        # イベント枠の銘柄は巡回の外から差し込んでいるので、巡回位置は
        # 進めない（進めると別の銘柄が1つ飛ばされる）。
        counts_towards_rotation = (
            candidate.code not in picked_codes and not candidate.is_due
        )
        mark_pending_attempts(
            event_state(state), event_ids_by_code, {candidate.code}, args.today
        )
        try:
            parsed, last_remaining = fetch_one(candidate, api_key)
        except FetchError as error:
            # 1銘柄の失敗でその日の取得を全部捨てない。既存の保存値は
            # そのまま残し、失敗の記録だけ足して次の銘柄へ進む。
            failed += 1
            consecutive_failures += 1
            if error.is_rate_limited:
                consecutive_rate_limit_failures += 1
            else:
                consecutive_rate_limit_failures = 0
            record_failure(state, candidate.code, fetched_at, error)
            if error.remaining is not None:
                last_remaining = error.remaining
            print(f"取得失敗（続行）: {error}", file=sys.stderr)
            # 失敗した銘柄でも待ち行列は進める（次回は次の銘柄から始める）。
            # イベント枠側は「処理済み」にしないので、翌日また拾われる。
            if counts_towards_rotation and not error.is_rate_limited:
                normal_processed += 1
            if error.is_fatal:
                fatal = error
                break
            if (
                error.is_rate_limited
                and error.remaining is not None
                and error.remaining <= RATE_LIMIT_LOW_THRESHOLD
            ):
                rate_limit_stopped = True
                print(
                    "取得枠を使い切ったため、本日はここまでにします。"
                    "取れなかったイベント銘柄は次回以降に再試行します。",
                    file=sys.stderr,
                )
                break
            if consecutive_rate_limit_failures >= CONSECUTIVE_FAILURE_LIMIT:
                # 枠切れは当日中に続けても回復しない。ここまでの成功分と
                # 失敗した銘柄の状態を保存し、後続ステップへ進める。
                rate_limit_stopped = True
                print(
                    "取得枠を使い切ったため、本日はここまでにします。"
                    "取れなかったイベント銘柄は次回以降に再試行します。",
                    file=sys.stderr,
                )
                break
            if consecutive_failures >= CONSECUTIVE_FAILURE_LIMIT:
                fatal = FetchError(
                    f"{consecutive_failures}件続けて失敗しました（最後: {error}）",
                    kind="consecutive",
                )
                break
        else:
            consecutive_failures = 0
            consecutive_rate_limit_failures = 0
            parsed["lastFetchedAt"] = fetched_at
            triggers = event_ids_by_code.get(candidate.code)
            if triggers:
                # 何をきっかけに取り直したかを残す（後から追える形にする）。
                parsed["lastEventAt"] = max(triggers.values())
                # 取れた分だけ「見た」と記録する。失敗した銘柄は記録しないので
                # 翌日また同じイベントで拾われる。
                event_state(state)["seen"].update(
                    {event_id: day for event_id, day in triggers.items()}
                )
                for event_id in triggers:
                    event_state(state)["pending"].pop(event_id, None)
            state["stocks"][candidate.code] = parsed
            if parsed["forecastDividend"] is None:
                no_forecast += 1
            if counts_towards_rotation:
                normal_processed += 1
            processed += 1
        if index % SAVE_INTERVAL == 0:
            save_progress()
        # 既定95件なら通常は残量5を残す。サーバー側残量が想定より少ない時も
        # 最低5件を温存して停止する。
        if last_remaining is not None and last_remaining <= 5:
            break

    rate_limit_days = update_rate_limit_streak(
        state, args.today, rate_limit_stopped
    )
    save_progress()
    print(
        f"予想取得完了: {processed:,}件 "
        f"（予想なし {no_forecast:,}件、失敗 {failed:,}件、"
        f"対象 {len(candidates):,}件）"
    )
    # 上の行は人間向け。こちらは呼び出し側（CIなど）が拾う用に、桁区切りも
    # 日本語も入れずに出す。個別銘柄の失敗ではもう止まらないので、
    # まとまった数が失敗したことに気づく手がかりが要る。
    print(
        f"summary selected={len(selected)} ok={processed} failed={failed} "
        f"eventPicks={len(event_picks)} eventRequests={event_requests} "
        f"rateLimitStopped={'true' if rate_limit_stopped else 'false'} "
        f"rateLimitDays={rate_limit_days}"
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
