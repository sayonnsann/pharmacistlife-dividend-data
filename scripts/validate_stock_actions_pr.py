#!/usr/bin/env python3
"""自動株式分割PRの安全弁を、Actionsとローカルテストで共有する。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Iterable


TARGET_PATH = "data/stock_actions_extracted.json"
MAX_ADDED_EVENTS = 50
MAX_REMOVED_EVENTS = 10


class ValidationError(ValueError):
    """PR検証で自動マージを止める入力エラー。"""


def _event_map(path: Path) -> dict[str, dict[str, Any]]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValidationError(f"JSONを読めません: {path}: {exc}") from exc
    events = document.get("events") if isinstance(document, dict) else None
    if not isinstance(events, list):
        raise ValidationError(f"events配列がありません: {path}")

    result: dict[str, dict[str, Any]] = {}
    for index, event in enumerate(events):
        if not isinstance(event, dict) or not isinstance(event.get("eventId"), str):
            raise ValidationError(f"{path}: events[{index}].eventIdが不正です")
        event_id = event["eventId"]
        if event_id in result:
            raise ValidationError(f"{path}: eventIdが重複しています: {event_id}")
        result[event_id] = event
    return result


def validate_changed_paths(
    paths: Iterable[str], target_path: str = TARGET_PATH
) -> list[str]:
    """変更ファイルがターゲット1ファイルだけであることを検証する。"""
    normalized = sorted({path.strip() for path in paths if path.strip()})
    if normalized != [target_path]:
        details = ", ".join(normalized) if normalized else "（変更なし）"
        raise ValidationError(
            f"変更ファイルは {target_path} のみでなければなりません: {details}"
        )
    return normalized


def compare_event_counts(base_path: Path, head_path: Path) -> dict[str, Any]:
    """mainとの差分件数を返し、急変ならValidationErrorにする。"""
    base = _event_map(base_path)
    head = _event_map(head_path)
    added_ids = sorted(set(head) - set(base))
    removed_ids = sorted(set(base) - set(head))
    result = {
        "baseCount": len(base),
        "headCount": len(head),
        "added": len(added_ids),
        "removed": len(removed_ids),
        "addedEventIds": added_ids,
        "removedEventIds": removed_ids,
    }
    errors: list[str] = []
    if len(added_ids) > MAX_ADDED_EVENTS:
        errors.append(
            f"イベント追加が閾値を超えています: {len(added_ids)}件"
            f"（許容: {MAX_ADDED_EVENTS}件）"
        )
    if len(removed_ids) > MAX_REMOVED_EVENTS:
        errors.append(
            f"イベント削除が閾値を超えています: {len(removed_ids)}件"
            f"（許容: {MAX_REMOVED_EVENTS}件）"
        )
    if errors:
        raise ValidationError("; ".join(errors))
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-file", type=Path, required=True)
    parser.add_argument("--head-file", type=Path, required=True)
    parser.add_argument("--changed-files-file", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        paths = args.changed_files_file.read_text(encoding="utf-8").splitlines()
        validate_changed_paths(paths)
        summary = compare_event_counts(args.base_file, args.head_file)
    except (OSError, ValidationError) as exc:
        print(f"::error::{exc}")
        return 1

    print(
        "株式分割PR検証OK: "
        f"main {summary['baseCount']}件 -> PR {summary['headCount']}件、"
        f"追加 {summary['added']}件、削除 {summary['removed']}件"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
