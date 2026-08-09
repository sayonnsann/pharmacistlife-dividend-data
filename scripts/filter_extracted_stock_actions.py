#!/usr/bin/env python3
"""配信側で使うEDINET株式分割イベントを、配当系列に必要なものへ絞る。

選別条件は次の3つをすべて満たすこと。

* 効力発生日が、fiscal_dividends.json の series 最終年度末より後
* newShares / oldShares が50倍未満
* tickers.json に銘柄コードが存在する

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


def preserve_existing_excluded(items: Any) -> list[dict[str, Any]]:
    if items is None:
        return []
    if not isinstance(items, list):
        raise ValueError("excludedがarrayではありません")
    result: list[dict[str, Any]] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValueError(f"excluded[{index}]がobjectではありません")
        preserved = dict(item)
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
    existing_excluded = preserve_existing_excluded(document.get("excluded"))
    # 前回の実行でexcludedへ移した元イベントも再評価する。これにより、
    # fiscal_dividends.jsonやtickers.jsonが更新された後に、現成果物を入力として
    # 再実行しても、条件を満たすイベントを復活できる。
    prior_excluded_events = [
        item
        for item in existing_excluded
        if isinstance(item.get("eventId"), str)
        and item.get("action") == "split"
    ]
    candidates = events + prior_excluded_events
    seen_event_ids: set[str] = set()
    unique_candidates: list[dict[str, Any]] = []
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
