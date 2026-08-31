#!/usr/bin/env python3
"""
DJ Lyra Ai - Publish the newest unpublished mix
==================================================
Meant to run on a Saturday schedule (separate from generation, which can
happen any day). Looks at docs/mixes.json:
  - If one or more unpublished mixes exist, publishes only the NEWEST one
    (sets published=True, publish_date=today). Any other unpublished
    entries are left alone (harmless leftovers).
  - If no unpublished mixes exist, does nothing this week (no
    re-announcement of an already-published mix).
Prints whether something was published, so the workflow can decide
whether to trigger the X reminder step.
Usage:
    python scripts/publish_mix.py
"""
import json
import sys
from datetime import date
from pathlib import Path

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
    if not MIXES_JSON_PATH.exists():
        print("mixes.json does not exist. Nothing to publish.")
        print("PUBLISHED=false")
        return

    mixes = json.loads(MIXES_JSON_PATH.read_text())
    unpublished = [m for m in mixes if not m.get("published", False)]

    if not unpublished:
        print("No unpublished mixes found. Nothing to publish this week.")
        print("PUBLISHED=false")
        write_status(
            "publish_skipped",
            True,
            "今週公開対象の未公開ミックスがありませんでした(生成が止まっている可能性があります)",
        )
        return

    # pick the one with the newest date
    newest = max(unpublished, key=lambda m: m["date"])
    for m in mixes:
        if m is newest:
            m["published"] = True
            m["publish_date"] = date.today().isoformat()

    MIXES_JSON_PATH.write_text(json.dumps(mixes, ensure_ascii=False, indent=2))

    print(f"Published: {newest['title']} ({newest['date']})")
    print(f"Audio URL: {newest['audio_url']}")
    print("PUBLISHED=true")
    write_status("publish", True, f"{newest['title']} を公開しました")


if __name__ == "__main__":
    main()
