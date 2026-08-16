#!/usr/bin/env python3
"""配当チェッカー用の非公開SQLiteストアを実データから構築する。"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import sqlite3
import subprocess
import tempfile
from collections.abc import Sequence
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FINANCIALS = REPOSITORY_ROOT / "data" / "all_financials.json"
DEFAULT_SECTORS = REPOSITORY_ROOT / "data" / "sector_stats.json"
DEFAULT_TICKERS = REPOSITORY_ROOT / "data" / "tickers.json"
DEFAULT_FISCAL_DIVIDENDS = REPOSITORY_ROOT / "data" / "fiscal_dividends.json"
DEFAULT_CALENDAR_DIVIDENDS = (
    REPOSITORY_ROOT / "data" / "calendar_dividends_frozen.json"
)
DEFAULT_STOCK_ACTIONS = REPOSITORY_ROOT / "data" / "stock_actions_manual.json"
DEFAULT_EXTRACTED_STOCK_ACTIONS = (
    REPOSITORY_ROOT / "data" / "stock_actions_extracted.json"
)
DEFAULT_FORECASTS = REPOSITORY_ROOT / "forecasts_state.json"
DEFAULT_OUTPUT = REPOSITORY_ROOT / "stocks.sqlite"
DAILY_PRICE_CSV_URL = (
    "https://cdn.jsdelivr.net/gh/sayonnsann/"
    "kouhaitou-db@main/data/database.csv"
)
# 株価だけを1日2回反映する軽量ワークフロー用。jsDelivrの@main指定は最大12時間
# キャッシュされるため、purgeに頼らず raw.githubusercontent.com（数分程度の
# 短いキャッシュ）から直接読む。列構成・フォーマットはjsDelivr版と同一。
DAILY_PRICE_CSV_URL_NO_CACHE = (
    "https://raw.githubusercontent.com/sayonnsann/"
    "kouhaitou-db/main/data/database.csv"
)
JST = ZoneInfo("Asia/Tokyo")
# kouhaitou-db側のCSVヘッダ（最終更新日時）がこれより古い場合、鮮度警告を出す。
# 更新ジョブの失敗やjsDelivr/raw.githubusercontentのキャッシュ滞留に人が気づける
# ようにするための健全性チェックで、ビルド自体は止めない。
PRICE_FRESHNESS_WARN_AFTER = timedelta(hours=3)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--financials", type=Path, default=DEFAULT_FINANCIALS)
    parser.add_argument("--sectors", type=Path, default=DEFAULT_SECTORS)
    parser.add_argument("--tickers", type=Path, default=DEFAULT_TICKERS)
    parser.add_argument(
        "--fiscal-dividends", type=Path, default=DEFAULT_FISCAL_DIVIDENDS
    )
    parser.add_argument(
        "--calendar-dividends",
        type=Path,
        default=DEFAULT_CALENDAR_DIVIDENDS,
        help=(
            "事業年度の系列を作れなかった銘柄の、暦年ベースの凍結スナップショット。"
            "無ければその銘柄の配当グラフだけが空になる（ビルドは通る）。"
        ),
    )
    parser.add_argument(
        "--stock-actions", type=Path, default=DEFAULT_STOCK_ACTIONS
    )
    parser.add_argument(
        "--stock-actions-extracted",
        type=Path,
        default=DEFAULT_EXTRACTED_STOCK_ACTIONS,
        help="監査合格分の自動取り込み台帳（manual側を優先して統合）",
    )
    parser.add_argument("--forecasts", type=Path, default=DEFAULT_FORECASTS)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--prices-url",
        default=DAILY_PRICE_CSV_URL,
        help="日次株価CSV URL（file://を含むcurl対応URLも可）",
    )
    parser.add_argument(
        "--prices-no-cache",
        action="store_true",
        help=(
            "株価CSVをjsDelivr(@main、最大12時間キャッシュ)ではなく"
            "raw.githubusercontent.com(数分程度のキャッシュ)から取得する。"
            "株価だけを1日複数回反映する軽量ワークフロー用。"
            "--prices-url を明示指定した場合はそちらを優先する。"
        ),
    )
    args = parser.parse_args()
    if args.prices_no_cache and args.prices_url == DAILY_PRICE_CSV_URL:
        args.prices_url = DAILY_PRICE_CSV_URL_NO_CACHE
    return args


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


def stock_action_paths(
    path: Path | Sequence[Path],
) -> list[Path]:
    """株式アクション台帳のパスを、優先順位順のリストにする。"""
    if isinstance(path, Path):
        paths = [path]
    else:
        paths = list(path)
    if not paths or not all(isinstance(item, Path) for item in paths):
        raise ValueError("株式アクション台帳のパスが空、またはPathではありません")
    return paths


def stock_action_source_label(
    path: Path | Sequence[Path] | None,
) -> str:
    if path is None:
        return ""
    return ",".join(str(item) for item in stock_action_paths(path))


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


def roe_year_end(financial: dict[str, Any]) -> float | None:
    """期末自己資本ベースのROE（最新の共通年度）を計算する。

    既存の ``roe`` は会社申告値（自己資本は期首・期末平均）なので、
    期末時点の比較用に、総資産×自己資本比率で自己資本を近似する。
    3系列の年度が一致する値だけを使い、自己資本が0になる年度は除外する。
    """
    assets = financial.get("totalAssets")
    equity_ratio = financial.get("equityRatio")
    net_income = financial.get("netIncome")
    if not all(isinstance(series, dict) for series in (assets, equity_ratio, net_income)):
        return None

    common_years = set(assets) & set(equity_ratio) & set(net_income)
    for year in sorted(common_years, key=lambda value: str(value), reverse=True):
        total_assets = finite_number(assets[year])
        ratio = finite_number(equity_ratio[year])
        income = finite_number(net_income[year])
        if total_assets is None or ratio is None or income is None:
            continue
        equity = float(total_assets) * float(ratio) / 100
        if equity == 0:
            continue
        return round(float(income) / equity * 100, 2)
    return None


def load_fiscal_dividends(path: Path) -> dict[str, dict[str, Any]]:
    """事業年度ベースの配当系列（edinet-direct/data/fiscal_dividends.json）を読む。

    暦年の系列（yfinance）では、3月決算の会社で「前期の期末配当＋当期の中間配当」が
    同じ暦年に入るため、実在しない横ばいが生まれて連続増配が途切れる
    （例: KDDIが連続増配1年と表示されていた）。事業年度で区切った系列に置き換える。

    値が空・壊れている銘柄は黙って落とし、その銘柄は暦年の系列のまま残す。

    このファイルはリポジトリに入れない。半分近くの年が haitoukin-checker 由来で、
    利用の許可はもらっているが再配布の許可ではないため。このリポジトリは
    jsDelivr 配信のため Public で、置くと誰でもダウンロードできてしまう。
    置き場所は ConoHa の非公開 data ディレクトリで、日次ワークフローが
    FTPS で取ってきて SQLite に入れる。詳しくは README の
    「事業年度の配当系列」を参照。
    """
    if not path.exists():
        raise FileNotFoundError(
            f"事業年度の配当系列が見つかりません: {path}\n"
            "このファイルはリポジトリに含めない運用です。"
            "ConoHaの非公開dataディレクトリから取得するか、"
            "edinet-direct/data/fiscal_dividends.json をコピーしてください。"
        )
    document = load_json(path, dict)

    result: dict[str, dict[str, Any]] = {}
    for raw_code, record in document.items():
        if not isinstance(record, dict):
            raise ValueError(f"{path}: {raw_code} がobjectではありません")
        try:
            code = normalized_code(raw_code)
        except ValueError:
            continue

        raw_series = record.get("series")
        if not isinstance(raw_series, dict) or not raw_series:
            continue
        series: dict[int, float] = {}
        for raw_year, raw_value in raw_series.items():
            try:
                year = int(raw_year)
            except (TypeError, ValueError):
                continue
            value = finite_number(raw_value)
            if value is None or value < 0:
                continue
            series[year] = float(value)
        if not series:
            continue

        connection = record.get("connection")
        connection = connection if isinstance(connection, dict) else {}
        external_years = record.get("externalYears")
        external_years = [
            int(year)
            for year in external_years
            if isinstance(year, int) or (isinstance(year, str) and year.isdigit())
        ] if isinstance(external_years, list) else []

        # 株式分割の基準がそろっていない年があり、連続増配年数を数えられない銘柄。
        # edinet-direct/scripts/annotate_split_basis.py が付ける印。
        # 印が無い（古い版のファイル）場合は、判定できるものとして扱う。
        streak_basis = record.get("streakBasis")
        streak_basis = streak_basis if isinstance(streak_basis, dict) else {}
        streak_reliable = streak_basis.get("reliable") is not False
        break_years = streak_basis.get("breakYears")
        break_years = [
            int(year) for year in break_years if isinstance(year, int)
        ] if isinstance(break_years, list) else []

        result[code] = {
            "series": series,
            "fiscalMonth": finite_number(record.get("fiscalMonth")),
            "connectionStatus": connection.get("status"),
            "connectionReason": connection.get("reason"),
            "externalSource": record.get("externalSource"),
            "externalYears": sorted(set(external_years) & set(series)),
            "streakReliable": streak_reliable,
            "streakUnreliableReason": (
                None if streak_reliable else streak_basis.get("reason")
            ),
            "streakUnreliableNote": (
                None if streak_reliable else streak_basis.get("note")
            ),
            "streakBreakYears": [] if streak_reliable else sorted(break_years),
        }
    return result


def load_calendar_dividends(path: Path | None) -> dict[str, dict[str, Any]]:
    """暦年ベースの配当系列の凍結スナップショットを読む。

    EDINETにも haitoukin-checker にも配当の記載が無く、事業年度の系列を
    作れなかった銘柄だけが入っている（2026-08時点で14銘柄）。中身は
    Yahoo(yfinance)由来のため再配布できず、Publicのこのリポジトリには置かない。
    置き場所は ConoHa の非公開 data ディレクトリ。凍結なので更新はしない。

    ファイルが無くてもビルドは通す。無い場合、この14銘柄の配当グラフだけが
    「配当データがありません」になる（他の3,794銘柄は事業年度の系列で出る）。
    """
    if path is None or not path.exists():
        print(f"暦年の凍結スナップショットなし（該当銘柄の配当グラフは空）: {path}")
        return {}
    document = load_json(path, dict)
    stocks = document.get("stocks")
    if not isinstance(stocks, dict):
        raise ValueError(f"{path}: stocksがobjectではありません")

    result: dict[str, dict[str, Any]] = {}
    for raw_code, record in stocks.items():
        if not isinstance(record, dict):
            raise ValueError(f"{path}: {raw_code} がobjectではありません")
        try:
            code = normalized_code(raw_code)
        except ValueError:
            continue
        series: dict[int, float] = {}
        for raw_year, raw_value in (record.get("annual") or {}).items():
            try:
                year = int(raw_year)
            except (TypeError, ValueError):
                continue
            value = finite_number(raw_value)
            if value is None or value < 0:
                continue
            series[year] = float(value)
        if not series:
            continue
        result[code] = {"name": record.get("name"), "series": series}
    print(f"暦年の凍結スナップショット: {len(result)}銘柄（{path}）")
    return result


def load_tickers(path: Path) -> dict[str, dict[str, Any]]:
    """JPX由来の銘柄マスタ（名称・市場・業種）を読む。

    以前はこの3項目も dividends.json から取っていたが、あちらはYahoo由来で
    再配布できない。tickers.json は同じ値をJPXの公式一覧から作っていて、
    このリポジトリに入れてよいので、そちらへ寄せた。
    """
    records = load_json(path, list)
    result, _ = index_by_code(records, "tickers", skip_nonstandard=True)
    return result


def _streak_from_series(
    series: dict[int, float], years: list[int], *, allow_equal: bool
) -> tuple[int, bool]:
    """最新年から遡って連続増配（または連続非減配）の年数と、天井かどうかを返す。

    天井 = 系列の最も古い年まで途切れずに続いている状態。それより前は
    データが無くて確認できないので、画面では「N年以上」と出す必要がある。

    途中で止める条件:
      - 年が飛んでいる（欠測年をまたいで比較すると連続性を確認できない）
      - どちらかの年が無配（0円）。減配で0になった年も、無配から復配した年も、
        「連続して増配してきた」とは言えないので、そこで区切る。
        暦年の系列（yfinance）は無配年のデータ自体を持たないので、
        この扱いにすると従来の数え方と結果が揃う。
    """
    count = 0
    index = len(years) - 1
    while index > 0:
        current_year, previous_year = years[index], years[index - 1]
        if current_year - previous_year != 1:
            break
        current, previous = series[current_year], series[previous_year]
        if current <= 0 or previous <= 0:
            break
        if not (current >= previous if allow_equal else current > previous):
            break
        count += 1
        index -= 1
    return count, count > 0 and index == 0


def fiscal_dividend_stats(
    series: dict[int, float],
    *,
    streak_reliable: bool = True,
    break_years: list[int] | tuple[int, ...] = (),
) -> dict[str, Any]:
    """事業年度の配当系列から連続増配年数と平均増配率を出す。

    streak_reliable=False は、株式分割の基準がそろっていない年があって
    連続増配を数えられない銘柄（edinet-direct が印を付ける）。この場合は
    「0年」ではなくNULLにする。0年で出すと、実際には増配が続いている会社を
    ランキングの最下位に並べてしまい、事実と反対の印象を与えるため。

    平均増配率も、基準がまたがる区間は同じ理由で使えない。たとえば
    イエローハット(9882)は2025年度100円→2026年度62円と並ぶので、
    3年増配率が0%になってしまう（実際は分割後の基準で62円＝124円相当）。
    基準の切れ目をまたぐ区間だけNULLにし、またがない区間は残す。
    """
    years = sorted(series)
    streak_increase, increase_capped = _streak_from_series(
        series, years, allow_equal=False
    )
    streak_non_decrease, non_decrease_capped = _streak_from_series(
        series, years, allow_equal=True
    )

    def cagr(span: int) -> float | None:
        # 「n年前の事業年度」と比べる。年が飛んでいる銘柄で
        # 実際には5年離れていない値を5年増配率と呼ばないため、
        # 件数ではなく年で数える。
        if not years:
            return None
        latest_year = years[-1]
        base_year = latest_year - span
        if base_year not in series:
            return None
        if not streak_reliable and _crosses_break(
            base_year, latest_year, break_years
        ):
            return None
        first, last = series[base_year], series[latest_year]
        if first <= 0 or last <= 0:
            return None
        return round(((last / first) ** (1 / span) - 1) * 100, 2)

    if not streak_reliable:
        streak_increase = None
        streak_non_decrease = None
        increase_capped = False
        non_decrease_capped = False

    return {
        "streakIncrease": streak_increase,
        "streakIncreaseCapped": increase_capped,
        "streakNonDecrease": streak_non_decrease,
        "streakNonDecreaseCapped": non_decrease_capped,
        "cagr3": cagr(3),
        "cagr5": cagr(5),
        "cagr10": cagr(10),
    }


def base_dividend_series(
    series: dict[int, float], breakdown: dict[str, Any]
) -> dict[int, float]:
    """記念・特別配当の内訳がある年度を普通配当額へ置き換える。"""
    result = dict(series)
    for raw_year, detail in breakdown.items():
        if not isinstance(detail, dict):
            continue
        try:
            year = int(raw_year)
        except (TypeError, ValueError):
            continue
        if year not in result:
            continue
        base = finite_number(detail.get("base"))
        if base is None or base < 0:
            continue
        result[year] = float(base)
    return result


def streak_base_from_breakdown(
    series: dict[int, float], breakdown: dict[str, Any]
) -> int | None:
    """普通配当だけの系列で、連続増配年数を再計算する。

    内訳が存在する銘柄だけの補助指標なので、株式分割の基準ズレで総額系列を
    ランキングから外している銘柄でも、内訳から作れる直近の普通配当系列は
    別指標として保持する。分割でさらに不確かな場合は、元系列の値自体が
    変わらないため、既存の ``streakIncrease`` とは別に扱う。
    """
    if not series or not isinstance(breakdown, dict):
        return None
    base_series = base_dividend_series(series, breakdown)
    return fiscal_dividend_stats(base_series)["streakIncrease"]


def streak_no_decrease_base_from_breakdown(
    series: dict[int, float], breakdown: dict[str, Any]
) -> int | None:
    """普通配当だけの系列で、連続非減配年数（実質累進配当）を再計算する。

    ``streak_base_from_breakdown``（実質連続増配）と同じ考え方・同じbase系列を
    使い、増配ではなく非減配を数える版。内訳が無い銘柄はNoneのまま
    （画面側は全額ベースの streakNonDecrease と同値扱いにしてカードを隠す）。
    """
    if not series or not isinstance(breakdown, dict):
        return None
    base_series = base_dividend_series(series, breakdown)
    return fiscal_dividend_stats(base_series)["streakNonDecrease"]


def _crosses_break(
    base_year: int, latest_year: int, break_years: list[int] | tuple[int, ...]
) -> bool:
    """base_year〜latest_year の区間が、基準の切れ目をまたぐか。

    切れ目の年が分からない場合（印はあるが年が無い）は、
    どの区間が安全か判断できないのでまたぐものとして扱う。
    """
    if not break_years:
        return True
    ordered = sorted(break_years)
    return base_year <= ordered[0] and ordered[-1] <= latest_year


def split_fallback_record(
    event: dict[str, Any], field: str, reason: str
) -> dict[str, str]:
    return {
        "eventId": str(event.get("eventId", "<unknown>")),
        "securityCode": str(event.get("securityCode", "<unknown>")),
        "field": field,
        "reason": reason,
    }


def register_split_fallback(
    fallback_events: list[dict[str, Any]] | None,
    record: dict[str, Any],
    *,
    log: bool,
) -> None:
    already_registered = (
        fallback_events is not None and record in fallback_events
    )
    if fallback_events is not None and not already_registered:
        fallback_events.append(record)
    if log and not already_registered:
        print(
            "株式分割補正フォールバック: "
            f"{record['eventId']} ({record['field']}) - {record['reason']}"
        )


def reject_legacy_split_field_names(
    event: dict[str, Any], context: str
) -> None:
    legacy_names = {
        "".join(("eps", "Adjusted")): "epsAdjustedByIssuer",
        "".join(("dps", "Adjusted")): "applyDividendAdjustment",
    }
    for legacy_name, replacement in legacy_names.items():
        if legacy_name in event:
            raise ValueError(
                f"{context}に旧フィールド名 {legacy_name!r} が含まれています。"
                f"新しいフィールド名 {replacement!r} に直してください。"
                "旧名を別名としては受け付けません。"
            )


def load_stock_actions(
    path: Path | Sequence[Path],
    *,
    as_of: date | None = None,
    fallback_events: list[dict[str, Any]] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """有効日を迎えた、適用可能な株式分割を銘柄コード別に返す。

    provisional は分割の事実・比率・効力発生日を先に反映する段階で、
    配当だけを補正する。EPS/BPSへの適用は confirmed になるまで行わない。
    """
    effective_as_of = as_of or datetime.now(JST).date()
    result: dict[str, list[dict[str, Any]]] = {}
    event_ids: set[str] = set()
    for source_path in stock_action_paths(path):
        document = load_json(source_path, dict)
        events = document.get("events")
        if not isinstance(events, list):
            raise ValueError(f"{source_path}: eventsがarrayではありません")
        source_event_ids: set[str] = set()
        for index, event in enumerate(events):
            label = f"{source_path}: events[{index}]"
            if not isinstance(event, dict):
                raise ValueError(f"{label}がobjectではありません")

            event_id = event.get("eventId")
            if not isinstance(event_id, str) or not event_id.strip():
                raise ValueError(f"{label}.eventIdが空です")
            if event_id in source_event_ids:
                raise ValueError(
                    f"{source_path}: eventId {event_id!r} が重複しています"
                )
            source_event_ids.add(event_id)
            # manualを先に渡すことで、同じeventIdの自動抽出値を捨てる。
            if event_id in event_ids:
                continue
            event_ids.add(event_id)

            if event.get("action") != "split":
                raise ValueError(f"{label}.actionがsplitではありません")
            reject_legacy_split_field_names(event, label)
            if event.get("status") not in ("confirmed", "provisional"):
                continue

            event = dict(event)
            event_fallbacks: list[dict[str, Any]] = []

            def mark_fallback(field: str, reason: str) -> None:
                if any(item["field"] == field for item in event_fallbacks):
                    return
                record = split_fallback_record(event, field, reason)
                event_fallbacks.append(record)
                register_split_fallback(fallback_events, record, log=True)

            eps_adjusted_by_issuer = event.get("epsAdjustedByIssuer")
            if "epsAdjustedByIssuer" not in event or (
                eps_adjusted_by_issuer is not None
                and not isinstance(eps_adjusted_by_issuer, bool)
            ):
                event["epsAdjustedByIssuer"] = None
                mark_fallback(
                    "epsAdjustedByIssuer",
                    "epsAdjustedByIssuerがtrue/false/nullではないためEPS/BPSを補正しません",
                )
                eps_adjusted_by_issuer = None
            elif (
                event.get("status") == "confirmed"
                and eps_adjusted_by_issuer is None
            ):
                mark_fallback(
                    "epsAdjustedByIssuer",
                    "confirmedなのにepsAdjustedByIssuerがnullのためEPS/BPSを補正しません",
                )

            apply_dividend_adjustment = event.get("applyDividendAdjustment", True)
            if apply_dividend_adjustment is not None and not isinstance(
                apply_dividend_adjustment, bool
            ):
                event["applyDividendAdjustment"] = None
                mark_fallback(
                    "applyDividendAdjustment",
                    "applyDividendAdjustmentがtrue/false/nullではないため配当を補正しません",
                )
                apply_dividend_adjustment = None
            elif apply_dividend_adjustment is None:
                mark_fallback(
                    "applyDividendAdjustmentがnullのため配当を補正しません",
                )

            code = normalized_code(event.get("securityCode", ""))
            try:
                effective_date = date.fromisoformat(str(event.get("effectiveDate", "")))
            except ValueError as error:
                raise ValueError(f"{label}.effectiveDateがISO日付ではありません") from error

            old_shares = finite_number(event.get("oldShares"))
            new_shares = finite_number(event.get("newShares"))
            if old_shares is None or old_shares <= 0:
                event["oldShares"] = None
                old_shares = None
            if new_shares is None or new_shares <= 0:
                event["newShares"] = None
                new_shares = None
            if old_shares is None or new_shares is None:
                if apply_dividend_adjustment is True:
                    event["applyDividendAdjustment"] = None
                    mark_fallback(
                        "applyDividendAdjustment",
                        "分割比率が不明のため配当を補正しません",
                    )
                if (
                    event.get("status") == "confirmed"
                    and eps_adjusted_by_issuer is False
                ):
                    mark_fallback(
                        "epsAdjustedByIssuer",
                        "分割比率が不明のためEPS/BPSを補正しません",
                    )
            source = event.get("source")
            if not isinstance(source, dict):
                raise ValueError(f"{label}.sourceがobjectではありません")
            source_url = source.get("url")
            if not isinstance(source_url, str) or not source_url.strip():
                raise ValueError(f"{label}.source.urlが空です")

            # 将来の分割は、現在株価がまだ旧株式数基準なので適用しない。
            if effective_date > effective_as_of:
                continue
            if event_fallbacks:
                event["_splitAdjustmentFallbacks"] = event_fallbacks
            result.setdefault(code, []).append(event)

    for code_events in result.values():
        code_events.sort(key=lambda event: (event["effectiveDate"], event["eventId"]))
    return result


def split_adjustment(
    events: list[dict[str, Any]],
    *,
    fallback_events: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """複数イベントの補正係数と、画面注記用の根拠を組み立てる。

    配当は confirmed / provisional の両方で補正する。EPS/BPSは、会社の
    遡及調整の有無を決算書類で確認できた confirmed イベントだけを対象にする。
    """
    for event in events:
        reject_legacy_split_field_names(
            event, f"株式分割イベント {event.get('eventId', '<unknown>')}"
        )
    events = [
        event
        for event in events
        if event.get("status") in ("confirmed", "provisional")
    ]
    if not events:
        return None

    dividend_factor = 1.0
    eps_bps_factor = 1.0
    payload_events = []
    adjustment_fallbacks: list[dict[str, Any]] = []
    for event in events:
        status = event["status"]
        event_fallbacks: list[dict[str, Any]] = []
        for record in event.get("_splitAdjustmentFallbacks", []):
            if record not in event_fallbacks:
                event_fallbacks.append(record)
            if record not in adjustment_fallbacks:
                adjustment_fallbacks.append(record)
            register_split_fallback(fallback_events, record, log=False)

        def mark_fallback(field: str, reason: str) -> None:
            if any(item["field"] == field for item in event_fallbacks):
                return
            record = split_fallback_record(event, field, reason)
            event_fallbacks.append(record)
            adjustment_fallbacks.append(record)
            register_split_fallback(fallback_events, record, log=True)

        eps_adjusted_by_issuer = event.get("epsAdjustedByIssuer")
        if "epsAdjustedByIssuer" not in event:
            eps_adjusted_by_issuer = None
            mark_fallback(
                "epsAdjustedByIssuer",
                "epsAdjustedByIssuerがないためEPS/BPSを補正しません",
            )
        if eps_adjusted_by_issuer is not None and not isinstance(
            eps_adjusted_by_issuer, bool
        ):
            eps_adjusted_by_issuer = None
            mark_fallback(
                "epsAdjustedByIssuer",
                "epsAdjustedByIssuerがtrue/false/nullではないためEPS/BPSを補正しません",
            )
        if status == "confirmed" and eps_adjusted_by_issuer is None:
            mark_fallback(
                "epsAdjustedByIssuer",
                "confirmedなのにepsAdjustedByIssuerがnullのためEPS/BPSを補正しません",
            )
        apply_dividend_adjustment = event.get(
            "applyDividendAdjustment", True
        )
        if apply_dividend_adjustment is not None and not isinstance(
            apply_dividend_adjustment, bool
        ):
            apply_dividend_adjustment = None
            mark_fallback(
                "applyDividendAdjustment",
                "applyDividendAdjustmentがtrue/false/nullではないため配当を補正しません",
            )
        if apply_dividend_adjustment is None:
            mark_fallback(
                "applyDividendAdjustment",
                "applyDividendAdjustmentがnullのため配当を補正しません",
            )
        old_shares = finite_number(event.get("oldShares"))
        new_shares = finite_number(event.get("newShares"))
        factor = (
            old_shares / new_shares
            if old_shares is not None
            and old_shares > 0
            and new_shares is not None
            and new_shares > 0
            else None
        )
        if factor is None:
            if apply_dividend_adjustment is True:
                apply_dividend_adjustment = None
                mark_fallback(
                    "applyDividendAdjustment",
                    "分割比率が不明のため配当を補正しません",
                )
            if status == "confirmed" and eps_adjusted_by_issuer is False:
                mark_fallback(
                    "epsAdjustedByIssuer",
                    "分割比率が不明のためEPS/BPSを補正しません",
                )
        if apply_dividend_adjustment is True and factor is not None:
            dividend_factor *= factor
        if (
            status == "confirmed"
            and eps_adjusted_by_issuer is False
            and factor is not None
        ):
            eps_bps_factor *= factor
        source = event["source"]
        audit = source.get("audit")
        if not isinstance(audit, dict):
            audit = {}
        payload_events.append(
            {
                "eventId": event["eventId"],
                "effectiveDate": event["effectiveDate"],
                "oldShares": event.get("oldShares"),
                "newShares": event.get("newShares"),
                "adjustmentFactor": factor,
                "status": status,
                "applyDividendAdjustment": apply_dividend_adjustment,
                "epsAdjustedByIssuer": eps_adjusted_by_issuer,
                "sourceUrl": source["url"],
                "sourceType": source.get("type"),
                "sourceDocID": source.get("docID"),
                "sourceDocIDs": source.get("docIDs"),
                "auditDecision": audit.get("decision"),
                "auditReasons": audit.get("reasons", []),
                "auditReasonLabels": audit.get("reasonLabels", []),
            }
        )
    return {
        "dividendFactor": dividend_factor,
        "epsBpsFactor": eps_bps_factor,
        "hasProvisional": any(
            event["status"] == "provisional" for event in events
        ),
        "fallbacks": adjustment_fallbacks,
        "events": payload_events,
    }


def adjustment_factor_for_period(
    adjustment: dict[str, Any],
    period: Any,
    *,
    fiscal_month: int | None,
    field: str,
) -> float:
    """期間末より後に効力が発生した分割だけの補正係数を返す。"""
    try:
        period_year = int(period)
    except (TypeError, ValueError):
        return 1.0

    factor = 1.0
    for event in adjustment.get("events", []):
        event_factor = finite_number(event.get("adjustmentFactor"))
        if event_factor is None:
            continue
        try:
            effective_date = date.fromisoformat(str(event["effectiveDate"]))
        except (KeyError, TypeError, ValueError):
            continue
        if fiscal_month is None:
            # 事業年度末が分からない系列は、イベント年の値もイベント前の
            # 基準で保存されている安全側として扱う。
            applies = effective_date.year >= period_year
        else:
            if not 1 <= fiscal_month <= 12:
                return 1.0
            if fiscal_month == 12:
                period_end = date(period_year, 12, 31)
            else:
                next_month = date(period_year, fiscal_month + 1, 1)
                period_end = next_month - timedelta(days=1)
            applies = effective_date > period_end
        if not applies:
            continue
        if field == "dividend" and event.get("applyDividendAdjustment") is True:
            factor *= event_factor
        elif (
            field == "epsBps"
            and event.get("status") == "confirmed"
            and event.get("epsAdjustedByIssuer") is False
        ):
            factor *= event_factor
    return factor


def adjustment_for_unadjusted_series(
    adjustment: dict[str, Any],
    series: dict[Any, Any],
    *,
    fiscal_month: int | None,
) -> dict[str, Any]:
    """既に過年度の分割を反映済みの系列へ、未反映イベントだけを渡す。"""
    if not series:
        return adjustment
    years = []
    for period in series:
        try:
            years.append(int(period))
        except (TypeError, ValueError):
            continue
    if not years:
        return adjustment
    latest_year = max(years)
    if fiscal_month is None or not 1 <= fiscal_month <= 12:
        latest_period_end = date(latest_year, 12, 31)
    elif fiscal_month == 12:
        latest_period_end = date(latest_year, 12, 31)
    else:
        latest_period_end = (
            date(latest_year, fiscal_month + 1, 1) - timedelta(days=1)
        )
    events = []
    for event in adjustment.get("events", []):
        try:
            effective_date = date.fromisoformat(str(event["effectiveDate"]))
        except (KeyError, TypeError, ValueError):
            continue
        if effective_date > latest_period_end:
            events.append(event)
    return {**adjustment, "events": events}


def adjust_per_share_series(
    series: Any,
    adjustment: dict[str, Any],
    *,
    fiscal_month: int | None = None,
    field: str = "dividend",
) -> Any:
    """期間末より後の分割だけを時系列へ適用する。"""
    if not isinstance(series, dict):
        return series
    return {
        period: (
            float(value)
            * adjustment_factor_for_period(
                adjustment,
                period,
                fiscal_month=fiscal_month,
                field=field,
            )
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


def payout_series(financial: dict[str, Any]) -> dict[str, Any]:
    """配当性向の系列（EDINET由来・事業年度キー）を返す。

    以前はグラフの折れ線に dividends.json の暦年ベースの値を使っていたが、
    棒グラフを事業年度に切り替えたので軸が合わなくなった。EDINET由来の
    payoutRatioTotalBased は事業年度キーなので、棒と同じ年の上に点が乗る。
    年数も暦年版の中央値3年に対して10年前後と長い。
    """
    for key in ("payoutRatioTotalBased", "payoutRatioConsolidated"):
        series = financial.get(key)
        if not isinstance(series, dict):
            continue
        # 利益がほぼゼロの年は配当性向が数千%になる。折れ線の縦軸は
        # 最大値に合わせて伸びるので、1年の外れ値で他の年が全部つぶれる。
        # ランキング用の payout 列と同じ 0〜1000% の範囲だけ描く。
        bounded_series = {
            str(year): value
            for year, value in series.items()
            if bounded(value, 0, 1000) is not None
        }
        if bounded_series:
            return bounded_series
    return {}


def payout_value(financial: dict[str, Any]) -> float | int | None:
    for key in ("payoutRatioTotalBased", "payoutRatioConsolidated"):
        value = latest_number(financial.get(key))
        if value is not None:
            return value
    return None


def forecast_fiscal_year(record: dict[str, Any]) -> int | None:
    """会社発表の予想が「どの事業年度のものか」を年で返す。

    fetch_forecasts.py が forecastFiscalYear を入れてくれていればそれを使う。
    無い場合（この項目を入れる前に取得したstate）は、表示用に組み立ててある
    forecastPeriod（例: 「2027年3月期(予)」「FY2027」）から年を拾う。

    事業年度の配当系列のキーは決算期末の暦年（2027年3月期＝2027）なので、
    ここで返す年をそのまま棒グラフの年として使える。
    """
    explicit = record.get("forecastFiscalYear")
    if isinstance(explicit, int) and not isinstance(explicit, bool):
        return explicit
    period = record.get("forecastPeriod")
    if not isinstance(period, str):
        return None
    match = re.search(r"(\d{4})\s*年", period) or re.search(
        r"FY\s*(\d{4})", period, re.IGNORECASE
    )
    return int(match.group(1)) if match else None


EARNINGS_FORECAST_FIELDS = (
    "forecastRevenue",
    "forecastRevenueChange",
    "forecastOperatingIncome",
    "forecastOperatingIncomeChange",
    "forecastOrdinaryIncome",
    "forecastOrdinaryIncomeChange",
    "forecastNetIncome",
    "forecastNetIncomeChange",
    "forecastEps",
    "forecastEpsChange",
)


def earnings_series_value(
    financial: dict[str, Any], field: str, fiscal_year: int | None
) -> float | int | None:
    """有報の年度系列から、予想と同じ事業年度の実績を1件取り出す。"""
    if fiscal_year is None:
        return None
    series = financial.get(field)
    if not isinstance(series, dict):
        return None
    raw = series.get(str(fiscal_year), series.get(fiscal_year))
    return finite_number(raw)


def actual_period_label(period: Any) -> str | None:
    if not isinstance(period, str) or not period.strip():
        return None
    return re.sub(r"\s*\(予\)\s*$", "", period.strip())


def earnings_payload(
    financial: dict[str, Any], forecast_record: Any
) -> dict[str, Any] | None:
    """業績予想と、同じ年度の有報実績を置き換え可能な形に揃える。

    ``forecast`` と ``actual`` に同じ period/fiscalYear を持たせ、actual が
    有報系列に現れた場合だけ値を入れる。表示側は actual があれば通常色、
    無ければ kind=forecast を灰色で表示できる。
    """
    if not isinstance(forecast_record, dict):
        return None

    period = forecast_record.get("forecastPeriod")
    fiscal_year = forecast_fiscal_year(forecast_record)
    quarter = forecast_record.get("forecastQuarter")
    if isinstance(quarter, bool) or not isinstance(quarter, int):
        quarter = None
    period_type = forecast_record.get("forecastPeriodType")
    if period_type not in ("current", "next"):
        period_type = (
            "next"
            if quarter == 4
            else "current"
            if quarter in (1, 2, 3)
            else None
        )

    metrics: dict[str, dict[str, float | int | None]] = {}
    metric_pairs = (
        ("revenue", "forecastRevenue", "forecastRevenueChange"),
        (
            "operatingIncome",
            "forecastOperatingIncome",
            "forecastOperatingIncomeChange",
        ),
        (
            "ordinaryIncome",
            "forecastOrdinaryIncome",
            "forecastOrdinaryIncomeChange",
        ),
        ("netIncome", "forecastNetIncome", "forecastNetIncomeChange"),
        ("eps", "forecastEps", "forecastEpsChange"),
    )
    for name, value_key, change_key in metric_pairs:
        metrics[name] = {
            "value": finite_number(forecast_record.get(value_key)),
            "change": finite_number(forecast_record.get(change_key)),
        }

    forecast = {
        "kind": "forecast",
        "period": period,
        "fiscalYear": fiscal_year,
        "periodType": period_type,
        "sourceQuarter": quarter,
        "sourceQuarterLabel": (
            f"Q{quarter}" if quarter in (1, 2, 3, 4) else None
        ),
        "metrics": metrics,
    }

    actual_metric_fields = {
        "revenue": "revenue",
        "operatingIncome": "operatingIncome",
        "ordinaryIncome": "ordinaryIncome",
        "netIncome": "netIncome",
        "eps": "eps",
    }
    actual_metrics = {
        name: {"value": earnings_series_value(financial, field, fiscal_year)}
        for name, field in actual_metric_fields.items()
    }
    has_actual = any(
        metric["value"] is not None for metric in actual_metrics.values()
    )
    actual = (
        {
            "kind": "actual",
            "period": actual_period_label(period),
            "fiscalYear": fiscal_year,
            "periodType": "actual",
            "metrics": actual_metrics,
        }
        if has_actual
        else None
    )
    return {
        "period": {
            "label": period,
            "fiscalYear": fiscal_year,
            "periodType": period_type,
        },
        "forecast": forecast,
        "actual": actual,
    }


def forecast_split_factor(record: dict[str, Any]) -> float | None:
    """予想に付いてくる分割係数（1株→N株のN）。計算に使えない値はNone。"""
    factor = finite_number(record.get("forecastSplitFactor"))
    if factor is None or factor <= 0:
        return None
    return float(factor)


def reported_share_basis(record: dict[str, Any]) -> str:
    """APIが申告している「予想値がどの株数基準か」。無ければ空文字。"""
    value = record.get("forecastShareBasis")
    return str(value).strip().lower() if isinstance(value, str) else ""


def forecast_split_effective_date(record: dict[str, Any]) -> date | None:
    raw = record.get("forecastSplitEffectiveDate")
    if not isinstance(raw, str):
        return None
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        return None


def legs_match_annual(
    interim: float | int | None,
    final: float | int | None,
    annual: float | int | None,
) -> bool:
    """中間＋期末が会社発表の年間値と一致するか。

    一致するなら、その期の予想は中間も期末も年間値も同じ株数で書かれている
    （＝どこかの一つの基準で揃っている）。一致しないなら、中間と期末が別々の
    株数に対する金額で、足しても意味のある数にならない期だと分かる。

      東計電算のQ2開示: 86.5 + 97.5 = 184.0 ≠ 97.5   → 混在
      同じ銘柄のQ1開示: 62.5 + 110.5 = 173.0 = 173.0 → 単一基準

    円未満の端数と、まとめて出すときの丸めを吸収するため、0.01円と
    年間値の0.5%の大きいほうまでは同じとみなす。
    """
    if interim is None or final is None or annual is None:
        return False
    tolerance = max(0.01, abs(float(annual)) * 0.005)
    return abs(float(interim) + float(final) - float(annual)) <= tolerance


def forecast_on_price_basis(record: Any, *, today: date) -> dict[str, Any]:
    """予想配当を、株価と同じ株数基準に揃えて返す。

    分割日をまたぐ期は、中間配当と期末配当が別々の株数に対して支払われる。
    東計電算(4746)なら中間86.5円は分割前の1株に、期末97.5円は分割後の1株に
    対して払われるので、単純に足した184円はどの株数の話でもない。
    株価は分割日に市場が1/4にするので、配当も同じ日に基準を切り替えれば
    利回りが連続する。

      分割日より前: 中間 + 期末×係数 = 86.5 + 97.5×4 = 476.5円（分割前の1株）
      分割日以降  : 中間÷係数 + 期末 = 86.5÷4 + 97.5 = 119.125円（分割後の1株）

    どちらも株価と組み合わせると利回りは8.46%で一致する。API側の
    adjusted_forecast_dividend_per_share は常に分割後基準なので、分割前の
    株価と組み合わせると利回りが1/4になってしまう。分割日を過ぎてからなら
    基準が合うのでそのまま使える。

    ただし、分割日をまたぐ期でも会社が予想を一つの基準に揃えて出している
    ことがある。組み立てるかどうかは中間＋期末が年間値と一致するかで決める
    （legs_match_annual 参照）。合っているなら組み立て直さない。

    返す辞書:
      value   … 表示・利回り計算に使う年間予想
      basis   … どの経路で決めたか
                  single_basis_as_reported … 内訳の合計が年間値と一致した
                                              ので、会社発表の年間値のまま
                  pre_split_composed  … 分割日前。中間 + 期末×係数
                  pre_split_reported  … 内訳が無く、申告が pre_split
                  post_split_adjusted … 分割日以降。API側の分割後年間値
                  post_split_composed … 分割日以降。中間÷係数 + 期末
                  raw                 … 分割の予定なし、または判断材料なし
      interim … value と同じ株数基準に揃えた中間（取れなければNone）
      final   … 同じく期末
    """
    if not isinstance(record, dict):
        return {"value": None, "basis": "raw", "interim": None, "final": None}

    raw_value = bounded(record.get("forecastDividend"), 0, 1_000_000)
    interim = bounded(record.get("forecastInterimDividend"), 0, 1_000_000)
    final = bounded(record.get("forecastFinalDividend"), 0, 1_000_000)

    def as_raw() -> dict[str, Any]:
        return {
            "value": raw_value,
            "basis": "raw",
            "interim": interim,
            "final": final,
        }

    effective = forecast_split_effective_date(record)
    factor = forecast_split_factor(record)
    # 分割の予定が無い（大多数）、または係数が使えない値なら今までどおり。
    if effective is None or factor is None:
        return as_raw()

    if today < effective:
        # まずデータ自身で確かめる。中間＋期末が年間値と一致するなら、その期の
        # 予想は一つの株数基準で書かれているので、組み立て直すと係数が二重に
        # 掛かる（東計電算のQ1開示なら 62.5+110.5×4 = 504.5円。正しくは173円で、
        # API側の adjusted 43.25×4 = 173 とも合う）。
        if legs_match_annual(interim, final, raw_value):
            return {
                "value": raw_value,
                "basis": "single_basis_as_reported",
                "interim": interim,
                "final": final,
            }
        # 合計が合わないのは、中間と期末が別々の株数に対する金額だから。
        # forecast_share_basis が何と申告していても（post_split でも）、
        # ここはデータ自身の検算を優先して組み立てる。混在期は会社も年間値と
        # して意味のある一つの数字を出せていない（東計電算のQ2開示なら
        # forecast_dividend_per_share=97.5 は期末だけの額）ので、内訳から
        # 組み立てるほうが根拠がはっきりする。
        if interim is None or final is None:
            # 内訳が無いと検算できない。申告が pre_split なら、生の年間値が
            # 分割前の株価と同じ基準だと分かるので、そのまま使ってよい。
            if reported_share_basis(record) == "pre_split":
                return {
                    "value": raw_value,
                    "basis": "pre_split_reported",
                    "interim": interim,
                    "final": final,
                }
            return as_raw()
        composed_final = float(final) * factor
        total = bounded(round(float(interim) + composed_final, 4), 0, 1_000_000)
        if total is None:
            return as_raw()
        return {
            "value": total,
            "basis": "pre_split_composed",
            "interim": interim,
            "final": round(composed_final, 4),
        }

    adjusted = bounded(record.get("forecastDividendAdjusted"), 0, 1_000_000)
    if adjusted is not None:
        composed_interim = (
            round(float(interim) / factor, 4) if interim is not None else None
        )
        return {
            "value": adjusted,
            "basis": "post_split_adjusted",
            "interim": composed_interim,
            "final": final,
        }
    if interim is None or final is None:
        return as_raw()
    composed_interim = float(interim) / factor
    total = bounded(round(composed_interim + float(final), 4), 0, 1_000_000)
    if total is None:
        return as_raw()
    return {
        "value": total,
        "basis": "post_split_composed",
        "interim": round(composed_interim, 4),
        "final": final,
    }


def pending_dividends(
    forecast_record: Any,
    series_years: set[int],
    *,
    today: date,
    adjustment: dict[str, Any] | None = None,
    fiscal_month: int | None = None,
) -> dict[str, dict[str, Any]]:
    """まだ配当系列に載っていない事業年度を、会社発表の値で組み立てる。

    出どころは edinetdb.jp（決算短信ベース）。以前この枠に出していた
    「集計中」は、Yahooの権利落ちベースの暦年途中累計だった。事業年度の
    棒グラフには混ぜられない値なので、会社が発表した予想・確定額に置き換える。

    kind は "confirmed"（会社発表の確定額）と "forecast"（予想）の2種類。
    同じ年に両方あるときは確定を優先する。EDINETの有報から実績を取れた年
    （series_years）は、そちらが正なのでここには出さない。

    台帳の分割係数は、対象年度の期末が効力発生日より前のときだけ掛ける
    （adjustment_factor_for_period と同じ判定）。edinetdb.jpの確定額・予想は
    会社がその期の実際の株数で発表した値なので、効力発生日を過ぎた期はすでに
    現在の株数基準になっている。ここへさらに台帳の係数を掛けると二重補正に
    なる（分割が発効済みのSPK(7466)で、41円の予想が20.5円に潰れた症状の原因）。
    """
    if not isinstance(forecast_record, dict):
        return {}
    latest_year = max(series_years) if series_years else None
    # 決算期末から有報が出るまで最長でも1年程度。それより先の年が来たら
    # 取得元の値がおかしいので捨てる（未来の年に棒が伸びると事故に見える）。
    upper_year = today.year + 2

    def acceptable(year: int | None) -> bool:
        return (
            year is not None
            and year not in series_years
            and (latest_year is None or year > latest_year)
            and 1990 <= year <= upper_year
        )

    def factor_for(year: int | None) -> float:
        if adjustment is None or year is None:
            return 1.0
        return adjustment_factor_for_period(
            adjustment, year, fiscal_month=fiscal_month, field="dividend"
        )

    result: dict[str, dict[str, Any]] = {}

    confirmed = bounded(forecast_record.get("confirmedDividend"), 0, 1_000_000)
    fiscal_year_end = forecast_record.get("confirmedFiscalYearEnd")
    confirmed_year = None
    if isinstance(fiscal_year_end, str) and fiscal_year_end[:4].isdigit():
        confirmed_year = int(fiscal_year_end[:4])
    # 「確定」を名乗れるのは、本決算(Q4)短信に載った実績だけ。
    # Q1〜Q3短信の年間配当欄には予想込みの額が入ってくるため、それを
    # 確定と表示すると誤り(例: 3837アドソル日進の2027年3月期のQ1短信)。
    # 期末日チェックだけだと、期末後〜本決算短信発表前(3月決算なら4〜5月)に
    # Q3短信の予想込み額が確定を名乗る穴が残るので、短信の四半期区分で判定
    # する。区分が無い古いレコードは期末日チェックのみ(従来動作の維持)。
    # 期末前の値はここでは出さず、後段の予想(kind=forecast)に任せる。
    quarter = forecast_record.get("forecastQuarter")
    from_full_year_report = quarter == 4 or quarter is None
    fiscal_year_ended = False
    if isinstance(fiscal_year_end, str):
        try:
            fiscal_year_ended = date.fromisoformat(fiscal_year_end[:10]) <= today
        except ValueError:
            fiscal_year_ended = False
    if (
        confirmed is not None
        and confirmed > 0
        and from_full_year_report
        and fiscal_year_ended
        and acceptable(confirmed_year)
    ):
        result[str(confirmed_year)] = {
            "value": round(float(confirmed) * factor_for(confirmed_year), 4),
            "kind": "confirmed",
            "label": "確定",
            "fiscalYearEnd": fiscal_year_end,
            "source": "edinetdb",
        }

    # 分割日をまたぐ期は、中間と期末が別の株数に対する金額なので、
    # 株価と同じ基準に揃えてから棒にする。
    resolved = forecast_on_price_basis(forecast_record, today=today)
    forecast = resolved["value"]
    forecast_year = forecast_fiscal_year(forecast_record)
    if (
        forecast is not None
        and forecast > 0
        and acceptable(forecast_year)
        and str(forecast_year) not in result
    ):
        forecast_factor = factor_for(forecast_year)
        entry: dict[str, Any] = {
            "value": round(float(forecast) * forecast_factor, 4),
            "kind": "forecast",
            "label": "予想",
            "source": "edinetdb",
        }
        # どの経路で決めた値かを残す（分割がある銘柄だけ）。
        if resolved["basis"] != "raw":
            entry["basis"] = resolved["basis"]
        period = forecast_record.get("forecastPeriod")
        if isinstance(period, str) and period.strip():
            entry["period"] = period.strip()[:100]
        for name in ("interim", "final"):
            value = resolved[name]
            if value is not None:
                entry[name] = round(float(value) * forecast_factor, 4)
        result[str(forecast_year)] = entry

    fetched_at = forecast_record.get("lastFetchedAt")
    if isinstance(fetched_at, str):
        for entry in result.values():
            entry["fetchedAt"] = fetched_at[:40]
    return result


def json_text(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )


def _parse_price_updated(updated: str) -> datetime | None:
    """kouhaitou-db CSVヘッダの日時文字列（"YYYY/MM/DD HH:MM:SS", JST）をdatetimeへ。

    解釈できない場合は None を返す。
    """
    updated = (updated or "").strip()
    if not updated:
        return None
    try:
        naive = datetime.strptime(updated, "%Y/%m/%d %H:%M:%S")
    except ValueError:
        return None
    return naive.replace(tzinfo=JST)


def check_price_freshness(
    updated: str, *, now: datetime | None = None
) -> None:
    """日次株価CSVヘッダの最終更新日時が古すぎる場合、::warning:: を出す。

    kouhaitou-db側の更新ジョブが止まっている、あるいはCDNキャッシュが
    想定より長く残っているケースに人が気づけるようにするための健全性
    チェック。ビルド自体は止めない（例外を投げない）。
    """
    now = now or datetime.now(JST)
    parsed = _parse_price_updated(updated)
    if parsed is None:
        print(f"::warning::日次株価CSVの最終更新日時を解釈できません: {updated!r}")
        return
    age = now - parsed
    if age > PRICE_FRESHNESS_WARN_AFTER:
        hours = age.total_seconds() / 3600
        print(
            "::warning::日次株価CSVの最終更新日時が古い可能性があります"
            f"（{updated}、約{hours:.1f}時間前）。"
            "kouhaitou-db側の株価更新ジョブとCDNキャッシュの状況を確認してください。"
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
    check_price_freshness(updated)
    return prices, daily_dividends, updated


def _price_session_meta_url(prices_url: str) -> str:
    """database.csv のURLから、隣にある price_update_meta.json のURLを組み立てる。

    kouhaitou-db側の株価のみ更新ワークフローが書き出す補助ファイル。
    URLの形が想定と違う（database.csvで終わらない）場合は空文字を返し、
    呼び出し側で取得をスキップする。
    """
    suffix = "database.csv"
    if not prices_url.endswith(suffix):
        return ""
    return prices_url[: -len(suffix)] + "price_update_meta.json"


def load_price_session_meta(url: str) -> dict[str, Any] | None:
    """kouhaitou-dbの price_update_meta.json を取得する。

    株価のみ更新ワークフロー導入前のkouhaitou-db（ファイルが無い）、
    ネットワーク不調、JSONとして壊れている等、どの理由でも失敗を
    ビルド停止にはしない（Noneを返し、呼び出し側でフォールバックする）。
    """
    if not url:
        return None
    try:
        completed = subprocess.run(
            [
                "curl",
                "--fail",
                "--silent",
                "--show-error",
                "--location",
                "--max-time",
                "30",
                url,
            ],
            capture_output=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    try:
        data = json.loads(completed.stdout.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def forecast_values(
    record: Any, price: float | int | None, *, today: date
) -> tuple[float | int | None, float | None, str | None, str | None, str | None]:
    """表示用の予想配当・予想利回りと、その値をどう決めたかの根拠を返す。

    株価と同じ株数基準に揃えた値を使う（forecast_on_price_basis 参照）。
    分割の予定が無い銘柄では今までどおり forecastDividend がそのまま出る。
    """
    if not isinstance(record, dict):
        return None, None, None, None, None
    resolved = forecast_on_price_basis(record, today=today)
    dividend = resolved["value"]
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
        resolved["basis"],
    )


def create_database(
    database_path: Path,
    financials: list[dict[str, Any]],
    sectors: dict[str, Any],
    tickers: dict[str, dict[str, Any]],
    forecasts: dict[str, Any],
    source_paths: list[Path],
    prices_url: str,
    stock_actions_by_code: dict[str, list[dict[str, Any]]] | None = None,
    stock_actions_path: Path | Sequence[Path] | None = None,
    fiscal_by_code: dict[str, dict[str, Any]] | None = None,
    fiscal_path: Path | None = None,
    calendar_by_code: dict[str, dict[str, Any]] | None = None,
    calendar_path: Path | None = None,
    today: date | None = None,
    stock_action_fallbacks: list[dict[str, Any]] | None = None,
) -> tuple[int, int, int]:
    financial_by_code, skipped_financials = index_by_code(
        financials, "all_financials"
    )
    if skipped_financials:
        raise ValueError("all_financialsに4桁英数でないコードがあります")
    daily_prices, daily_dividends, prices_updated = load_daily_prices(prices_url)
    # kouhaitou-dbが株価のみ更新ワークフロー（前場寄付/後場引けの1日2回）を
    # 導入している場合、隣にある price_update_meta.json からセッション名を読む。
    # 無い/取れない場合は「afternoon_close」（従来の1日1回更新=終値相当）に
    # フォールバックする。
    price_session_meta = load_price_session_meta(_price_session_meta_url(prices_url))
    price_session = (
        (price_session_meta or {}).get("session") or "afternoon_close"
    )
    # 「その株価データが実際にいつ時点のものか」を表す日付。朝6:00のフル更新
    # （kouhaitou-db側のupdate.yml）はこのmetaファイルを更新しないため、
    # 6:00〜7:00の間はdailyPricesUpdated（CSVヘッダの更新日時）とセッション名の
    # 組み合わせが噛み合わなくなる。表示側（checker.html）がその不整合を避けら
    # れるよう、メタが持つ日付をそのまま別キーとして渡す。metaが無い/取得に
    # 失敗した場合はNoneのままとし、payloadにはキー自体を入れない
    # （呼び出し側でif文により省く）。
    price_session_as_of = (price_session_meta or {}).get("as_of_date")
    stock_actions_by_code = stock_actions_by_code or {}
    fiscal_by_code = fiscal_by_code or {}
    calendar_by_code = calendar_by_code or {}
    stock_action_fallbacks = (
        stock_action_fallbacks if stock_action_fallbacks is not None else []
    )
    today = today or datetime.now(JST).date()
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
                    streak_capped INTEGER NOT NULL DEFAULT 0,
                    streak_nd_capped INTEGER NOT NULL DEFAULT 0,
                    -- 1なら「株式分割の基準がそろわず年数を判定できない」。
                    -- streak / streak_nd はNULLになる。NULLは比較条件に
                    -- 一致しないので、絞り込み（streak >= N）からは自動的に外れる。
                    streak_unreliable INTEGER NOT NULL DEFAULT 0,
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
            matched_tickers = 0
            adjusted_stocks = 0
            applied_events = 0
            fiscal_based_stocks = 0
            calendar_based_stocks = 0
            frozen_calendar_stocks = 0
            streak_unreliable_stocks = 0
            pending_stocks = 0
            pending_confirmed = 0
            pending_forecast = 0
            payout_line_stocks = 0
            for code, financial in financial_by_code.items():
                ticker = tickers.get(code, {})
                if ticker:
                    matched_tickers += 1

                daily_price = daily_prices.get(code)
                fiscal_record = fiscal_by_code.get(code)
                adjustment = split_adjustment(
                    stock_actions_by_code.get(code, []),
                    fallback_events=stock_action_fallbacks,
                )
                if adjustment is not None:
                    adjusted_stocks += 1
                    applied_events += len(adjustment["events"])
                effective_price = daily_price
                daily_yield = None
                # 分子はkouhaitou-dbの年間配当を最優先で使い、既知の分割は
                # stock_actions_manual.jsonの係数でこの後に現在株価基準へ揃える。
                numerator = daily_dividends.get(code)
                # kouhaitou-dbが把握していて年間配当0＝現在無配。
                # この場合は過去の配当履歴に遡らない（東電の2010年60円のような
                # 十数年前の金額で利回りを出してしまうため）。
                currently_unpaid = numerator is not None and float(numerator) <= 0
                if adjustment is not None and finite_number(numerator) is not None:
                    numerator = (
                        float(numerator) * adjustment["dividendFactor"]
                    )
                if daily_price and numerator is not None and float(numerator) > 0:
                    daily_yield = round(float(numerator) / daily_price * 100, 2)
                forecast_record = forecasts.get(code) or {}
                (
                    forecast_dividend,
                    forecast_yield,
                    forecast_period,
                    forecast_fetched_at,
                    forecast_basis,
                ) = forecast_values(
                    forecasts.get(code), effective_price, today=today
                )

                payload = dict(financial)
                payload["roeYearEnd"] = roe_year_end(financial)
                payload["streakBase"] = None
                for key in ("name", "market", "sector", "sector17"):
                    if ticker.get(key):
                        payload[key] = ticker[key]
                # グラフの折れ線。EDINET由来・事業年度キーなので棒と軸が揃う。
                payload["payoutRatio"] = payout_series(financial)
                if payload["payoutRatio"]:
                    payout_line_stocks += 1
                if adjustment is not None:
                    dividend_factor = adjustment["dividendFactor"]
                    eps_bps_factor = adjustment["epsBpsFactor"]
                    if "dividendPerShare" in financial:
                        payload["dividendPerShare"] = adjust_per_share_series(
                            financial["dividendPerShare"],
                            adjustment,
                            fiscal_month=(
                                fiscal_record.get("fiscalMonth")
                                if fiscal_record
                                else None
                            ),
                            field="dividend",
                        )
                    # PER/PBR自体は価格と1株当たり値の比なので分割で不変。
                    # 現在価格との計算に使うEPS/BPSだけを現在基準へ揃える。
                    if eps_bps_factor != 1.0:
                        for key in ("eps", "bps"):
                            if key in financial:
                                payload[key] = adjust_per_share_series(
                                    financial[key],
                                    adjustment,
                                    fiscal_month=(
                                        fiscal_record.get("fiscalMonth")
                                        if fiscal_record
                                        else None
                                    ),
                                    field="epsBps",
                                )
                    payload["splitAdjustment"] = adjustment

                # 配当系列を事業年度ベースへ差し替える。
                # 系列が取れなかった銘柄だけ、暦年の系列のまま残す。
                fiscal = fiscal_record
                if fiscal is not None:
                    fiscal_based_stocks += 1
                    series = fiscal["series"]
                    if adjustment is not None:
                        series_adjustment = adjustment_for_unadjusted_series(
                            adjustment,
                            series,
                            fiscal_month=fiscal.get("fiscalMonth"),
                        )
                        # 既存系列に未反映の分割だけを、各年度の期末基準で揃える。
                        series = {
                            year: round(
                                value
                                * adjustment_factor_for_period(
                                    series_adjustment,
                                    year,
                                    fiscal_month=fiscal.get("fiscalMonth"),
                                    field="dividend",
                                ),
                                4,
                            )
                            for year, value in series.items()
                        }
                    streak_reliable = fiscal.get("streakReliable", True)
                    stats = fiscal_dividend_stats(
                        series,
                        streak_reliable=streak_reliable,
                        break_years=fiscal.get("streakBreakYears", []),
                    )
                    if not streak_reliable:
                        streak_unreliable_stocks += 1
                    payload["annual"] = {
                        str(year): series[year] for year in sorted(series)
                    }
                    payload["streakIncrease"] = stats["streakIncrease"]
                    payload["streakNonDecrease"] = stats["streakNonDecrease"]
                    payload["streakIncreaseCapped"] = stats[
                        "streakIncreaseCapped"
                    ]
                    payload["streakNonDecreaseCapped"] = stats[
                        "streakNonDecreaseCapped"
                    ]
                    payload["cagr3"] = stats["cagr3"]
                    payload["cagr5"] = stats["cagr5"]
                    payload["cagr10"] = stats["cagr10"]
                    payload["dividendSeries"] = {
                        "basis": "fiscal",
                        "fiscalMonth": fiscal["fiscalMonth"],
                        "startYear": min(series),
                        "endYear": max(series),
                        # 出典。externalYears に無い年はすべてEDINET由来。
                        "externalSource": fiscal["externalSource"],
                        "externalYears": fiscal["externalYears"],
                        "connectionStatus": fiscal["connectionStatus"],
                        "connectionReason": fiscal["connectionReason"],
                    }
                    # 画面で「株式分割の影響で判定できません」と出せるようにする印。
                    # 年数がNULLなのが「データが無い」からなのか
                    # 「基準がそろわず数えられない」からなのかを区別するため。
                    payload["streakUnreliable"] = (
                        None
                        if streak_reliable
                        else {
                            "reason": fiscal.get("streakUnreliableReason"),
                            "note": fiscal.get("streakUnreliableNote"),
                            "breakYears": fiscal.get("streakBreakYears", []),
                        }
                    )
                else:
                    # 事業年度の系列を作れなかった銘柄。EDINETにも
                    # haitoukin-checkerにも配当の記載が無い会社で、実質的な
                    # 配当履歴があるのは14銘柄だけ（東京電力など、十数年前に
                    # 配当をやめた会社が中心）。その分だけ暦年の凍結
                    # スナップショットから出す。残りは配当履歴そのものが無い。
                    calendar_based_stocks += 1
                    frozen = calendar_by_code.get(code)
                    series = frozen["series"] if frozen else {}
                    if series and adjustment is not None:
                        series_adjustment = adjustment_for_unadjusted_series(
                            adjustment, series, fiscal_month=12
                        )
                        series = {
                            year: round(
                                value
                                * adjustment_factor_for_period(
                                    series_adjustment,
                                    year,
                                    fiscal_month=12,
                                    field="dividend",
                                ),
                                4,
                            )
                            for year, value in series.items()
                        }
                    stats = fiscal_dividend_stats(series) if series else {}
                    if series:
                        frozen_calendar_stocks += 1
                        payload["annual"] = {
                            str(year): series[year] for year in sorted(series)
                        }
                    payload["streakIncrease"] = stats.get("streakIncrease")
                    payload["streakNonDecrease"] = stats.get("streakNonDecrease")
                    payload["cagr3"] = stats.get("cagr3")
                    payload["cagr5"] = stats.get("cagr5")
                    payload["cagr10"] = stats.get("cagr10")
                    payload["streakIncreaseCapped"] = bool(
                        stats.get("streakIncreaseCapped")
                    )
                    payload["streakNonDecreaseCapped"] = bool(
                        stats.get("streakNonDecreaseCapped")
                    )
                    payload["streakUnreliable"] = None
                    payload["dividendSeries"] = {
                        "basis": "calendar",
                        "frozen": bool(series),
                        "startYear": min(series) if series else None,
                        "endYear": max(series) if series else None,
                        "externalSource": "yfinance" if series else None,
                        "externalYears": sorted(series),
                    }

                # まだ系列に載っていない事業年度を、会社発表の予想・確定額で足す。
                # 以前ここに出していたYahooの「集計中」（権利落ちベースの暦年
                # 途中累計）の置き換え。
                series_years = {
                    int(year)
                    for year in (payload.get("annual") or {})
                    if str(year).isdigit()
                }
                pending = pending_dividends(
                    forecast_record,
                    series_years,
                    today=today,
                    adjustment=adjustment,
                    fiscal_month=(
                        fiscal_record.get("fiscalMonth") if fiscal_record else None
                    ),
                )
                payload["annualPending"] = pending
                # 既存の表示コードが読む形。値だけの {年: 円}。
                payload["annualPartial"] = {
                    year: entry["value"] for year, entry in pending.items()
                }
                if pending:
                    pending_stocks += 1
                    pending_confirmed += sum(
                        1 for e in pending.values() if e["kind"] == "confirmed"
                    )
                    pending_forecast += sum(
                        1 for e in pending.values() if e["kind"] == "forecast"
                    )
                streak_value = finite_number(payload.get("streakIncrease"))
                streak_nd_value = finite_number(payload.get("streakNonDecrease"))
                cagr3_value = finite_number(payload.get("cagr3"))

                payload["code"] = code
                payload["industry"] = financial.get("industry")
                breakdown_entry = dividend_breakdown.get(code)
                if breakdown_entry:
                    payload["dividendBreakdown"] = breakdown_entry
                    payload["streakBase"] = streak_base_from_breakdown(
                        series, breakdown_entry
                    )
                    payload["streakNoDecreaseBase"] = (
                        streak_no_decrease_base_from_breakdown(
                            series, breakdown_entry
                        )
                    )
                else:
                    # 内訳DBに記録が無い銘柄＝記念・特別配当の記録が無い
                    # （＝全額が普通配当）ので、実質値は全額ベースの
                    # streakIncrease/streakNonDecrease とそのまま同値になる。
                    # 全額ベース側がNULL（判定不能）ならフォールバックもNULLのまま。
                    payload["streakBase"] = payload.get("streakIncrease")
                    payload["streakNoDecreaseBase"] = payload.get(
                        "streakNonDecrease"
                    )
                if daily_price:
                    payload["price"] = daily_price
                    # 表示側が「2026年8月13日 前場寄付時点」のように出すための生データ。
                    # 日時の直書き表記は取引時間の変更に弱いため、セッション名は
                    # フロント側でラベルに変換する想定（checker.html参照）。
                    payload["dailyPricesUpdated"] = prices_updated or None
                    payload["dailyPricesSession"] = price_session
                    if price_session_as_of:
                        payload["dailyPricesAsOf"] = price_session_as_of
                if daily_yield is not None:
                    payload["dividendYield"] = daily_yield
                elif currently_unpaid:
                    # dividends.json由来の古い利回りが銘柄詳細に残らないようにする
                    payload["dividendYield"] = None
                payload["forecastDividend"] = forecast_dividend
                payload["forecastYield"] = forecast_yield
                payload["forecastPeriod"] = forecast_period
                payload["forecastFetchedAt"] = forecast_fetched_at
                # forecastDividend をどう決めたかの根拠。分割日をまたぐ期は
                # 生の値ではなく中間・期末から組み立てているので、後から
                # 表示の裏を取れるようにしておく。
                payload["forecastBasis"] = forecast_basis
                for key in EARNINGS_FORECAST_FIELDS:
                    # 取得前・予想非開示企業も固定キーで返す。表示側はNULLを
                    # 「予想なし」として扱え、古いstateとの混在でもスキーマが
                    # 変わらない。
                    payload[key] = forecast_record.get(key)
                payload["forecastFiscalYear"] = forecast_fiscal_year(
                    forecast_record
                )
                payload["forecastPeriod"] = forecast_period
                payload["forecastQuarter"] = forecast_record.get(
                    "forecastQuarter"
                )
                payload["forecastQuarterLabel"] = forecast_record.get(
                    "forecastQuarterLabel"
                )
                payload["forecastPeriodType"] = forecast_record.get(
                    "forecastPeriodType"
                )
                payload["forecastKind"] = "forecast"
                # 同じ事業年度の有報実績が届いたら actual が埋まる。表示側は
                # actual を優先すれば、予想カードを本表示へ自然に切り替えられる。
                payload["earnings"] = earnings_payload(payload, forecast_record)
                # 会社発表の確定年度配当（edinetdb由来）。
                # annualPending の「確定」バーと同じ値で、旧い表示コードが
                # こちらを読むので残してある。
                confirmed = bounded(forecast_record.get("confirmedDividend"), 0, 1_000_000)
                if confirmed is not None:
                    payload["confirmedDividend"] = confirmed
                    payload["confirmedFiscalYearEnd"] = forecast_record.get("confirmedFiscalYearEnd")

                name = str(
                    ticker.get("name") or financial.get("name") or code
                ).strip()
                rows.append(
                    (
                        code,
                        name,
                        financial.get("industry"),
                        bounded(daily_yield, 0, 30),
                        forecast_yield,
                        streak_value,
                        streak_nd_value,
                        1 if payload.get("streakIncreaseCapped") else 0,
                        1 if payload.get("streakNonDecreaseCapped") else 0,
                        1 if payload.get("streakUnreliable") else 0,
                        cagr3_value,
                        latest_number(financial.get("roe")),
                        latest_number(financial.get("equityRatio")),
                        bounded(payout_value(financial), 0, 1000),
                        bounded(effective_price, 0.1, 10_000_000),
                        json_text(payload),
                    )
                )

            connection.executemany(
                """
                INSERT INTO stocks (
                    code, name, industry, yield, forecast_yield, streak,
                    streak_nd, streak_capped, streak_nd_capped,
                    streak_unreliable, cagr3,
                    roe, equity_ratio, payout, price, payload
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                "ticker_match_count": str(matched_tickers),
                "financials_source": str(source_paths[0]),
                "sectors_source": str(source_paths[1]),
                "tickers_source": str(source_paths[2]),
                "forecasts_source": str(source_paths[3]),
                "stock_actions_source": stock_action_source_label(stock_actions_path),
                "split_adjusted_stock_count": str(adjusted_stocks),
                "split_adjustment_event_count": str(applied_events),
                "split_adjustment_fallback_count": str(
                    len(stock_action_fallbacks)
                ),
                "split_adjustment_fallbacks": json_text(
                    stock_action_fallbacks
                ),
                "fiscal_dividends_source": str(fiscal_path or ""),
                "fiscal_based_stock_count": str(fiscal_based_stocks),
                "calendar_based_stock_count": str(calendar_based_stocks),
                "calendar_dividends_source": str(calendar_path or ""),
                "frozen_calendar_stock_count": str(frozen_calendar_stocks),
                "streak_unreliable_stock_count": str(streak_unreliable_stocks),
                "pending_stock_count": str(pending_stocks),
                "pending_confirmed_count": str(pending_confirmed),
                "pending_forecast_count": str(pending_forecast),
                "payout_line_stock_count": str(payout_line_stocks),
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
            print(
                f"配当系列: 事業年度 {fiscal_based_stocks:,}銘柄 / "
                f"暦年（凍結） {frozen_calendar_stocks:,}銘柄 / "
                f"配当履歴なし {calendar_based_stocks - frozen_calendar_stocks:,}銘柄"
            )
            print(
                f"連続増配を判定できない銘柄（株式分割の基準ズレ）: "
                f"{streak_unreliable_stocks:,}銘柄"
            )
            print(
                f"進行中の事業年度のバー: {pending_stocks:,}銘柄"
                f"（確定 {pending_confirmed:,} / 予想 {pending_forecast:,}）"
            )
            print(f"配当性向の折れ線: {payout_line_stocks:,}銘柄")
            print(
                f"株式分割補正フォールバック: "
                f"{len(stock_action_fallbacks):,}件"
            )
            for fallback in stock_action_fallbacks:
                print(
                    f"  {fallback['eventId']} "
                    f"({fallback['field']}): {fallback['reason']}"
                )
        finally:
            connection.close()

        os.chmod(temporary_path, 0o664)
        os.replace(temporary_path, database_path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise

    return len(rows), matched_tickers, frozen_calendar_stocks


def main() -> None:
    args = parse_args()
    financials = load_json(args.financials, list)
    sectors = load_json(args.sectors, dict)
    tickers = load_tickers(args.tickers)
    fiscal_dividends = load_fiscal_dividends(args.fiscal_dividends)
    calendar_dividends = load_calendar_dividends(args.calendar_dividends)
    stock_action_fallbacks: list[dict[str, Any]] = []
    stock_action_paths = [args.stock_actions, args.stock_actions_extracted]
    stock_actions = load_stock_actions(
        stock_action_paths, fallback_events=stock_action_fallbacks
    )
    forecasts = load_forecasts(args.forecasts)
    count, matched, frozen = create_database(
        args.output,
        financials,
        sectors,
        tickers,
        forecasts,
        [
            args.financials,
            args.sectors,
            args.tickers,
            args.forecasts,
        ],
        args.prices_url,
        stock_actions,
        stock_action_paths,
        fiscal_dividends,
        args.fiscal_dividends,
        calendar_dividends,
        args.calendar_dividends,
        stock_action_fallbacks=stock_action_fallbacks,
    )
    size = args.output.stat().st_size
    print(f"生成完了: {args.output}")
    print(f"銘柄数: {count:,}（銘柄マスタ突合: {matched:,}）")
    print(f"暦年の凍結スナップショットを使った銘柄: {frozen:,}")
    print(f"業種数: {len(sectors):,}")
    print(f"ファイルサイズ: {size:,} bytes ({size / 1024 / 1024:.2f} MiB)")


if __name__ == "__main__":
    main()
