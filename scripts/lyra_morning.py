#!/usr/bin/env python3
"""
DJ Lyra Ai - Morning post (08:00 JST, "宇宙天気予報")
======================================================
Builds the morning post without Claude:

    今日の太陽フレア予報☀️
      R1-R2: ◯%
      R3: ◯%

    今日のエクリプス情報✨          <- only on eclipse days (data/eclipses.json)
      (地域): (種類)

    (one random encouragement line)

- Flare numbers come from NOAA SWPC 3-day forecast (no key needed).
  The forecast columns are UTC dates; we use the column for today's
  Japan-time date.
- If the flare forecast can't be read: post without it
  (eclipse + encouragement, or encouragement only) and email Dai.
- Does nothing in death mode.

Usage:
    python scripts/lyra_morning.py            # post for real
    python scripts/lyra_morning.py --dry-run  # print only, no post/email
"""
import random
import re
import sys
from datetime import datetime

import requests

import lyra_common as c

NOAA_URL = "https://services.swpc.noaa.gov/text/3-day-forecast.txt"


def parse_flare_forecast(text: str, jst_date: str) -> tuple[str, str]:
    """Return (R1-R2 %, R3 %) for the given Japan date, e.g. ('10', '1')."""
    lines = text.splitlines()
    start = next(i for i, l in enumerate(lines) if "Radio Blackout Forecast" in l)
    header_idx = None
    for i in range(start + 1, min(start + 6, len(lines))):
        if re.search(r"[A-Z][a-z]{2}\s+\d{1,2}", lines[i]) and "R1" not in lines[i]:
            header_idx = i
            break
    if header_idx is None:
        raise ValueError("forecast header not found")
    cols = [f"{m} {int(d):02d}" for m, d in re.findall(r"([A-Z][a-z]{2})\s+(\d{1,2})", lines[header_idx])]
    want = datetime.strptime(jst_date, "%Y-%m-%d").strftime("%b %d")
    if want not in cols:
        raise ValueError(f"{want} not in forecast columns {cols}")
    idx = cols.index(want)

    def row(prefix: str) -> str:
        for l in lines[header_idx + 1: header_idx + 6]:
            if l.strip().startswith(prefix):
                vals = re.findall(r"(\d{1,3})%", l)
                if len(vals) != len(cols):
                    raise ValueError(f"unexpected row: {l}")
                return vals[idx]
        raise ValueError(f"row {prefix} not found")

    return row("R1-R2"), row("R3")


def build_post(flare: tuple[str, str] | None, eclipse: dict | None, encouragement: str) -> str:
    blocks = []
    if flare:
        blocks.append(f"今日の太陽フレア予報☀️\n  R1-R2: {flare[0]}%\n  R3: {flare[1]}%")
    if eclipse:
        blocks.append(eclipse["text"])
    blocks.append(encouragement)
    return "\n\n".join(blocks)


def main():
    dry = "--dry-run" in sys.argv
    if c.is_dead():
        print("Death mode is active. Skipping morning post.")
        return

    date = c.today_jst()
    texts = c.load_texts()
    eclipse = c.load_eclipses().get(date)
    encouragement = random.choice(texts["morning_encouragement"])

    flare, flare_error = None, None
    try:
        resp = requests.get(NOAA_URL, timeout=30)
        resp.raise_for_status()
        flare = parse_flare_forecast(resp.text, date)
    except Exception as e:
        flare_error = str(e)
        print(f"[flare] could not read forecast: {e}")

    post = build_post(flare, eclipse, encouragement)
    print(f"[post] ({c.x_len(post)}/{c.X_LIMIT})\n{post}")
    if dry:
        return

    state = c.load_state()
    try:
        tweet_id = c.x_post(post)
    except Exception as e:
        c.send_email(
            "【DJ Lyra Ai】朝の投稿ができませんでした",
            f"朝8時の投稿に失敗しました。\n\nエラー: {e}\n\n投稿しようとした文:\n{post}\n\n"
            "GitHub Actionsのログを確認して、必要なら Lyra Morning Post を手動で再実行してください。",
        )
        raise

    context = []
    if flare:
        context.append(f"今日の太陽フレア予報（NOAA）: R1-R2 {flare[0]}%、R3 {flare[1]}%")
    if eclipse:
        context.append(f"今日の{eclipse['type']}: 見える地域 {eclipse['regions']}、最大になる時刻（日本時間）{eclipse['greatest_jst']}")
    state["auto_posts"][tweet_id] = {"kind": "morning", "date": date, "text": post, "context": "\n".join(context)}
    c.save_state(state)
    print(f"[post] OK id={tweet_id}")

    if flare_error:
        c.send_email(
            "【DJ Lyra Ai】太陽フレア予報を取得できませんでした",
            f"NOAAの太陽フレア予報を読み取れなかったため、今朝はフレア予報なしで投稿しました。\n\n"
            f"エラー: {flare_error}\n\n投稿した文:\n{post}\n\n"
            "一時的なものなら、明日は自動で元に戻ります。続くようならGitHub Actionsのログを確認してください。",
        )


if __name__ == "__main__":
    main()
