"""Fetch the last published private store using the existing FTPS transport."""
from __future__ import annotations

import os
import sys
from pathlib import Path

from store_review_inputs import download


def fetch_previous(output):
    host, user, password, remote = [
        os.environ[key] for key in ("FTP_HOST", "FTP_USER", "FTP_PASS", "FTP_REMOTE_DIR")]
    # 急低下の比較用に過ぎないので、取得できなくても本体の構築は止めない。
    if not all((host, user, password, remote)) or "://" in host or "/" in host or not remote.startswith("/"):
        print("::warning::FTPS設定が不正なため、前回公開stocks.sqliteを取得しません。急低下比較なし。")
        return
    status = download(f"ftp://{host}{remote.rstrip('/')}/stocks.sqlite",
                      output, f"{user}:{password}")
    if status == 78:
        print("::warning::前回公開stocks.sqliteなし。初回は急低下比較なし。")
    elif status:
        Path(output).unlink(missing_ok=True)
        print("::warning::前回公開stocks.sqliteの取得に失敗しました。急低下比較なし。")


if __name__ == "__main__":
    fetch_previous(Path(sys.argv[1]))
