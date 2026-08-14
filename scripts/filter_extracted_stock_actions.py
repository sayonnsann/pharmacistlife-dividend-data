#!/usr/bin/env python3
"""配信側で使うEDINET株式分割イベントを、配当系列に必要なものへ絞る。

選別条件は次の条件をすべて満たすこと。

* 効力発生日が、fiscal_dividends.json の series 最終年度末より後
* newShares / oldShares が50倍未満
* tickers.json に銘柄コードが存在する
* action が ``split``
* 台帳が ``duplicateOf`` を付けたイベントではない

入力と出力に同じパスを指定できる。除外イベントは ``excluded`` に元の
イベント情報と理由コードを付けて残すため、台帳を更新した後も監査できる。
"""

from __future__ import annotations

import argparse
import json
import math
import os
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = REPOSITORY_ROOT / "data" / "stock_actions_extracted.json"
DEFAULT_OUTPUT = DEFAULT_INPUT
DEFAULT_FISCAL_DIVIDENDS = Path(
    "/Users/yusuke/workspace/edinet-direct/data/fiscal_dividends.json"
)
DEFAULT_TICKERS = REPOSITORY_ROOT / "data" / "tickers.json"
MAX_SPLIT_RATIO = 50.0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--fiscal-dividends", type=Path, default=DEFAULT_FISCAL_DIVIDENDS)
    parser.add_argument("--tickers", type=Path, default=DEFAULT_TICKERS)
    return parser.parse_args()


def load_json(path: Path, expected_type: type) -> Any:
    with path.open("r", encoding="utf-8") as source:
        value = json.load(source)
    if not isinstance(value, expected_type):
        raise ValueError(f"{path}: JSONの最上位が{expected_type.__name__}ではありません")
    return value


def finite_number(value: Any) -> float | int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value):
        return None
    return value


def normalized_code(value: Any) -> str:
    code = str(value).strip().upper()
    if len(code) != 4 or not code.isalnum() or not code.isascii():
        raise ValueError(f"不正な銘柄コードです: {value!r}")
    return code


def load_fiscal_series(path: Path) -> dict[str, tuple[dict[int, float], int | None]]:
    """build_store.load_fiscal_dividends と同じ有効系列を読み込む。"""
    document = load_json(path, dict)
    result: dict[str, tuple[dict[int, float], int | None]] = {}
    for raw_code, record in document.items():
        if not isinstance(record, dict):
            continue
        try:
            code = normalized_code(raw_code)
        except ValueError:
            continue
        raw_series = record.get("series")
        if not isinstance(raw_series, dict):
            continue
        series: dict[int, float] = {}
        for raw_year, raw_value in raw_series.items():
            try:
                year = int(raw_year)
            except (TypeError, ValueError):
                continue
            value = finite_number(raw_value)
            if value is not None and value >= 0:
                series[year] = float(value)
        if not series:
            continue

        raw_month = record.get("fiscalMonth")
        month_number = finite_number(raw_month)
        fiscal_month = (
            int(month_number)
            if month_number is not None
            and float(month_number).is_integer()
            and 1 <= month_number <= 12
            else None
        )
        result[code] = (series, fiscal_month)
    return result


def load_ticker_codes(path: Path) -> set[str]:
    records = load_json(path, list)
    result: set[str] = set()
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            raise ValueError(f"{path}: tickers[{index}]がobjectではありません")
        try:
            result.add(normalized_code(record.get("code", "")))
        except ValueError:
            continue
    return result


def fiscal_period_end(
    series: dict[int, float], fiscal_month: int | None
) -> date:
    latest_year = max(series)
    if fiscal_month is None or fiscal_month == 12:
        return date(latest_year, 12, 31)
    return date(latest_year, fiscal_month + 1, 1) - timedelta(days=1)


def split_ratio(event: dict[str, Any]) -> float | None:
    old_shares = finite_number(event.get("oldShares"))
    new_shares = finite_number(event.get("newShares"))
    if old_shares is None or new_shares is None or old_shares <= 0:
        return None
    return float(new_shares) / float(old_shares)


