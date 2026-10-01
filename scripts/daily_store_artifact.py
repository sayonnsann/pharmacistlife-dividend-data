"""Choose a recent daily-store run and check its split-event artifact list."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from typing import Any


MAX_RUN_AGE = timedelta(hours=36)
SPLIT_EVENT_ARTIFACT = "split-event-feed"


def _parse_timestamp(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("createdAt must be an ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("createdAt must be an ISO timestamp") from error
    if parsed.tzinfo is None:
        raise ValueError("createdAt must include a timezone")
    return parsed.astimezone(timezone.utc)


def select_recent_successful_run(
    runs: Any,
    *,
    now: datetime | None = None,
    max_age: timedelta = MAX_RUN_AGE,
) -> str | None:
    """Return the newest run ID created within the inclusive age window."""
    if not isinstance(runs, list):
        raise ValueError("run list must be a JSON array")
    reference_time = now or datetime.now(timezone.utc)
    if reference_time.tzinfo is None:
        raise ValueError("now must include a timezone")
    reference_time = reference_time.astimezone(timezone.utc)

    eligible: list[tuple[datetime, str]] = []
    for run in runs:
        if not isinstance(run, dict):
            raise ValueError("each run must be a JSON object")
        run_id = run.get("databaseId")
        if isinstance(run_id, bool) or not isinstance(run_id, (int, str)):
            raise ValueError("run databaseId must be an integer or string")
        run_id_text = str(run_id)
        if not run_id_text.isdigit():
            raise ValueError("run databaseId must contain only digits")
        created_at = _parse_timestamp(run.get("createdAt"))
        age = reference_time - created_at
        if timedelta(0) <= age <= max_age:
            eligible.append((created_at, run_id_text))

    if not eligible:
        return None
    return max(eligible, key=lambda item: item[0])[1]


def has_split_event_artifact(response: Any) -> bool:
    """Return whether the Actions run artifact API lists split-event-feed."""
    pages = response if isinstance(response, list) else [response]
    if not pages:
        raise ValueError("artifact response must contain at least one page")
    for page in pages:
        if not isinstance(page, dict) or not isinstance(page.get("artifacts"), list):
            raise ValueError("each artifact response page must contain an artifacts array")
        for artifact in page["artifacts"]:
            if not isinstance(artifact, dict) or not isinstance(artifact.get("name"), str):
                raise ValueError("each artifact must have a string name")
            if artifact["name"] == SPLIT_EVENT_ARTIFACT:
                return True
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=("select-run", "has-artifact"))
    args = parser.parse_args(argv)

    try:
        document = json.load(sys.stdin)
        if args.operation == "select-run":
            run_id = select_recent_successful_run(document)
            if run_id is not None:
                print(run_id)
        else:
            print("true" if has_split_event_artifact(document) else "false")
    except (json.JSONDecodeError, ValueError) as error:
        print(f"daily-store artifact判定データを解析できません: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
