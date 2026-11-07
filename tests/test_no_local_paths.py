"""公開リポジトリに手元の絶対パス（ユーザー名やドライブ名を含むもの）を入れないための見張り。

git が追跡しているテキストファイルを全部読み、Mac のホーム（/Users/<名前>/）、
Windows のユーザーフォルダ（C:\\Users\\ や C:/Users/）、母艦の作業ドライブ（G:\\ H:\\）、
母艦の作業フォルダ名を探す。1件でも見つかれば失敗する。
"""

from __future__ import annotations

import re
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
THIS_FILE = Path(__file__).resolve()

# 自分自身に当たらないよう、文字を分けて組み立てる。
_USERS = "Us" + "ers"
LOCAL_PATH_PATTERNS = (
    re.compile("/" + _USERS + "/[^/\\s\"']+/"),
    re.compile(r"(?i)\b[a-z]:[\\/]+" + _USERS + r"[\\/]"),
    re.compile(r"(?i)(?<![a-z0-9])[gh]:[\\/]+[a-z0-9_]"),
    re.compile("Claude" + "Code" + r"[\\/]"),
)
BINARY_SUFFIXES = {
    ".sqlite", ".db", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico",
    ".pdf", ".gz", ".zip", ".woff", ".woff2", ".ttf",
}


def tracked_files() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True
    )
    names = [name for name in result.stdout.decode("utf-8").split("\0") if name]
    return [ROOT / name for name in names]


def find_local_paths(text: str) -> list[str]:
    hits = []
    for pattern in LOCAL_PATH_PATTERNS:
        for match in pattern.finditer(text):
            hits.append(match.group(0))
    return hits


class LocalPathPatternTest(unittest.TestCase):
    def test_patterns_catch_local_paths(self) -> None:
        samples = [
            "/" + _USERS + "/someone/workspace/edinet-direct/data/x.json",
            "C:\\" + _USERS + "\\someone\\phaseA\\x.pdf",
            "C:/" + _USERS + "/someone/phaseA/x.pdf",
            "G:\\archive\\x.pdf",
            "H:\\" + "Claude" + "Code\\workspace\\x.json",
        ]
        for sample in samples:
            with self.subTest(sample=sample):
                self.assertTrue(find_local_paths(sample))

    def test_patterns_allow_relative_paths_and_urls(self) -> None:
        samples = [
            "edinet-direct/data/stock_action_ledger.json",
            "https://example.com/" + _USERS.lower() + "/guide",
            "yuho_FY2007.pdf",
            '"ratio": "1:2"',
        ]
        for sample in samples:
            with self.subTest(sample=sample):
                self.assertEqual(find_local_paths(sample), [])


class TrackedFilesHaveNoLocalPathsTest(unittest.TestCase):
    def test_tracked_text_files_have_no_local_paths(self) -> None:
        found = []
        for path in tracked_files():
            if path.resolve() == THIS_FILE or path.suffix.lower() in BINARY_SUFFIXES:
                continue
            if not path.is_file():
                continue
            data = path.read_bytes()
            if b"\0" in data[:8192]:
                continue
            text = data.decode("utf-8", errors="replace")
            for hit in find_local_paths(text):
                found.append(f"{path.relative_to(ROOT)}: {hit}")
        self.assertEqual(found, [], "手元の絶対パスが公開リポジトリに入っている")


if __name__ == "__main__":
    unittest.main()
