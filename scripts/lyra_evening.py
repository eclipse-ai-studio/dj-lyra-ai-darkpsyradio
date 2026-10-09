#!/usr/bin/env python3
"""
DJ Lyra Ai - Evening post (around 15:00 JST)
=======================================
- 10%: one random fixed food/animal line (data/lyra_texts.json)
- 90%: Claude writes a short "ふとした気づき" (small everyday notice),
       avoiding the last 30 notices kept in data/lyra_state.json
- If Claude fails or keeps writing something unusable, falls back to a
  fixed line so the post still goes out, and emails Dai.
- Does nothing in death mode.
- Started by cron-job.org at 15:00 / 15:30 / 16:00 JST (plus one GitHub
  backup at 16:20). The first run that happens posts; once today's post is
  out, later runs do nothing, so it never posts twice.

Usage:
    python scripts/lyra_evening.py            # post for real
    python scripts/lyra_evening.py --dry-run  # print only (still calls Claude)
"""
import difflib
import random
import re
import sys

import lyra_common as c
import lyra_persona as p

FIXED_CHANCE = 0.10
MAX_ATTEMPTS = 3
MAX_NOTICE_LEN = 55  # characters; target is ~30 (prompt says 50, small margin so near-misses are not rejected)

SIMILAR_LIMIT = 0.8  # 80% or more alike = treated as a copy

BANNED = ["わかりません", "分かりません", "わからない", "分からない", "何も起きていません", "#", "http",
          "判断できません", "不明", "エラー", "故障", "不具合",
          # no daily-broadcast wording (she DJs at a club on weekends), and the club name only when asked
          "今日の放送", "放送中", "放送を終", "放送が終わ", "SPACIA", "スパシア"]


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
    for old in p.EVENING_EXAMPLES:
        if difflib.SequenceMatcher(None, text, old).ratio() >= SIMILAR_LIMIT:
            return f"too close to an example: {old}"
    for old in recent:
        if difflib.SequenceMatcher(None, text, old).ratio() >= SIMILAR_LIMIT:
            return f"too close to a recent post: {old}"
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

    force = "--force" in sys.argv
    if not dry and c.too_late("evening"):
        print("This backup run started too late in the day (GitHub's timer was late). Not posting.")
        return
    if not dry and not force:
        c.sync_repo()
        st = c.load_state()
        c.resolve_unfinished(st, "evening")  # an earlier run never heard back from X?
        if c.already_posted(st, "evening", c.today_jst()):
            print("Today's evening post is already out (or being checked). Nothing to do.")
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
            problem = f"{type(e).__name__}: {e}"
            post, kind = random.choice(fixed_pool), "fixed (fallback)"

    print(f"[post] {kind} ({c.x_len(post)}/{c.X_LIMIT}): {post}")
    if dry:
        return

    record = {"kind": "evening", "date": c.today_jst(), "text": post, "context": ""}
    state = c.begin_post("evening", record, force)
    if state is None:
        return
    try:
        tweet_id = c.x_post(post)
    except c.XPostUncertain as e:
        # X didn't answer clearly: it may be posted. The "posting" flag stays;
        # the next run checks X for this exact text before doing anything.
        if kind == "notice":
            state["recent_notices"].append(post)  # keep it out of future notices either way
            c.save_state(state)
        print(f"[post] {e}")
        return
    except Exception as e:
        # no email here: the next run (10 minutes later) tries again.
        # If nothing has gone out by the end of the window, lyra_replies.py emails Dai.
        c.finish_post(state, "evening", None)
        print(f"[post] failed, the next run will try again: {e}")
        return

    if kind == "notice":
        state["recent_notices"].append(post)
    c.finish_post(state, "evening", tweet_id)
    print(f"[post] OK id={tweet_id}")

    if problem:
        c.send_email(
            "【DJ Lyra Ai】夕方のつぶやきを作れませんでした",
            f"Claudeでつぶやきを作れなかったため、固定文で投稿しました。\n\nエラー: {problem}\n\n投稿した文: {post}\n\n"
            "一時的なものなら、明日は自動で元に戻ります。続くようならClaude APIの残高・キーを確認してください。",
        )


if __name__ == "__main__":
    c.run_main(main, "Lyra Evening Post")
