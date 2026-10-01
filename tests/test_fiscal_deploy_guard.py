from __future__ import annotations

from pathlib import Path
import subprocess
import sys

import pytest

from scripts.fiscal_deploy_guard import FISCAL_PATH, is_nightly_publication, same_content


@pytest.mark.parametrize("message,expected", [
    ("夜間バッチ 2026-10-01: 取得0件・反映331銘柄", True),
    ("夜間バッチ test\n\nadditional details", True),
    ("手動修正", False),
    ("fix: 夜間バッチ test", False),
    ("夜間バッチ", False),
    (" 夜間バッチ test", False),
])
def test_origin_checks_file_history_not_workspace_head(tmp_path, message, expected):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    # Synthetic history, without staging anything in the working repository.
    def commit(subject, path, content, parent=""):
        return (
            b"commit refs/heads/main\n"
            b"committer Test <test@example.invalid> 1700000000 +0000\n"
            + f"data {len(subject.encode())}\n{subject}\n".encode()
            + (f"from {parent}\n".encode() if parent else b"")
            + f"M 100644 inline {path}\ndata {len(content)}\n".encode()
            + content + b"\n\n"
        )
    history = commit(message, FISCAL_PATH, b"{}")
    history += commit("Unrelated latest commit", "README.md", b"test")
    subprocess.run(["git", "-C", str(tmp_path), "fast-import", "--quiet"],
                   input=history, check=True, capture_output=True)
    subprocess.run(["git", "-C", str(tmp_path), "symbolic-ref", "HEAD", "refs/heads/main"],
                   check=True)
    assert is_nightly_publication(tmp_path) is expected
    # A later non-batch modification to the target must block publication.
    subprocess.run(["git", "-C", str(tmp_path), "fast-import", "--quiet"],
                   input=commit("Manual correction", FISCAL_PATH, b'{"changed":1}', "refs/heads/main^0"),
                   check=True, capture_output=True)
    assert not is_nightly_publication(tmp_path)


def test_no_file_history_is_not_a_publication(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    # An invalid/unavailable history must fail rather than approve deployment.
    with pytest.raises(subprocess.CalledProcessError):
        is_nightly_publication(tmp_path)


@pytest.mark.parametrize("production,expected", [
    (b'{"value":1}', True),
    (b'{"value":2}', False),  # Same length, different bytes.
    (b'{ "value":1 }', False),  # Compare bytes, not JSON semantics.
    (b"", False),
    (None, False),
])
def test_hash_comparison(tmp_path, production, expected):
    candidate = tmp_path / "candidate.json"
    candidate.write_bytes(b'{"value":1}')
    current = tmp_path / "production.json"
    if production is not None:
        current.write_bytes(production)
    assert same_content(candidate, current) is expected


def test_unavailable_download_and_candidate(tmp_path):
    candidate = tmp_path / "candidate.json"
    candidate.write_bytes(b"{}")
    assert not same_content(candidate, None)
    assert not same_content(candidate, tmp_path)  # Unreadable as a file.
    with pytest.raises(FileNotFoundError):
        same_content(tmp_path / "missing.json", None)


def test_compare_cli_outputs_for_actions(tmp_path):
    candidate = tmp_path / "candidate.json"
    candidate.write_bytes(b"{}")
    script = Path(__file__).resolve().parents[1] / "scripts/fiscal_deploy_guard.py"
    for args, expected in [([str(candidate)], "send=false"), ([], "send=true")]:
        result = subprocess.run([sys.executable, str(script), "compare", str(candidate), *args],
                                check=True, text=True, capture_output=True)
        assert result.stdout.splitlines()[0] == expected
        assert result.stdout.splitlines()[1].startswith("reason=")
