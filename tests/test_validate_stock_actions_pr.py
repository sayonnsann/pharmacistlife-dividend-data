from __future__ import annotations

import json
from pathlib import Path

import pytest

from validate_stock_actions_pr import (
    ValidationError,
    compare_event_counts,
    validate_changed_paths,
)


def _write_events(path: Path, ids: list[str]) -> None:
    path.write_text(
        json.dumps({"events": [{"eventId": event_id} for event_id in ids]}),
        encoding="utf-8",
    )


def test_normal_pr_passes_with_small_event_change(tmp_path: Path) -> None:
    base = tmp_path / "base.json"
    head = tmp_path / "head.json"
    _write_events(base, ["A", "B"])
    _write_events(head, ["B", "C"])

    assert validate_changed_paths(["data/stock_actions_extracted.json"]) == [
        "data/stock_actions_extracted.json"
    ]
    summary = compare_event_counts(base, head)
    assert (summary["added"], summary["removed"]) == (1, 1)


def test_mixed_file_pr_fails_scope_check() -> None:
    with pytest.raises(ValidationError, match="変更ファイル"):
        validate_changed_paths(
            [
                "data/stock_actions_extracted.json",
                "scripts/build_store.py",
            ]
        )


def test_spike_pr_fails_event_thresholds(tmp_path: Path) -> None:
    base = tmp_path / "base.json"
    head = tmp_path / "head.json"
    _write_events(base, [f"old-{index}" for index in range(11)])
    _write_events(head, [f"new-{index}" for index in range(51)])

    with pytest.raises(ValidationError, match="追加.*51件.*削除.*11件"):
        compare_event_counts(base, head)
