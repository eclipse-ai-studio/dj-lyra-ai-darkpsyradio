#!/usr/bin/env python3
"""
DJ Lyra Ai - Remove a mix entry from mixes.json
==================================================
Removes an entry from docs/mixes.json by date, so a rejected/redone mix
doesn't get accidentally published. Run this locally or via a GitHub
Actions manual workflow, after deleting the corresponding item on
Internet Archive.

Usage:
    python scripts/remove_mix.py --date 2026-09-06
    python scripts/remove_mix.py --list   (just show current entries, no changes)
"""

import argparse
import json
from pathlib import Path

MIXES_JSON_PATH = Path("docs/mixes.json")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", help="Date of the mix to remove, e.g. 2026-09-06")
    parser.add_argument("--list", action="store_true", help="Just list current entries")
    args = parser.parse_args()

    if not MIXES_JSON_PATH.exists():
        print("mixes.json does not exist yet.")
        return

    mixes = json.loads(MIXES_JSON_PATH.read_text())

    if args.list or not args.date:
        for m in mixes:
            print(f"{m['date']} | published={m.get('published', False)} | {m['title']} | {m['archive_url']}")
        if not args.date:
            print("\nPass --date YYYY-MM-DD to remove an entry.")
        return

    before = len(mixes)
    mixes = [m for m in mixes if m["date"] != args.date]
    after = len(mixes)

    if before == after:
        print(f"No entry found with date {args.date}. Nothing removed.")
        return

    MIXES_JSON_PATH.write_text(json.dumps(mixes, ensure_ascii=False, indent=2))
    print(f"Removed entry for {args.date}. {before} -> {after} entries remaining.")
    print("Don't forget to also delete the item on Internet Archive, and commit this change.")


if __name__ == "__main__":
    main()