def exclusion_reasons(
    event: dict[str, Any],
    fiscal_by_code: dict[str, tuple[dict[int, float], int | None]],
    ticker_codes: set[str],
) -> tuple[list[str], dict[str, Any]]:
    code = str(event.get("securityCode", "")).strip().upper()
    reasons: list[str] = []
    details: dict[str, Any] = {}

    duplicate_of = event.get("duplicateOf")
    if duplicate_of:
        details["duplicateOf"] = duplicate_of
        reasons.append("duplicate_of_event")

    if event.get("action") != "split":
        reasons.append("unsupported_action")

    fiscal = fiscal_by_code.get(code)
    effective_date = None
    try:
        effective_date = date.fromisoformat(str(event.get("effectiveDate", "")))
    except ValueError:
        pass

    if fiscal is None:
        reasons.append("fiscal_series_missing")
    else:
        series, fiscal_month = fiscal
        period_end = fiscal_period_end(series, fiscal_month)
        details["fiscalSeriesEnd"] = period_end.isoformat()
        if effective_date is None or effective_date <= period_end:
            reasons.append("effective_date_not_after_fiscal_series_end")

    ratio = split_ratio(event)
    if ratio is not None:
        details["ratio"] = ratio
    if ratio is None or ratio >= MAX_SPLIT_RATIO:
        reasons.append("ratio_not_below_50")

    if code not in ticker_codes:
        reasons.append("not_listed")

    return reasons, details


def japanese_reason(reason_code: str, details: dict[str, Any]) -> str:
    if reason_code == "fiscal_series_missing":
        return "fiscal_dividends.json に有効な配当系列がない"
    if reason_code == "effective_date_not_after_fiscal_series_end":
        return (
            "効力発生日が配当系列の最終年度末以前"
            f"（最終年度末 {details.get('fiscalSeriesEnd', '不明')}）"
        )
    if reason_code == "ratio_not_below_50":
        ratio = details.get("ratio")
        return f"分割比率が50倍以上または不正（{ratio if ratio is not None else '不明'}）"
    if reason_code == "not_listed":
        return "data/tickers.json に銘柄コードが存在しない"
    if reason_code == "unsupported_action":
        return "配信側で扱うのは株式分割（action=split）のみ"
    if reason_code == "duplicate_of_event":
        return (
            "台帳で別イベントの重複候補"
            f"（duplicateOf={details.get('duplicateOf', '不明')}）"
        )
    return reason_code


def add_exclusion_metadata(
    event: dict[str, Any], reason_codes: list[str], details: dict[str, Any]
) -> dict[str, Any]:
    excluded = dict(event)
    excluded["reasonCode"] = reason_codes[0]
    excluded["reasonCodes"] = reason_codes
    excluded["reason"] = "; ".join(
        japanese_reason(reason_code, details) for reason_code in reason_codes
    )
    return excluded


LEGACY_FIELD_RENAMES = {
    "epsAdjusted": "epsAdjustedByIssuer",
    "dpsAdjusted": "applyDividendAdjustment",
}


def normalize_legacy_fields(event: dict[str, Any]) -> dict[str, Any]:
    """台帳(edinet-direct/data/stock_action_ledger.json)の旧フィールド名を、
    配信側 build_store.py が要求する新フィールド名に直す。

    build_store.py の reject_legacy_split_field_names() は旧名が残っていると
    ビルドを中断させる（旧名を別名として黙って受け付けない設計）。台帳側は
    まだ旧名 'epsAdjusted' を使っているため、ここで詰め替えないと月次の
    自動更新（tools/update_pdd_stock_actions.py）が本番ビルドを壊す。
    """
    normalized = dict(event)
    for legacy_name, new_name in LEGACY_FIELD_RENAMES.items():
        if legacy_name in normalized:
            value = normalized.pop(legacy_name)
            if new_name not in normalized:
                normalized[new_name] = value
    # build_store.py は単数形の source（{"url": ...}）を要求するが、台帳の
    # 自動抽出イベントは複数出典を持つ sources（配列）しか持たない。
    # build_store.py 側の期待に合わせて、primary種別（無ければ先頭のurl持ち）
    # から source を組み立てる。
    if not isinstance(normalized.get("source"), dict):
        sources = normalized.get("sources")
        if isinstance(sources, list):
            candidate = next(
                (
                    item
                    for item in sources
                    if isinstance(item, dict)
                    and item.get("kind") == "primary"
                    and isinstance(item.get("url"), str)
                    and item["url"].strip()
                ),
                None,
            )
            if candidate is None:
                candidate = next(
                    (
                        item
                        for item in sources
                        if isinstance(item, dict)
                        and isinstance(item.get("url"), str)
                        and item["url"].strip()
                    ),
                    None,
                )
            if candidate is not None:
                normalized["source"] = {
                    "url": candidate["url"],
                    "type": candidate.get("type", candidate.get("kind", "filing")),
                }
    return normalized


def preserve_existing_excluded(items: Any) -> list[dict[str, Any]]:
    if items is None:
        return []
    if not isinstance(items, list):
        raise ValueError("excludedがarrayではありません")
    result: list[dict[str, Any]] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValueError(f"excluded[{index}]がobjectではありません")
        preserved = normalize_legacy_fields(item)
        if "reasonCode" not in preserved:
            preserved["reasonCode"] = "issuer_mismatch"
        if "reasonCodes" not in preserved:
            preserved["reasonCodes"] = [preserved["reasonCode"]]
        result.append(preserved)
    return result


