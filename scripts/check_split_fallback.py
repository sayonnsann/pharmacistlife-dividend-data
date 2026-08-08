#!/usr/bin/env python3
"""SQLiteに記録された株式分割補正フォールバックを確認する。"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Any


def check(database_path: Path) -> int:
    try:
        with sqlite3.connect(database_path) as connection:
            rows = dict(connection.execute("SELECT key, value FROM meta"))
    except (OSError, sqlite3.Error) as error:
        print(f"::error::SQLiteの分割補正メタ情報を読めません: {error}")
        return 1

    raw_count = rows.get("split_adjustment_fallback_count", "0")
    try:
        count = int(raw_count)
    except (TypeError, ValueError):
        print(
            "::error::split_adjustment_fallback_countが整数ではありません: "
            f"{raw_count!r}"
        )
        return 1
    if count < 0:
        print(
            "::error::split_adjustment_fallback_countが負数です: "
            f"{count}"
        )
        return 1

    raw_fallbacks = rows.get("split_adjustment_fallbacks", "[]")
    try:
        fallbacks: Any = json.loads(raw_fallbacks)
    except (TypeError, json.JSONDecodeError) as error:
        print(f"::error::分割補正フォールバック一覧がJSONではありません: {error}")
        return 1
    if not isinstance(fallbacks, list):
        print("::error::分割補正フォールバック一覧がarrayではありません")
        return 1
    if len(fallbacks) != count:
        print(
            "::error::分割補正フォールバックの件数が一致しません: "
            f"count={count} details={len(fallbacks)}"
        )
        return 1

    print(f"株式分割補正フォールバック: {count}件")
    for fallback in fallbacks:
        if not isinstance(fallback, dict):
            print(f"  {fallback!r}")
            continue
        print(
            f"  {fallback.get('eventId', '<unknown>')} "
            f"({fallback.get('field', '<unknown>')}): "
            f"{fallback.get('reason', '<unknown>')}"
        )
    if count:
        print(
            "::error::株式分割補正にフォールバックが発生しました。"
            "決算書類・分割比率・手動登録を確認してください。"
        )
        return 1
    return 0


def main() -> None:
    if len(sys.argv) != 2:
        print(f"使い方: {Path(sys.argv[0]).name} stocks.sqlite")
        raise SystemExit(2)
    raise SystemExit(check(Path(sys.argv[1])))


if __name__ == "__main__":
    main()
