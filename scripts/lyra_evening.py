#!/usr/bin/env python3
"""
DJ Lyra Ai - Evening post (around 15:00 JST)
=======================================
- 10%: one random fixed food/animal line (data/lyra_texts.json)
- 90%: Claude writes a short "ふとした気づき" (small everyday notice),
       avoiding the last 30 notices kept in data/lyra_state.json
- If Claude fails or keeps writing something unusable, falls back to a
  fixed line so the post still goes out, and emails the Doctor.
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
MAX_ATTEMPTS = 4
MAX_NOTICE_LEN = 70  # characters; target is ~30 (prompt says at most 50); a bit over is still accepted

SIMILAR_LIMIT = 0.8  # 80% or more alike = treated as a copy
# 2026-11-25 (notices kept saying 「受信ログを見直していたら…」):
RECENT_THEMES_SKIP = 5   # a theme used in the last 5 notices is not picked again
WORD_WINDOW = 7          # look at the last 7 notices...
WORD_LIMIT = 2           # ...a watched word found in 2 or more of them is rested today
OPENING_LEN = 8          # same first 8 characters as one of...
OPENING_WINDOW = 10      # ...the last 10 notices = rejected

# 2026-10-10 the character setting changed (weekend club DJ, not a daily broadcast).
# Notices written before that are forgotten once, so Claude isn't shown the old tone as "recent posts".
NOTICES_RESET = "2026-10-10"


def fresh_notices(state: dict) -> list[str]:
    return state["recent_notices"] if state.get("notices_reset") == NOTICES_RESET else []


def remember_notice(state: dict, post: str):
    if state.get("notices_reset") != NOTICES_RESET:
        state["recent_notices"] = []
        state["notices_reset"] = NOTICES_RESET
    state["recent_notices"].append(post)

BANNED = ["わかりません", "分かりません", "わからない", "分からない", "何も起きていません", "#", "http",
          "判断できません", "不明", "エラー", "故障", "不具合",
          # no daily-broadcast wording (she DJs at a club on weekends), and the club name only when asked
          "今日の放送", "放送中", "放送を終", "放送が終わ", "SPACIA", "スパシア",
          "博士"]  # 2026-10-24: posts never bring up 博士 (only replies, when asked)


def rested_words(recent: list[str]) -> list[str]:
    window = recent[-WORD_WINDOW:]
    return [w for w in p.WATCH_WORDS if sum(w in t for t in window) >= WORD_LIMIT]


def pick_theme(state: dict, rested: list[str]) -> str:
    used = state.get("recent_evening_themes", [])[-RECENT_THEMES_SKIP:]
    themes = [t for t in p.EVENING_THEMES if t not in used]
    if rested:
        themes = [t for t in themes if t != "signals"]  # can't talk about signals without those words
    return random.choice(themes or list(p.EVENING_THEMES))


def remember_theme(state: dict, theme: str):
    state["recent_evening_themes"] = (state.get("recent_evening_themes", []) + [theme])[-10:]


def clean(text: str) -> str:
    text = text.strip().strip("「」\"'")
    return re.sub(r"\s*\n\s*", "", text)


def is_usable(text: str, recent: list[str], rested: list[str] = ()) -> str | None:
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
    for w in rested:
        if w in text:
            return f"rested word: {w}"
    for old in recent[-OPENING_WINDOW:]:
        if text[:OPENING_LEN] == old[:OPENING_LEN]:
            return f"same opening as a recent post: {old}"
    return None


def retry_hint(text: str, reason: str) -> str:
    """Tell Claude why the last try was not used, so the next one fixes it."""
    if reason.startswith("too long"):
        return f"\n\n（さっきの案は{len(text)}字で長すぎました。30字前後、長くても50字以内で、短く書き直してください）"
    if reason.startswith("too close"):
        return "\n\n（さっきの案はお手本や最近のつぶやきに似すぎていました。別の内容で書いてください）"
    if reason.startswith("rested word"):
        return f"\n\n（さっきの案は今日使わない言葉「{reason.split(': ', 1)[1]}」が入っていました。その言葉なしで書き直してください）"
    if reason.startswith("same opening"):
        return "\n\n（さっきの案は最近のつぶやきと書き出しが同じでした。違う書き出しで書いてください）"
    return f"\n\n（さっきの案は使えませんでした: {reason}。ルールを守って書き直してください）"


def write_notice(recent: list[str], theme: str, rested: list[str]) -> str:
    print(f"[claude] theme: {theme}, rested words: {rested or 'none'}")
    prompt = p.evening_user_prompt(recent, theme, rested)
    last_reason = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        text = clean(c.claude_write(p.EVENING_SYSTEM, prompt, max_tokens=150))
        reason = is_usable(text, recent, rested)
        print(f"[claude] attempt {attempt}: {text!r} -> {reason or 'OK'}")
        if reason is None:
            return text
        last_reason = reason
        prompt = p.evening_user_prompt(recent, theme, rested) + retry_hint(text, reason)
    raise ValueError(f"Claude's notices did not pass the checks ({last_reason})")


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
    state = c.load_state()
    recent = fresh_notices(state)
    rested = rested_words(recent)
    theme = pick_theme(state, rested)
    # fixed lines: every one is used once (random order) before any repeats, then a new round
    all_fixed = texts["evening_food"] + texts["evening_animal"]
    fixed_pool = c.fixed_pool(state, "evening", all_fixed)

    problem = None
    if random.random() < FIXED_CHANCE:
        post, kind = random.choice(fixed_pool), "fixed"
    else:
        try:
            post, kind = write_notice(recent, theme, rested), "notice"
        except ValueError as e:
            # Claude answered, but every try broke a rule (too long etc.): no email, just use a fixed line
            print(f"[claude] {e}")
            post, kind = random.choice(fixed_pool), "fixed (Claude's notices rejected)"
        except Exception as e:
            # API trouble (balance, key, outage): worth telling the Doctor
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
            remember_notice(state, post)  # keep it out of future notices either way
            remember_theme(state, theme)
        else:
            state.setdefault("recent_evening_fixed", []).append(post)
            c.remember_fixed(state, "evening", post, all_fixed)
        c.save_state(state)
        print(f"[post] {e}")
        return
    except Exception as e:
        # no email here: the next run (10 minutes later) tries again.
        # If nothing has gone out by the end of the window, lyra_replies.py emails the Doctor.
        c.finish_post(state, "evening", None)
        print(f"[post] failed, the next run will try again: {e}")
        return

    if kind == "notice":
        remember_notice(state, post)
        remember_theme(state, theme)
    else:
        state.setdefault("recent_evening_fixed", []).append(post)
        c.remember_fixed(state, "evening", post, all_fixed)
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
