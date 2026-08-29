#!/usr/bin/env python3
"""
DJ Lyra Ai - Upload weekly mix to Internet Archive
=====================================================
1. Uploads output/weekly_mix.mp3 to Internet Archive (permanent, free storage)
2. Appends the new mix's info to docs/mixes.json (read by the GitHub Pages site)

Requires env vars: IA_ACCESS_KEY, IA_SECRET_KEY

Usage:
    python scripts/upload_to_archive.py
"""

import json
import os
import sys
from datetime import date
from pathlib import Path

import internetarchive as ia

MIX_PATH = Path("output/weekly_mix.mp3")
MIXES_JSON_PATH = Path("docs/mixes.json")
STATUS_PATH = Path("output/pipeline_status.json")


def write_status(stage: str, ok: bool, detail: str):
    STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATUS_PATH.write_text(json.dumps({
        "stage": stage,
        "ok": ok,
        "detail": detail,
    }, ensure_ascii=False, indent=2))


def main():
    access_key = os.environ.get("IA_ACCESS_KEY")
    secret_key = os.environ.get("IA_SECRET_KEY")

    if not access_key or not secret_key:
        write_status("archive_upload", False, "Internet ArchiveのAPIキーが設定されていません")
        sys.exit(1)

    if not MIX_PATH.exists():
        write_status("archive_upload", False, "アップロードするミックスファイルが見つかりません")
        sys.exit(1)

    today = date.today().isoformat()
    identifier = f"dj-lyra-ai-darkpsy-mix-{today}"
    title = f"DJ Lyra Ai - Darkpsy Mix {today}"

    print(f"[upload] identifier: {identifier}")

    try:
        result = ia.upload(
            identifier,
            files={"weekly_mix.mp3": str(MIX_PATH)},
            metadata={
                "title": title,
                "mediatype": "audio",
                "collection": "opensource_audio",
                "creator": "DJ Lyra Ai",
                "subject": "dark psytrance; psytrance; AI music; DJ Lyra Ai; Eclipse Ai Studio",
                "description": (
                    "Autonomous AI-generated dark psytrance mix by DJ Lyra Ai, "
                    "Model-001 of Eclipse Ai Studio. Fully automated, no human DJ."
                ),
            },
            access_key=access_key,
            secret_key=secret_key,
            verbose=True,
        )
    except Exception as e:
        write_status("archive_upload", False, f"Internet Archiveへのアップロードに失敗しました: {e}")
        sys.exit(1)

    if not all(r.status_code == 200 for r in result):
        write_status("archive_upload", False, "Internet Archiveへのアップロードが正常に完了しませんでした")
        sys.exit(1)

    mix_url = f"https://archive.org/details/{identifier}"
    audio_url = f"https://archive.org/download/{identifier}/weekly_mix.mp3"
    print(f"[upload] OK: {mix_url}")

    # --- append to docs/mixes.json for GitHub Pages ---
    MIXES_JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
    if MIXES_JSON_PATH.exists():
        mixes = json.loads(MIXES_JSON_PATH.read_text())
    else:
        mixes = []

    mixes.append({
        "date": today,
        "title": title,
        "archive_url": mix_url,
        "audio_url": audio_url,
    })

    MIXES_JSON_PATH.write_text(json.dumps(mixes, ensure_ascii=False, indent=2))
    print(f"[mixes.json] now has {len(mixes)} mix(es)")

    write_status("complete", True, f"{title} をInternet Archiveに保存し、公開リストを更新しました")


if __name__ == "__main__":
    main()
