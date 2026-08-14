#!/usr/bin/env python3
"""分割・併合まわりの「要確認リスト」を、既存の取得結果から自動蓄積する。

新規のEDINETDB APIアクセスは行わない（forecasts_state.json / fiscal_dividends.json
という、日次更新(fetch_forecasts.py・build_store.py)が既に取得・生成しているファイル
だけを読む）。既存の巡回・重複取得防止のロジックには一切触れない。

入口は3つ（優先順）:

  1. 主入口: イベント待ち行列の滞留・脱落検知（trigger="api_overflow"）
     forecasts_state.json の events.pending を見て、
       - 持ち越し試行回数が上限(EVENT_PENDING_MAX_ATTEMPTS=5)に近い
       - 経過日数が上限(EVENT_PENDING_MAX_DAYS=14)に近い
       - 前回のスナップショットには居たのに、今回は pending にも seen にも
         居ない（＝上限を超えて脱落した）
     のいずれかに該当する分割・併合イベントを拾う。edinetdb.jpのイベント枠は
     1日最大31件+持ち越し上限5回/14日しかなく、決算月はイベントが集中して
     取得しきれない銘柄が出るため、取りこぼす前に人の目に上げる。
     ir_sites.json / ir_sites_candidates.json から公式IRサイトのURLを引いて
     detail に付ける（週次消化のとき、IRサイトへ直行して確認するため）。
     URLが見つからない銘柄は irUrl=null・irUrlSource="not_found" として記録する。

  2. 副入口: 通常のイベント取得（trigger="edinetdb_event"）
     まだ滞留していない（＝安全圏の）分割・併合イベントを、参考情報として
     軽く積んでおく。配当修正イベント(dividend_revision)・決算短信イベント
     (earnings_summary)は既存の巡回フローがそのまま処理するので対象外。

  3. 安全網: dpsSuspectYearsの新規発生差分（trigger="new_suspect_year"）
     fiscal_dividends.json の streakBasis（分割の基準がそろっていない年に
     annotate_split_basis.py が付ける印。データ提供元では旧称
     dpsSuspectYears と呼ばれていた同じ概念）で reliable=false になっている
     銘柄の breakYears を、前回スナップショットとの差分で見る。
     1・2のイベント検知より確実に遅れて気づく最後の砦という位置づけ
     （分割そのものの開示イベントを取り損ねた場合や、決算書類の遡及訂正で
     後から段差が判明した場合など）。

出力:
  data/pending_ir_review.json … {queue: [{code, name, trigger, detail,
      detectedAt, status}, ...]} に追記。重複排除は (code, trigger, イベントの
      識別子) 単位（識別子は detail.eventId、無ければ detail.year）。
      status は "pending" で追加し、以後このスクリプトは書き換えない
      （週次消化のときに人・Codex・Claudeが更新する）。
  data/ir_review_snapshot.json … 次回との差分計算に使う内部状態
      （suspectYears・pendingSplitEvents）。

週次のバッチ消化（Codex収集→Claude検証→台帳登録）の手順は
edinet-direct/TASK_BATCH_STATUS.md を参照。台帳登録先は
data/stock_actions_manual.json。

使い方（手動実行）:
  python3 scripts/build_ir_review_queue.py
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STATE = REPOSITORY_ROOT / "forecasts_state.json"
DEFAULT_FISCAL_DIVIDENDS = REPOSITORY_ROOT / "data" / "fiscal_dividends.json"
DEFAULT_EDINET_DIR = REPOSITORY_ROOT / "edinet"
DEFAULT_IR_SITES = REPOSITORY_ROOT / "data" / "ir_sites.json"
DEFAULT_IR_SITES_CANDIDATES = REPOSITORY_ROOT / "data" / "ir_sites_candidates.json"
DEFAULT_QUEUE = REPOSITORY_ROOT / "data" / "pending_ir_review.json"
DEFAULT_SNAPSHOT = REPOSITORY_ROOT / "data" / "ir_review_snapshot.json"

CODE_PATTERN = re.compile(r"^[0-9A-Z]{4}$")

# fetch_forecasts.py 側の上限と同じ値（乖離すると「近い」の意味が変わるので
# 変更時は両方合わせること。tests/test_build_ir_review_queue.py で一致を確認）。
EVENT_PENDING_MAX_ATTEMPTS = 5
EVENT_PENDING_MAX_DAYS = 14
# 「上限に近い」の閾値。上限そのものではなく1〜2手前で拾う
# （拾った翌日に脱落されると、要確認リストに載る前に消えてしまうため）。
NEAR_ATTEMPTS_THRESHOLD = EVENT_PENDING_MAX_ATTEMPTS - 1  # 4
NEAR_AGE_DAYS_THRESHOLD = EVENT_PENDING_MAX_DAYS - 2  # 12

# 主入口・副入口が対象にするイベント種別（分割・併合のみ）。
# dividend_revision（配当修正）・earnings_summary（決算短信）は既存の巡回
# フローがそのまま処理するので、ここでは対象にしない。
SPLIT_EVENT_TYPES = frozenset({"stock_split", "reverse_split"})


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument(
        "--fiscal-dividends", type=Path, default=DEFAULT_FISCAL_DIVIDENDS
    )
    parser.add_argument("--edinet-dir", type=Path, default=DEFAULT_EDINET_DIR)
    parser.add_argument("--ir-sites", type=Path, default=DEFAULT_IR_SITES)
    parser.add_argument(
        "--ir-sites-candidates", type=Path, default=DEFAULT_IR_SITES_CANDIDATES
    )
    parser.add_argument("--queue", type=Path, default=DEFAULT_QUEUE)
    parser.add_argument("--snapshot", type=Path, default=DEFAULT_SNAPSHOT)
    parser.add_argument(
        "--today",
        type=date.fromisoformat,
        default=None,
        metavar="YYYY-MM-DD",
        help="差分計算・detectedAtに使う日付（テスト用。省略時は実行日）",
    )
    return parser.parse_args(argv)


def load_json_optional(path: Path | None) -> Any:
    """壊れている・存在しないファイルは None を返す（呼び出し側が入口ごとに
    スキップの可否を決める。このスクリプト自体は非0で終了しない）。"""
    if path is None or not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as source:
            return json.load(source)
    except (OSError, json.JSONDecodeError) as error:
        print(f"::warning::{path} を読めませんでした: {error}", file=sys.stderr)
        return None


def normalize_code(value: Any) -> str:
    code = str(value or "").strip().upper()
    return code if CODE_PATTERN.fullmatch(code) else ""


# ---------------------------------------------------------------------------
# 銘柄コードの解決・名称引き
# ---------------------------------------------------------------------------


class CodeResolver:
    """secCode/edinetCode から edinet/{code}.json のティッカーコードへ解決する。

    fetch_forecasts.py の match_candidate と同じ規則（secCodeがそのまま4桁、
    または5桁末尾0を切り詰めたもの。それでも合わなければedinetCode索引）。
    edinetCode索引は3800件超のファイルを読むので、実際に必要になるまで
    作らない（大半のイベントはsecCodeで直接解決できる）。
    """

    def __init__(self, edinet_dir: Path) -> None:
        self.edinet_dir = edinet_dir
        self._edinet_code_index: dict[str, str] | None = None
        self._name_cache: dict[str, str] = {}

    def _index(self) -> dict[str, str]:
        if self._edinet_code_index is None:
            index: dict[str, str] = {}
            if self.edinet_dir.exists():
                for path in sorted(self.edinet_dir.glob("*.json")):
                    data = load_json_optional(path)
                    if not isinstance(data, dict):
                        continue
                    edinet_code = str(data.get("edinetCode") or "").strip().upper()
                    if edinet_code and edinet_code not in index:
                        index[edinet_code] = path.stem
            self._edinet_code_index = index
        return self._edinet_code_index

    def resolve(self, sec_code: Any, edinet_code: Any) -> str | None:
        code = normalize_code(sec_code)
        if code and (self.edinet_dir / f"{code}.json").exists():
            return code
        raw = str(sec_code or "").strip().upper()
        if len(raw) == 5 and raw.endswith("0"):
            code = normalize_code(raw[:4])
            if code and (self.edinet_dir / f"{code}.json").exists():
                return code
        edinet_code_norm = str(edinet_code or "").strip().upper()
        if edinet_code_norm:
            return self._index().get(edinet_code_norm)
        return None

    def name_for(self, code: str) -> str:
        if code not in self._name_cache:
            data = load_json_optional(self.edinet_dir / f"{code}.json")
            name = data.get("name") if isinstance(data, dict) else None
            self._name_cache[code] = str(name or "")
        return self._name_cache[code]


# ---------------------------------------------------------------------------
# 入口1・2: forecasts_state.json の events.pending
# ---------------------------------------------------------------------------


def scan_pending_split_events(state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """state['events']['pending'] のうち、分割・併合イベントだけを返す。"""
    block = state.get("events") if isinstance(state, dict) else None
    pending = block.get("pending") if isinstance(block, dict) else None
    if not isinstance(pending, dict):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for event_id, raw in pending.items():
        if not isinstance(event_id, str) or not isinstance(raw, dict):
            continue
        event_type = str(raw.get("eventType") or "")
        if event_type not in SPLIT_EVENT_TYPES:
            continue
        result[event_id] = raw
    return result


def compute_age_days(first_seen_text: Any, today: date) -> int | None:
    try:
        first_seen = date.fromisoformat(str(first_seen_text)[:10])
    except ValueError:
        return None
    return (today - first_seen).days


def classify_overflow_risk(
    attempts: Any, age_days: int | None
) -> tuple[bool, str | None]:
    if isinstance(attempts, bool):
        attempts = None
    if isinstance(attempts, int) and attempts >= NEAR_ATTEMPTS_THRESHOLD:
        return True, "attempts_near_limit"
    if age_days is not None and age_days >= NEAR_AGE_DAYS_THRESHOLD:
        return True, "age_near_limit"
    return False, None


def lookup_ir_url(
    code: str,
    ir_sites: dict[str, Any],
    ir_sites_candidates: dict[str, Any],
) -> tuple[str | None, str]:
    entry = ir_sites.get(code) if isinstance(ir_sites, dict) else None
    if isinstance(entry, dict) and entry.get("irTopUrl"):
        return str(entry["irTopUrl"]), "ir_sites"
    sites = (
        ir_sites_candidates.get("sites")
        if isinstance(ir_sites_candidates, dict)
        else None
    )
    entry = sites.get(code) if isinstance(sites, dict) else None
    if isinstance(entry, dict) and entry.get("irTopUrl"):
        return str(entry["irTopUrl"]), "ir_sites_candidates"
    return None, "not_found"


def make_entry(
    code: str, name: str, trigger: str, detail: dict[str, Any], today: date
) -> dict[str, Any]:
    return {
        "code": code,
        "name": name,
        "trigger": trigger,
        "detail": detail,
        "detectedAt": today.isoformat(),
        "status": "pending",
    }


def build_pending_split_entries(
    state: dict[str, Any],
    resolver: CodeResolver,
    ir_sites: dict[str, Any],
    ir_sites_candidates: dict[str, Any],
    previous_pending: dict[str, dict[str, Any]],
    today: date,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """入口1(api_overflow)・入口2(edinetdb_event)のエントリと、次回比較用の
    トラッキング状態(pendingSplitEvents)を返す。"""
    entries: list[dict[str, Any]] = []
    current_pending = scan_pending_split_events(state)
    events_block = state.get("events") if isinstance(state, dict) else None
    seen = (
        events_block.get("seen") if isinstance(events_block, dict) else None
    )
    seen = seen if isinstance(seen, dict) else {}

    new_tracked: dict[str, dict[str, Any]] = {}

    for event_id, raw in current_pending.items():
        sec_code = raw.get("secCode")
        edinet_code = raw.get("edinetCode")
        code = resolver.resolve(sec_code, edinet_code)
        attempts = raw.get("attempts", 0)
        first_seen = raw.get("firstSeenAt")
        age_days = compute_age_days(first_seen, today)
        near_limit, reason = classify_overflow_risk(attempts, age_days)

        new_tracked[event_id] = {
            "code": code,
            "secCode": sec_code,
            "edinetCode": edinet_code,
            "eventType": raw.get("eventType"),
            "eventDate": raw.get("eventDate"),
            "attempts": attempts,
            "firstSeenAt": first_seen,
        }

        if code is None:
            # コードを解決できない銘柄（新規上場直後など）は要確認リストに
            # 積めない。トラッキングはしておき、次回以降に解決できれば拾う。
            continue

        if near_limit:
            ir_url, ir_source = lookup_ir_url(code, ir_sites, ir_sites_candidates)
            detail = {
                "eventId": event_id,
                "eventType": raw.get("eventType"),
                "eventDate": raw.get("eventDate"),
                "attempts": attempts,
                "ageDays": age_days,
                "reason": reason,
                "irUrl": ir_url,
                "irUrlSource": ir_source,
            }
            entries.append(
                make_entry(code, resolver.name_for(code), "api_overflow", detail, today)
            )
        else:
            detail = {
                "eventId": event_id,
                "eventType": raw.get("eventType"),
                "eventDate": raw.get("eventDate"),
            }
            entries.append(
                make_entry(
                    code, resolver.name_for(code), "edinetdb_event", detail, today
                )
            )

    # 脱落検知: 前回は追跡していたが、今回は pending にも seen にも居ない
    # ＝ fetch_forecasts.py 側の持ち越し上限(試行回数・経過日数)を超えて
    # 諦められた（もう自動では再試行されない）。
    for event_id, info in previous_pending.items():
        if event_id in current_pending or event_id in seen:
            continue
        code = info.get("code") or resolver.resolve(
            info.get("secCode"), info.get("edinetCode")
        )
        if code is None:
            continue
        ir_url, ir_source = lookup_ir_url(code, ir_sites, ir_sites_candidates)
        detail = {
            "eventId": event_id,
            "eventType": info.get("eventType"),
            "eventDate": info.get("eventDate"),
            "attempts": info.get("attempts"),
            "reason": "dropped_from_queue",
            "irUrl": ir_url,
            "irUrlSource": ir_source,
        }
        entries.append(
            make_entry(code, resolver.name_for(code), "api_overflow", detail, today)
        )

    return entries, new_tracked


# ---------------------------------------------------------------------------
# 入口3(安全網): fiscal_dividends.json の streakBasis
# ---------------------------------------------------------------------------


def extract_suspect_years(fiscal_dividends: dict[str, Any]) -> dict[str, list[str]]:
    """streakBasis.reliable=false の銘柄について、breakYearsを返す。

    streakBasis は edinet-direct/scripts/annotate_split_basis.py が付ける印で、
    データ提供元ではこの概念を dpsSuspectYears と呼んでいた（現行実装での
    正式なフィールド名は streakBasis.breakYears）。
    """
    result: dict[str, list[str]] = {}
    if not isinstance(fiscal_dividends, dict):
        return result
    for raw_code, record in fiscal_dividends.items():
        code = normalize_code(raw_code)
        if not code or not isinstance(record, dict):
            continue
        streak_basis = record.get("streakBasis")
        if not isinstance(streak_basis, dict):
            continue
        if streak_basis.get("reliable") is not False:
            continue
        break_years = streak_basis.get("breakYears")
        if not isinstance(break_years, list):
            continue
        years = sorted(
            {str(year) for year in break_years if isinstance(year, (int, str))}
        )
        if years:
            result[code] = years
    return result


def build_suspect_year_entries(
    current_suspect: dict[str, list[str]],
    previous_suspect: dict[str, list[str]],
    resolver: CodeResolver,
    today: date,
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for code, years in current_suspect.items():
        previous_years = set(previous_suspect.get(code, []))
        new_years = sorted(set(years) - previous_years)
        for year in new_years:
            entries.append(
                make_entry(
                    code,
                    resolver.name_for(code),
                    "new_suspect_year",
                    {"year": year},
                    today,
                )
            )
    return entries


# ---------------------------------------------------------------------------
# キュー・スナップショットの読み書き
# ---------------------------------------------------------------------------


def entry_identity(entry: dict[str, Any]) -> tuple[Any, Any, str]:
    """重複排除の識別子。(code, trigger, イベントの識別子) で見る。

    detail 自体には attempts・ageDays・irUrl など、同じイベントでも日によって
    変わりうる値が入るため、detail全体ではなく detail.eventId（無ければ
    detail.year）を安定な識別子として使う。どちらも無ければ detail 全体を
    フォールバックにする。
    """
    detail = entry.get("detail")
    detail = detail if isinstance(detail, dict) else {}
    if detail.get("eventId"):
        ident = f"eventId:{detail['eventId']}"
    elif detail.get("year"):
        ident = f"year:{detail['year']}"
    else:
        ident = json.dumps(detail, sort_keys=True, ensure_ascii=False)
    return (entry.get("code"), entry.get("trigger"), ident)


def load_queue(path: Path) -> dict[str, Any]:
    data = load_json_optional(path)
    queue = data.get("queue") if isinstance(data, dict) else None
    if not isinstance(queue, list):
        return {"queue": []}
    return {"queue": [item for item in queue if isinstance(item, dict)]}


def save_queue(path: Path, queue_doc: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as target:
        json.dump(queue_doc, target, ensure_ascii=False, indent=2)
        target.write("\n")


def load_snapshot(
    path: Path,
) -> tuple[dict[str, list[str]], dict[str, dict[str, Any]]]:
    data = load_json_optional(path)
    if not isinstance(data, dict):
        data = {}
    suspect = data.get("suspectYears")
    suspect = suspect if isinstance(suspect, dict) else {}
    pending = data.get("pendingSplitEvents")
    pending = pending if isinstance(pending, dict) else {}
    return suspect, pending


def save_snapshot(
    path: Path,
    suspect_years: dict[str, list[str]],
    pending_events: dict[str, dict[str, Any]],
    today: date,
) -> None:
    doc = {
        "updatedAt": today.isoformat(),
        "suspectYears": suspect_years,
        "pendingSplitEvents": pending_events,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as target:
        json.dump(doc, target, ensure_ascii=False, indent=2, sort_keys=True)
        target.write("\n")


# ---------------------------------------------------------------------------
# メイン
# ---------------------------------------------------------------------------


def run(args: argparse.Namespace) -> int:
    today = args.today or date.today()

    state = load_json_optional(args.state)
    if not isinstance(state, dict):
        print(
            f"forecasts_stateが見つかりません({args.state})。"
            "入口1・2(イベント由来)はスキップします。"
        )
        state = {}

    fiscal_dividends = load_json_optional(args.fiscal_dividends)
    if not isinstance(fiscal_dividends, dict):
        print(
            f"fiscal_dividends.jsonが見つかりません({args.fiscal_dividends})。"
            "入口3(安全網)はスキップします。"
        )
        fiscal_dividends = {}

    ir_sites = load_json_optional(args.ir_sites)
    ir_sites = ir_sites if isinstance(ir_sites, dict) else {}
    ir_sites_candidates = load_json_optional(args.ir_sites_candidates)
    ir_sites_candidates = (
        ir_sites_candidates if isinstance(ir_sites_candidates, dict) else {}
    )
    if not ir_sites and not ir_sites_candidates:
        print(
            f"IRサイトURLの索引が見つかりません({args.ir_sites} / "
            f"{args.ir_sites_candidates})。api_overflowのirUrlは全てnullになります。"
        )

    resolver = CodeResolver(args.edinet_dir)
    previous_suspect, previous_pending = load_snapshot(args.snapshot)

    split_entries, new_tracked_pending = build_pending_split_entries(
        state, resolver, ir_sites, ir_sites_candidates, previous_pending, today
    )
    current_suspect = extract_suspect_years(fiscal_dividends)
    suspect_entries = build_suspect_year_entries(
        current_suspect, previous_suspect, resolver, today
    )

    all_entries = split_entries + suspect_entries

    queue_doc = load_queue(args.queue)
    existing_ids = {entry_identity(item) for item in queue_doc["queue"]}
    added = 0
    for entry in all_entries:
        ident = entry_identity(entry)
        if ident in existing_ids:
            continue
        existing_ids.add(ident)
        queue_doc["queue"].append(entry)
        added += 1

    save_queue(args.queue, queue_doc)
    save_snapshot(args.snapshot, current_suspect, new_tracked_pending, today)

    by_trigger: dict[str, int] = {}
    for entry in all_entries:
        by_trigger[entry["trigger"]] = by_trigger.get(entry["trigger"], 0) + 1
    duplicate = len(all_entries) - added
    print(
        f"要確認リスト: 新規{added}件追加(検出{len(all_entries)}件・"
        f"重複{duplicate}件) / 累計{len(queue_doc['queue'])}件"
    )
    for trigger in sorted(by_trigger):
        print(f"  {trigger}: {by_trigger[trigger]}件")
    return 0


def main(argv: list[str] | None = None) -> int:
    return run(parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
