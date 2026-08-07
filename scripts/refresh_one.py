#!/usr/bin/env python3
"""指定した銘柄の配当予想だけを、巡回を待たずに取り直す。

決算月から作る近似発表日は最大2週間ずれる。東計電算(4746)は8/3にQ2決算短信で
分割と大幅増配を出したが、近似日は8/15だった。日次更新のイベント枠で拾えな
かった場合の手当てとして、銘柄を指定して即座に直せるようにしておく。

EDINETコードと決算月は edinet/<コード>.json（このリポジトリにある配信用feed）
から引く。feedに無い新規銘柄などは --overrides で直接渡せる。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date
from pathlib import Path
from typing import Any


sys.path.insert(0, str(Path(__file__).resolve().parent))

import fetch_forecasts  # noqa: E402


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_EDINET_DIR = REPOSITORY_ROOT / "edinet"
DEFAULT_STATE = REPOSITORY_ROOT / "forecasts_state.json"
# 一度に取り直せる件数。手作業の押し間違いで枠を使い切らないための歯止め。
MAX_TARGETS = 20
# 実行後に表示する項目（何がどう変わったか目で確かめるため）。
SHOWN_FIELDS = (
    "forecastDividend",
    "forecastDividendAdjusted",
    "forecastInterimDividend",
    "forecastFinalDividend",
    "forecastSplitFactor",
    "forecastSplitEffectiveDate",
    "forecastShareBasis",
    "forecastPeriod",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--edinet-dir", type=Path, default=DEFAULT_EDINET_DIR)
    parser.add_argument(
        "--codes",
        required=True,
        help="取り直す銘柄コード（カンマ区切り。例: 4746,9433）",
    )
    parser.add_argument(
        "--overrides",
        default="",
        help=(
            "feedに無い銘柄の指定（コード:EDINETコード:決算月 をカンマ区切り。"
            "例: 4746:E05066:12）"
        ),
    )
    parser.add_argument(
        "--today", type=date.fromisoformat, default=date.today(), metavar="YYYY-MM-DD"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="取得して表示するだけで、stateには書き戻さない（API枠は消費します）",
    )
    return parser.parse_args()


def parse_codes(raw: str) -> list[str]:
    codes: list[str] = []
    for chunk in raw.replace("\n", ",").split(","):
        text = chunk.strip()
        if not text:
            continue
        code = fetch_forecasts.normalize_code(text)
        if not code:
            raise ValueError(f"銘柄コードとして読めません: {text!r}")
        if code not in codes:
            codes.append(code)
    if not codes:
        raise ValueError("銘柄コードが1つも指定されていません")
    if len(codes) > MAX_TARGETS:
        raise ValueError(
            f"一度に指定できるのは{MAX_TARGETS}銘柄までです（{len(codes)}件）"
        )
    return codes


def parse_overrides(raw: str) -> dict[str, tuple[str, int]]:
    """「コード:EDINETコード:決算月」を読む。feedに無い銘柄の逃げ道。"""
    result: dict[str, tuple[str, int]] = {}
    for chunk in raw.replace("\n", ",").split(","):
        text = chunk.strip()
        if not text:
            continue
        parts = [part.strip() for part in text.split(":")]
        if len(parts) != 3:
            raise ValueError(
                f"--overrides は コード:EDINETコード:決算月 の形で書いてください: {text!r}"
            )
        code = fetch_forecasts.normalize_code(parts[0])
        edinet_code = parts[1].upper()
        if not code:
            raise ValueError(f"銘柄コードとして読めません: {parts[0]!r}")
        if not fetch_forecasts.EDINET_CODE_PATTERN.fullmatch(edinet_code):
            raise ValueError(f"EDINETコードとして読めません: {parts[1]!r}")
        try:
            fiscal_month = int(parts[2])
        except ValueError as error:
            raise ValueError(f"決算月は1〜12の数字で書いてください: {parts[2]!r}") from error
        if not 1 <= fiscal_month <= 12:
            raise ValueError(f"決算月は1〜12で指定してください: {fiscal_month}")
        result[code] = (edinet_code, fiscal_month)
    return result


def resolve_target(
    code: str, edinet_dir: Path, overrides: dict[str, tuple[str, int]]
) -> fetch_forecasts.Candidate:
    """銘柄コードから EDINETコードと決算月を引く。

    このリポジトリに data/code_map.json は無い（edinet-direct側のファイル）。
    代わりに配信用feed（edinet/<コード>.json）が edinetCode と fiscalMonth を
    持っているので、そこから引ける。
    """
    if code in overrides:
        edinet_code, fiscal_month = overrides[code]
    else:
        path = edinet_dir / f"{code}.json"
        if not path.exists():
            raise ValueError(
                f"{code}: {path} が無いのでEDINETコードを引けません。"
                f"--overrides {code}:Exxxxx:決算月 の形で指定してください。"
            )
        feed = fetch_forecasts.load_json(path, dict)
        edinet_code = str(feed.get("edinetCode") or "").strip().upper()
        fiscal_month = feed.get("fiscalMonth")
        if not fetch_forecasts.EDINET_CODE_PATTERN.fullmatch(edinet_code):
            raise ValueError(f"{code}: feedのedinetCodeが不正です（{edinet_code!r}）")
        if (
            isinstance(fiscal_month, bool)
            or not isinstance(fiscal_month, int)
            or not 1 <= fiscal_month <= 12
        ):
            raise ValueError(f"{code}: feedのfiscalMonthが不正です（{fiscal_month!r}）")
    return fetch_forecasts.Candidate(
        code=code,
        fiscal_month=fiscal_month,
        edinet_code=edinet_code,
        dividend_yield=0.0,
        priority_rank=None,
        event=None,
        last_fetched=None,
    )


def describe(record: Any, fields: tuple[str, ...]) -> str:
    source = record if isinstance(record, dict) else {}
    return json.dumps(
        {key: source.get(key) for key in fields}, ensure_ascii=False
    )


def warn_if_remaining_is_low(remaining: int | None) -> None:
    """手動更新で定期実行分を使い切りそうなときに、利用者へ知らせる。"""
    if remaining is None:
        return
    if remaining <= fetch_forecasts.RATE_LIMIT_LOW_THRESHOLD:
        print(
            "警告: edinetdbの本日残量が少ないため、"
            f"このまま手動更新を続けると定期実行の枠がなくなる可能性があります"
            f"（残り{remaining}件）"
        )


def main() -> None:
    args = parse_args()
    api_key = os.environ.get("EDINETDB_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("EDINETDB_API_KEYがありません。")

    codes = parse_codes(args.codes)
    overrides = parse_overrides(args.overrides)
    targets = [resolve_target(code, args.edinet_dir, overrides) for code in codes]

    state = fetch_forecasts.load_state(args.state)
    stocks = state["stocks"]
    fetched_at = args.today.isoformat()
    failures: list[str] = []
    blocking_failures: list[str] = []

    for target in targets:
        try:
            parsed, remaining = fetch_forecasts.fetch_one(target, api_key)
        except fetch_forecasts.FetchError as error:
            # 1銘柄が失敗しても残りは進める。既存の保存値には触らない。
            failures.append(str(error))
            if not error.is_rate_limited:
                # 429だけは、成功した銘柄をstateへ反映するために許容する。
                # それ以外は従来どおり手動実行を異常終了させる。
                blocking_failures.append(str(error))
            print(f"取得失敗: {error}", file=sys.stderr)
            if error.remaining is not None:
                print(f"（失敗時点のedinetdb日次残量={error.remaining}）")
                warn_if_remaining_is_low(error.remaining)
            continue
        before = stocks.get(target.code)
        parsed["lastFetchedAt"] = fetched_at
        print(
            f"{target.code} / {target.edinet_code} / 決算月={target.fiscal_month} "
            f"（edinetdb日次残量={remaining if remaining is not None else '不明'}）"
        )
        warn_if_remaining_is_low(remaining)
        print("  前: " + describe(before, ("forecastDividend", "lastFetchedAt")))
        print("  後: " + describe(parsed, SHOWN_FIELDS))
        if args.dry_run:
            continue
        stocks[target.code] = parsed

    if args.dry_run:
        print("dry-run: stateは書き換えていません")
    else:
        fetch_forecasts.write_state(args.state, state)
        print(f"状態保存: {args.state}")

    print(
        f"summary targets={len(targets)} failed={len(failures)} "
        f"blocking={len(blocking_failures)}"
    )
    if blocking_failures:
        raise SystemExit(
            f"429以外の失敗が{len(blocking_failures)}銘柄で発生しました"
        )


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError) as error:
        print(f"エラー: {error}", file=sys.stderr)
        raise SystemExit(1) from error
