#!/usr/bin/env python3
"""日次/株価更新と同じFTPS入力・株価配信元をランナー一時領域に取得する。"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path

import build_store as store


def download(url, output, credentials=None):
    args = ["curl", "--fail", "--silent", "--show-error", "--location", "--max-time", "90", "--retry", "2"]
    if credentials:
        # FTPS取得: 既存ワークフローと同じ暗号化必須・証明書検証あり。
        args.extend(["--ssl-reqd", "--user", credentials])
    args.extend(["--output", str(output), url])
    # curlの応答やエラー本文にも私的な情報が含まれ得るため表示しない。
    result = subprocess.run(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    if result.returncode:
        output.unlink(missing_ok=True)
    return result.returncode


def prepare(directory):
    directory.mkdir(parents=True, exist_ok=True)
    host, user, password, remote = [os.environ[key] for key in ("FTP_HOST", "FTP_USER", "FTP_PASS", "FTP_REMOTE_DIR")]
    if not all([host, user, password, remote]) or "://" in host or "/" in host or not remote.startswith("/"):
        raise ValueError("FTPSの設定が不正です")
    base = f"ftp://{host}{remote.rstrip('/')}/"
    credentials = f"{user}:{password}"
    for filename, optional in [("fiscal_dividends.json", False), ("forecasts_state.json", False),
                               ("calendar_dividends_frozen.json", True)]:
        path = directory / filename
        status = download(base + filename, path, credentials)
        if filename == "forecasts_state.json" and status == 78:
            # price-only-storeと同じ、state未作成の場合の空予想。
            # load_forecastsは「ファイルなし」を空予想として扱う。
            path.unlink(missing_ok=True)
        elif status and not optional:
            raise RuntimeError("画面データの入力取得に失敗しました")
        if path.exists():
            document = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(document, dict):
                raise ValueError("入力のJSON形式が不正です")
            if filename == "fiscal_dividends.json" and len(document) < 3000:
                raise ValueError("事業年度の配当系列が不足しています")
            if filename == "calendar_dividends_frozen.json" and not document.get("stocks"):
                raise ValueError("暦年の凍結スナップショットが空です")
            if filename == "forecasts_state.json" and not isinstance(document.get("stocks"), dict):
                raise ValueError("配当予想の入力形式が不正です")
    # price-only-store --prices-no-cache と同じ配信元。2ビルドで再取得しない。
    public_base = store.DAILY_PRICE_CSV_URL_NO_CACHE.removesuffix("database.csv")
    for filename, optional in [("database.csv", False), ("split_adjustments.json", False),
                               ("price_update_meta.json", True)]:
        status = download(public_base + filename, directory / filename)
        if status and not optional:
            raise RuntimeError("株価・分割記録の取得に失敗しました")
    store.parse_yield_split_adjustments(json.loads((directory / "split_adjustments.json").read_text()),
                                       allow_missing_active=True)
    meta = directory / "price_update_meta.json"
    if meta.exists():
        try:
            document = json.loads(meta.read_text())
            if not isinstance(document, dict):
                raise ValueError("補助ファイルの形式が不正です")
        except (ValueError, UnicodeError):
            # 本番と同じフォールバック。要約にも「未確認」と明記する。
            meta.unlink()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    try:
        prepare(args.directory)
    except Exception:
        raise SystemExit("比較用の入力取得・検証に失敗しました（非公開の応答内容は表示しません）") from None


if __name__ == "__main__":
    main()
