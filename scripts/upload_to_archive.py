#!/usr/bin/env python3
"""
DJ Lyra Ai - Upload weekly mix to Internet Archive
=====================================================
1. Uploads output/weekly_mix.mp3 to Internet Archive (permanent, free storage),
   marked noindex so it doesn't show up in Archive.org search results before
   Dai has had a chance to check it / it gets published on the official site
2. Appends the new mix's info to docs/mixes.json, flagged as unpublished
   (a separate publish step decides when it actually goes live, and sets the
   final public-facing title using the publish date)

The Internet Archive identifier includes a time component (not just the date)
so that manually running this more than once on the same day never overwrites
a previous upload.

Requires env vars: IA_ACCESS_KEY, IA_SECRET_KEY

Usage:
    python scripts/upload_to_archive.py
"""

import json
import os
import sys
from datetime import date, datetime
from pathlib import Path

import internetarchive as ia

MIX_PATH = Path("output/weekly_mix.mp3")
MIXES_JSON_PATH = Path("docs/mixes.json")
STATUS_PATH = Path("output/pipeline_status.json")

IDENTIFIER_PREFIX = "dj-lyra-ai-darkpsyradio-mix"
UPLOAD_FILENAME = "djlyraai_weekly_mix.mp3"


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
    now_stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    # identifier includes a time component so same-day re-runs don't overwrite each other
    identifier = f"{IDENTIFIER_PREFIX}-{now_stamp}"
    # This is a placeholder title, only used on Internet Archive's own item
    # page. The public-facing title shown on the site is set later, at
    # publish time, using the actual publish date.
    ia_title = f"DJ Lyra Ai - Darkpsy Mix (generated {today})"

    print(f"[upload] identifier: {identifier}")

    try:
        result = ia.upload(
            identifier,
            files={UPLOAD_FILENAME: str(MIX_PATH)},
            metadata={
                "title": ia_title,
                "mediatype": "audio",
                "collection": "opensource_audio",
                "creator": "DJ Lyra Ai",
                "subject": "dark psytrance; psytrance; AI music; DJ Lyra Ai; Eclipse Ai Studio",
                "description": (
                    "Autonomous AI-generated dark psytrance mix by DJ Lyra Ai, "
                    "Model-001 of Eclipse Ai Studio. Fully automated, no human DJ."
                ),
                "noindex": "true",
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
    audio_url = f"https://archive.org/download/{identifier}/{UPLOAD_FILENAME}"
    print(f"[upload] OK: {mix_url}")

    # --- append to docs/mixes.json, flagged unpublished ---
    # title is a placeholder here; publish_mix.py sets the final public
    # title using the actual publish date once this mix goes live.
    MIXES_JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
    if MIXES_JSON_PATH.exists():
        mixes = json.loads(MIXES_JSON_PATH.read_text())
    else:
        mixes = []

    mixes.append({
        "date": today,
        "title": ia_title,
        "archive_url": mix_url,
        "audio_url": audio_url,
        "published": False,
        "publish_date": None,
    })

    MIXES_JSON_PATH.write_text(json.dumps(mixes, ensure_ascii=False, indent=2))
    print(f"[mixes.json] now has {len(mixes)} mix(es), newest is unpublished")

    write_status(
        "mix_ready",
        True,
        f"新しいミックスをInternet Archiveに保存しました(未公開)。聴いて確認できます: {mix_url}\n"
        f"土曜日に自動で公開されます。気に入らない場合は、もう一度ワークフローを実行して作り直してください。",
    )


if __name__ == "__main__":
    main()
