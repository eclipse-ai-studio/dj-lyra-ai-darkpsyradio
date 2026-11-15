#!/usr/bin/env python3
"""
DJ Lyra Ai - Upload weekly mix to Internet Archive
=====================================================
1. Uploads output/weekly_mix.mp3 to Internet Archive (permanent, free storage),
   marked noindex so it doesn't show up in Archive.org search results before
   The Doctor has had a chance to check it / it gets published on the official site
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
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import internetarchive as ia

MIX_PATH = Path("output/weekly_mix.mp3")
MIXES_JSON_PATH = Path("docs/mixes.json")
STATUS_PATH = Path("output/pipeline_status.json")

JST = timezone(timedelta(hours=9))  # dates and the identifier's time are Japan time (the runner clock is UTC)

IDENTIFIER_PREFIX = "dj-lyra-ai-darkpsyradio-mix"
UPLOAD_FILENAME = "djlyraai_weekly_mix.mp3"

# When Internet Archive is congested ("Please reduce your request rate",
# "total_tasks_queued exceeds global_limit", 503 SlowDown), wait and try again.
# Waits grow: 5, 10, 20, 40, 60, 60 minutes (about 3h15m in total), which still
# fits inside the weekly-mix job's 350-minute limit after the tracks are made.
RETRY_WAITS_MIN = [5, 10, 20, 40, 60, 60]
CONGESTION_WORDS = (
    "reduce your request rate",
    "global_limit",
    "slowdown",
    "slow down",
    "503",
    "overloaded",
    "total_tasks_queued",
)


def write_status(stage: str, ok: bool, detail: str):
    STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATUS_PATH.write_text(json.dumps({
        "stage": stage,
        "ok": ok,
        "detail": detail,
    }, ensure_ascii=False, indent=2))


def is_congestion(text: str) -> bool:
    t = str(text).lower()
    return any(w in t for w in CONGESTION_WORDS)


def s3_overloaded(session, identifier: str, access_key: str) -> bool:
    """Ask Internet Archive whether it is too busy right now. If the check itself fails, assume it's fine."""
    try:
        return bool(session.s3_is_overloaded(identifier=identifier, access_key=access_key))
    except Exception as e:
        print(f"[upload] busy check failed ({e}), trying anyway")
        return False


def main():
    access_key = os.environ.get("IA_ACCESS_KEY")
    secret_key = os.environ.get("IA_SECRET_KEY")

    if not access_key or not secret_key:
        write_status("archive_upload", False, "Internet ArchiveのAPIキーが設定されていません")
        sys.exit(1)

    if not MIX_PATH.exists():
        write_status("archive_upload", False, "アップロードするミックスファイルが見つかりません")
        sys.exit(1)

    now = datetime.now(JST)
    today = now.date().isoformat()
    now_stamp = now.strftime("%Y-%m-%d-%H%M%S")
    # identifier includes a time component so same-day re-runs don't overwrite each other
    identifier = f"{IDENTIFIER_PREFIX}-{now_stamp}"
    # This is a placeholder title, only used on Internet Archive's own item
    # page. The public-facing title shown on the site is set later, at
    # publish time, using the actual publish date.
    ia_title = f"DJ Lyra Ai - Darkpsy Mix (generated {today})"

    print(f"[upload] identifier: {identifier}")

    session = ia.get_session(config={"s3": {"access": access_key, "secret": secret_key}})
    metadata = {
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
    }

    # the same identifier is used for every try, so retries never leave extra empty items behind
    attempts = len(RETRY_WAITS_MIN) + 1
    last_error = ""
    for attempt in range(1, attempts + 1):
        if attempt > 1:
            wait = RETRY_WAITS_MIN[attempt - 2]
            print(f"[upload] Internet Archive is busy, waiting {wait} min before try {attempt}/{attempts}")
            time.sleep(wait * 60)
            if attempt < attempts and s3_overloaded(session, identifier, access_key):
                last_error = "Internet Archive is still overloaded (busy check)"
                print(f"[upload] still busy, skipping try {attempt}/{attempts}")
                continue

        print(f"[upload] try {attempt}/{attempts}")
        try:
            result = ia.upload(
                identifier,
                files={UPLOAD_FILENAME: str(MIX_PATH)},
                metadata=metadata,
                access_key=access_key,
                secret_key=secret_key,
                verbose=True,
            )
        except Exception as e:
            last_error = str(e)
            if is_congestion(last_error):
                print(f"[upload] busy error on try {attempt}/{attempts}: {last_error}")
                continue
            # any other error (wrong key, missing file...) will not fix itself by waiting
            write_status("archive_upload", False, f"Internet Archiveへのアップロードに失敗しました: {e}")
            sys.exit(1)

        bad = [r for r in result if r.status_code != 200]
        if not bad:
            print(f"[upload] uploaded on try {attempt}/{attempts}")
            break
        last_error = f"HTTP {bad[0].status_code}: {getattr(bad[0], 'text', '')[:300]}"
        if bad[0].status_code == 503 or is_congestion(last_error):
            print(f"[upload] busy response on try {attempt}/{attempts}: {last_error}")
            continue
        write_status("archive_upload", False, "Internet Archiveへのアップロードが正常に完了しませんでした")
        sys.exit(1)
    else:
        write_status(
            "archive_upload",
            False,
            f"Internet Archiveへのアップロードに失敗しました({attempts}回試しましたが混雑が続きました): {last_error}",
        )
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

    # a new mix replaces any mix still waiting to be published (e.g. the Doctor remade it):
    # the older ones are marked so they are never published, not even later
    for m in mixes:
        if not m.get("published", False) and not m.get("superseded", False):
            m["superseded"] = True
            print(f"[mixes.json] older unpublished mix replaced: {m.get('archive_url')}")

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

    notes_path = Path("output/mix_notes.txt")  # written by build_weekly_mix.py (trimmed silence etc.)
    notes = f"\n\n【ミックスのメモ】\n{notes_path.read_text().strip()}" if notes_path.exists() else ""
    write_status(
        "mix_ready",
        True,
        f"新しいミックスをInternet Archiveに保存しました(未公開)。聴いて確認できます: {mix_url}\n"
        f"土曜日に自動で公開されます。気に入らない場合は、もう一度ワークフローを実行して作り直してください。"
        + notes,
    )


if __name__ == "__main__":
    main()