def select_events(
    events: list[dict[str, Any]],
    fiscal_by_code: dict[str, tuple[dict[int, float], int | None]],
    ticker_codes: set[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    selected: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for index, event in enumerate(events):
        if not isinstance(event, dict):
            raise ValueError(f"events[{index}]がobjectではありません")
        reasons, details = exclusion_reasons(event, fiscal_by_code, ticker_codes)
        if reasons:
            excluded.append(add_exclusion_metadata(event, reasons, details))
        else:
            selected.append(event)
    return selected, excluded


def write_json(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as target:
            json.dump(document, target, ensure_ascii=False, indent=1)
            target.write("\n")
        os.replace(temporary_path, path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def filter_document(
    document: dict[str, Any],
    fiscal_by_code: dict[str, tuple[dict[int, float], int | None]],
    ticker_codes: set[str],
) -> tuple[dict[str, Any], dict[str, int]]:
    events = document.get("events")
    if not isinstance(events, list):
        raise ValueError("eventsがarrayではありません")
    events = [
        normalize_legacy_fields(event) if isinstance(event, dict) else event
        for event in events
    ]
    existing_excluded = preserve_existing_excluded(document.get("excluded"))
    # 月次ジョブは必ず台帳を入力にする。すでに選別済みの成果物を手入力で
    # 再入力した場合は、現在のeventsをスナップショットとして保ち、excluded
    # だけを再評価する。外部のfiscal_dividends.jsonが更新されても、配信側の
    # 既存イベントが意図せず消えることを防ぐためである。
    prior_excluded_events = [
        item
        for item in existing_excluded
        if isinstance(item.get("eventId"), str)
        and item.get("action") == "split"
    ]
    generated_selection = (
        isinstance(document.get("generatedFrom"), dict)
        and isinstance(document["generatedFrom"].get("selection"), dict)
    )
    if generated_selection:
        unique_candidates = events + prior_excluded_events
        selected = list(events)
        _, newly_excluded = select_events(
            prior_excluded_events, fiscal_by_code, ticker_codes
        )
    else:
        candidates = events + prior_excluded_events
        seen_event_ids: set[str] = set()
        unique_candidates = []
        for event in candidates:
            event_id = event.get("eventId")
            if isinstance(event_id, str) and event_id in seen_event_ids:
                continue
            if isinstance(event_id, str):
                seen_event_ids.add(event_id)
            unique_candidates.append(event)
        selected, newly_excluded = select_events(
            unique_candidates, fiscal_by_code, ticker_codes
        )
    preserved_summaries = [
        item for item in existing_excluded if item not in prior_excluded_events
    ]
    output = dict(document)
    output["note"] = (
        "EDINET有報の株式分割台帳から、配当系列の最終年度末より後に効力が発生し、"
        "比率50倍未満かつJPX上場一覧に存在するイベントだけを取り込んだ表示側データ。"
        "併合は含めない。"
    )
    output["generatedAt"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    generated_from = dict(output.get("generatedFrom") or {})
    generated_from["selection"] = {
        "fiscalDividends": "fiscal_dividends.json",
        "tickers": "data/tickers.json",
        "effectiveDate": "series最終年度末より後（build_store.adjustment_for_unadjusted_seriesと同義）",
        "maxSplitRatioExclusive": MAX_SPLIT_RATIO,
        "selectedCount": len(selected),
        "newlyExcludedCount": len(newly_excluded),
    }
    output["generatedFrom"] = generated_from
    output["events"] = selected
    output["excluded"] = preserved_summaries + newly_excluded
    counts = {
        "input": len(unique_candidates),
        "selected": len(selected),
        "newlyExcluded": len(newly_excluded),
        "preservedExcluded": len(preserved_summaries),
        "excluded": len(output["excluded"]),
    }
    return output, counts


def main() -> None:
    args = parse_args()
    document = load_json(args.input, dict)
    fiscal_by_code = load_fiscal_series(args.fiscal_dividends)
    ticker_codes = load_ticker_codes(args.tickers)
    output, counts = filter_document(document, fiscal_by_code, ticker_codes)
    write_json(args.output, output)
    print(
        "株式分割イベントを選別: "
        f"入力 {counts['input']:,} / 採用 {counts['selected']:,} / "
        f"新規除外 {counts['newlyExcluded']:,} / "
        f"excluded合計 {counts['excluded']:,}"
    )


if __name__ == "__main__":
    main()
