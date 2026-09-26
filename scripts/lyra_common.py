#!/usr/bin/env python3
"""
DJ Lyra Ai - X personality bot: shared helpers
================================================
Used by lyra_morning.py, lyra_evening.py and lyra_replies.py.

- Time helpers (everything is decided in Japan time)
- Death mode check (docs/heartbeat.json)
- Bot state file (data/lyra_state.json) load/save, kept small on purpose
- X API: post / reply (always with the "Made with AI" label) and reads
- Claude API: write one short text
- Gmail: failure notifications (same account/secrets as notify.py)
- X character counting (X counts CJK/emoji as 2, ASCII as 1, limit 280)

This file does not touch the existing weekly-mix pipeline.
"""
import hashlib
import hmac
import json
import os
import smtplib
import sys
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from pathlib import Path

import requests

JST = timezone(timedelta(hours=9))

HEARTBEAT_PATH = Path("docs/heartbeat.json")
STATE_PATH = Path("data/lyra_state.json")
TEXTS_PATH = Path("data/lyra_texts.json")
ECLIPSES_PATH = Path("data/eclipses.json")

X_API = "https://api.x.com/2"
CLAUDE_URL = "https://api.anthropic.com/v1/messages"
CLAUDE_MODEL = os.environ.get("LYRA_MODEL", "claude-haiku-4-5-20251001")

X_LIMIT = 280            # X weighted length limit
RECENT_NOTICES_MAX = 30  # evening "notices" kept for de-duplication
KEEP_DAYS = 7            # reply bookkeeping older than this is dropped
DAILY_REPLY_LIMIT = 30   # replies per Japan-time day


# ---------------------------------------------------------------- time
def now_jst() -> datetime:
    return datetime.now(JST)


def today_jst() -> str:
    return now_jst().strftime("%Y-%m-%d")


# ---------------------------------------------------------------- death mode
def is_dead() -> bool:
    try:
        return bool(json.loads(HEARTBEAT_PATH.read_text())["death_mode"])
    except Exception:
        # If heartbeat can't be read, do NOT post (safer to stay quiet).
        return True


# ---------------------------------------------------------------- state
def default_state() -> dict:
    return {
        "my_user_id": None,
        "auto_posts": {},          # tweet_id -> {kind, date, text, context}
        "recent_notices": [],      # last 30 evening notices written by Claude
        "last_mention_id": None,   # newest mention already handled
        "mentions_start_time": None,  # set on first run (used until the first mention arrives)
        "last_own_tweet_id": None, # newest own tweet already scanned
        "replied_comment_ids": {}, # comment_id -> date (replied by bot or by Dai manually)
        "reply_counts": {},        # conversation_id -> {user_hash: count}
        "reply_count_dates": {},   # conversation_id -> date (for pruning)
        "daily": {"date": None, "count": 0},
    }


def load_state() -> dict:
    state = default_state()
    if STATE_PATH.exists():
        try:
            state.update(json.loads(STATE_PATH.read_text()))
        except Exception:
            pass
    return state


def prune_state(state: dict):
    """Keep the state file small: drop anything older than KEEP_DAYS."""
    cutoff = (now_jst() - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d")
    state["auto_posts"] = {k: v for k, v in state["auto_posts"].items() if v.get("date", "") >= cutoff}
    state["replied_comment_ids"] = {k: d for k, d in state["replied_comment_ids"].items() if d >= cutoff}
    keep_convs = {k for k, d in state["reply_count_dates"].items() if d >= cutoff}
    state["reply_counts"] = {k: v for k, v in state["reply_counts"].items() if k in keep_convs}
    state["reply_count_dates"] = {k: d for k, d in state["reply_count_dates"].items() if k in keep_convs}
    state["recent_notices"] = state["recent_notices"][-RECENT_NOTICES_MAX:]


def save_state(state: dict):
    prune_state(state)
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=1))


def user_hash(author_id: str) -> str:
    """Store commenters only as an irreversible hash (the repo is public)."""
    secret = os.environ.get("X_API_SECRET", "lyra").encode()
    return hmac.new(secret, str(author_id).encode(), hashlib.sha256).hexdigest()[:16]


# ---------------------------------------------------------------- texts
def load_texts() -> dict:
    return json.loads(TEXTS_PATH.read_text())


def load_eclipses() -> dict:
    return json.loads(ECLIPSES_PATH.read_text())


# ---------------------------------------------------------------- X length
def x_len(text: str) -> int:
    """X weighted length: CJK/emoji = 2, Latin/ASCII/newline = 1."""
    n = 0
    for ch in text.replace("️", ""):
        cp = ord(ch)
        if cp <= 4351 or 8192 <= cp <= 8205 or 8208 <= cp <= 8223 or 8242 <= cp <= 8247:
            n += 1
        else:
            n += 2
    return n


# ---------------------------------------------------------------- X API
def _x_auth():
    from requests_oauthlib import OAuth1

    keys = [os.environ.get(k) for k in ("X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_TOKEN_SECRET")]
    if not all(keys):
        raise RuntimeError("X API keys are not set")
    return OAuth1(keys[0], client_secret=keys[1], resource_owner_key=keys[2], resource_owner_secret=keys[3])


def x_post(text: str, reply_to: str | None = None) -> str:
    """Post (or reply) with the Made with AI label. Returns the new tweet id."""
    if x_len(text) > X_LIMIT:
        raise ValueError(f"text too long for X ({x_len(text)}/{X_LIMIT}): {text}")
    body = {"text": text, "made_with_ai": True}
    if reply_to:
        body["reply"] = {"in_reply_to_tweet_id": str(reply_to)}
    r = requests.post(f"{X_API}/tweets", auth=_x_auth(), json=body, timeout=30)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"X post failed: {r.status_code} {r.text}")
    return r.json()["data"]["id"]


def x_get(path: str, params: dict | None = None) -> dict:
    r = requests.get(f"{X_API}{path}", auth=_x_auth(), params=params or {}, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"X GET {path} failed: {r.status_code} {r.text}")
    return r.json()


def my_user_id(state: dict) -> str:
    if not state.get("my_user_id"):
        state["my_user_id"] = x_get("/users/me")["data"]["id"]
    return state["my_user_id"]


# ---------------------------------------------------------------- Claude API
def claude_write(system: str, user: str, max_tokens: int = 300) -> str:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set")
    r = requests.post(
        CLAUDE_URL,
        headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
        json={
            "model": CLAUDE_MODEL,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        },
        timeout=60,
    )
    if r.status_code != 200:
        raise RuntimeError(f"Claude API failed: {r.status_code} {r.text}")
    parts = [b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text"]
    return "".join(parts).strip()


# ---------------------------------------------------------------- Gmail
def send_email(subject: str, body: str):
    address = os.environ.get("GMAIL_ADDRESS")
    app_password = os.environ.get("GMAIL_APP_PASSWORD")
    if not address or not app_password:
        print("GMAIL_ADDRESS / GMAIL_APP_PASSWORD not set; cannot send notification.")
        return
    # tell the workflow's "crashed" step that Dai was already emailed
    Path("lyra_notified.flag").touch()
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = address
    msg["To"] = address
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(address, app_password)
            server.send_message(msg)
        print(f"Notification sent: {subject}")
    except Exception as e:
        print(f"Could not send notification: {e}", file=sys.stderr)
