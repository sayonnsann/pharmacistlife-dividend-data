"""Auto-deploy guards; never print private commit messages or file contents."""
from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import subprocess

FISCAL_PATH = "edinet-direct/data/fiscal_dividends.json"


def is_nightly_publication(repository: Path) -> bool:
    # Checkout must include history, not just the latest workspace commit.
    message = subprocess.check_output(
        ["git", "-C", str(repository), "log", "-1", "--format=%B", "--", FISCAL_PATH],
        text=True,
        encoding="utf-8",
    )
    return message.startswith("夜間バッチ ")


def same_content(candidate: Path, production: Path | None) -> bool:
    def digest(path: Path) -> bytes:
        hasher = hashlib.sha256()
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                hasher.update(chunk)
        return hasher.digest()

    candidate_hash = digest(candidate)
    if production is None:
        return False
    try:
        return candidate_hash == digest(production)
    except OSError:
        # Missing/unreadable production copies must not block a publication.
        return False


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    origin = subparsers.add_parser("origin")
    origin.add_argument("repository", type=Path)
    compare = subparsers.add_parser("compare")
    compare.add_argument("candidate", type=Path)
    compare.add_argument("production", type=Path, nargs="?")
    args = parser.parse_args()
    if args.command == "origin":
        allowed = is_nightly_publication(args.repository)
        print(f"allowed={str(allowed).lower()}")
        print("reason=" + ("夜間バッチの公開物であることを確認" if allowed else
                           "対象ファイルの最終変更コミットが「夜間バッチ 」で始まらないため送信しません"))
    else:
        identical = same_content(args.candidate, args.production)
        print(f"send={str(not identical).lower()}")
        print("reason=" + ("本番とSHA-256が同一のため送信・再構築を省略" if identical else
                           "本番と内容が異なる、または本番ファイルを取得できないため送信対象"))


if __name__ == "__main__":
    main()
