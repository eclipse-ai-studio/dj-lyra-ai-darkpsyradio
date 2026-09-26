#!/usr/bin/env python3
"""
DJ Lyra Ai - Evening post (17:00 JST)
=======================================
- 10%: one random fixed food/animal line (data/lyra_texts.json)
- 90%: Claude writes a short "ふとした気づき" (small everyday notice),
       avoiding the last 30 notices kept in data/lyra_state.json
- If Claude fails or keeps writing something unusable, falls back to a
  fixed line so the post still goes out, and emails Dai.
- Does nothing in death mode.

Usage:
    python scripts/lyra_evening.py            # post for real
    python scripts/lyra_evening.py --dry-run  # print only (still calls Claude)
"""
import random
import re
import sys

import lyra_common as c
import lyra_persona as p

FIXED_CHANCE = 0.10
MAX_ATTEMPTS = 3
MAX_NOTICE_LEN = 60  # characters; target is ~30

BANNED = ["わかりません", "分かりません", "わからない", "分からない", "何も起きていません", "#", "http"]


def clean(text: str) -> str:
    text = text.strip().strip("「」\"'")
    return re.sub(r"\s*\n\s*", "", text)


def is_usable(text: str, recent: list[str]) -> str | None:
    """Return None if OK, else the reason it's rejected."""
    if not text:
        return "empty"
    if len(text) > MAX_NOTICE_LEN:
        return f"too long ({len(text)})"
    for b in BANNED:
        if b in text:
            return f"contains banned phrase: {b}"
    if text in recent:
        return "same as a recent post"
    return None


def write_notice(recent: list[str]) -> str:
    last_reason = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        text = clean(c.claude_write(p.EVENING_SYSTEM, p.evening_user_prompt(recent), max_tokens=150))
        reason = is_usable(text, recent)
        print(f"[claude] attempt {attempt}: {text!r} -> {reason or 'OK'}")
        if reason is None:
            return text
        last_reason = reason
    raise RuntimeError(f"Claude could not write a usable notice ({last_reason})")


def main():
    dry = "--dry-run" in sys.argv
    if c.is_dead():
        print("Death mode is active. Skipping evening post.")
        return

    texts = c.load_texts()
    fixed_pool = texts["evening_food"] + texts["evening_animal"]
    state = c.load_state()
    recent = state["recent_notices"]

    problem = None
    if random.random() < FIXED_CHANCE:
        post, kind = random.choice(fixed_pool), "fixed"
    else:
        try:
            post, kind = write_notice(recent), "notice"
        except Exception as e:
            problem = str(e)
            post, kind = random.choice(fixed_pool), "fixed (fallback)"

    print(f"[post] {kind} ({c.x_len(post)}/{c.X_LIMIT}): {post}")
    if dry:
        return

    try:
        tweet_id = c.x_post(post)
    except Exception as e:
        c.send_email(
            "【DJ Lyra Ai】夕方の投稿ができませんでした",
            f"夕方17時の投稿に失敗しました。\n\nエラー: {e}\n\n投稿しようとした文:\n{post}\n\n"
            "GitHub Actionsのログを確認して、必要なら Lyra Evening Post を手動で再実行してください。",
        )
        raise

    state["auto_posts"][tweet_id] = {"kind": "evening", "date": c.today_jst(), "text": post, "context": ""}
    if kind == "notice":
        state["recent_notices"].append(post)
    c.save_state(state)
    print(f"[post] OK id={tweet_id}")

    if problem:
        c.send_email(
            "【DJ Lyra Ai】夕方のつぶやきを作れませんでした",
            f"Claudeでつぶやきを作れなかったため、固定文で投稿しました。\n\nエラー: {problem}\n\n投稿した文: {post}\n\n"
            "一時的なものなら、明日は自動で元に戻ります。続くようならClaude APIの残高・キーを確認してください。",
        )


if __name__ == "__main__":
    main()
