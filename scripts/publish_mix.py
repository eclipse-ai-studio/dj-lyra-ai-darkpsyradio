#!/usr/bin/env python3
"""
DJ Lyra Ai - Publish the newest unpublished mix
==================================================
Meant to run on a Saturday schedule (separate from generation, which can
happen any day). Looks at docs/mixes.json:
  - If one or more unpublished mixes exist, publishes only the NEWEST one
    (sets published=True, publish_date=today, and rewrites the public-facing
    title to use today's date instead of the generation date). Any other
    unpublished entries are left alone (harmless leftovers).
  - If no unpublished mixes exist, does nothing this week (no
    re-announcement of an already-published mix).
Writes a "published" GitHub Actions output (true/false) so the workflow
can decide whether to trigger the X announcement step.
Usage:
    python scripts/publish_mix.py
"""
import json
import os
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


def write_github_output(published: bool, title: str = "", url: str = ""):
    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a", encoding="utf-8") as f:
            f.write(f"published={'true' if published else 'false'}\n")
            f.write(f"title={title}\n")
            f.write(f"url={url}\n")


def main():
    if not MIXES_JSON_PATH.exists():
        print("mixes.json does not exist. Nothing to publish.")
        write_github_output(published=False)
        return

    mixes = json.loads(MIXES_JSON_PATH.read_text())
    unpublished = [m for m in mixes if not m.get("published", False)]

    if not unpublished:
        print("No unpublished mixes found. Nothing to publish this week.")
        write_status(
            "publish_skipped",
            True,
            "今週公開対象の未公開ミックスがありませんでした(生成が止まっている可能性があります)",
        )
        write_github_output(published=False)
        return

    # pick the one with the newest date (generation date, used only for ordering)
    newest = max(unpublished, key=lambda m: m["date"])
    today = date.today().isoformat()

    # public-facing title uses the publish date, not the generation date
    public_title = f"DJ Lyra Ai - Darkpsy Mix {today}"

    for m in mixes:
        if m is newest:
            m["published"] = True
            m["publish_date"] = today
            m["title"] = public_title

    MIXES_JSON_PATH.write_text(json.dumps(mixes, ensure_ascii=False, indent=2))

    print(f"Published: {public_title}")
    print(f"Audio URL: {newest['audio_url']}")
    write_status("publish", True, f"{public_title} を公開しました")
    write_github_output(published=True, title=public_title)


if __name__ == "__main__":
    main()
