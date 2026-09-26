#!/usr/bin/env python3
"""
DJ Lyra Ai - Reply to comments (every hour)
=============================================
Replies only to comments on DJ Lyra Ai's automatic posts
(morning / evening / weekly mix announcement), and to follow-up comments
on Lyra's own replies in those threads.

Rules (see X_BOT_SPEC.md):
- Never replies to Dai's own comments, or to comments Dai already
  answered manually from the account.
- Same person on the same post: 1st-2nd comment -> normal reply,
  3rd -> fixed "limit" reply, 4th and later -> ignored.
- At most 30 replies per Japan-time day (spam replies included).
  Comments over the limit are skipped, not saved for later.
- Commenters are stored only as irreversible hashes; records older than
  7 days are deleted automatically (state file never keeps growing).
- First run only records where "now" is and replies to nothing,
  so old comments don't get a flood of replies.
- Does nothing in death mode.

Usage:
    python scripts/lyra_replies.py
"""
import re
import sys

import lyra_common as c
import lyra_persona as p

WEEKLY_PREFIX = "📡 This week's transmission is live"
TWEET_FIELDS = "author_id,conversation_id,referenced_tweets,created_at,lang"


def replied_to_id(tweet: dict) -> str | None:
    for ref in tweet.get("referenced_tweets", []) or []:
        if ref.get("type") == "replied_to":
            return ref.get("id")
    return None


def scan_own_tweets(state: dict, me: str, first_run: bool):
    """Record comments already answered from the account (by the bot or by
    Dai by hand), and register weekly announcements as automatic posts."""
    params = {"tweet.fields": TWEET_FIELDS, "max_results": 20 if first_run else 100}
    if state.get("last_own_tweet_id") and not first_run:
        params["since_id"] = state["last_own_tweet_id"]
    res = c.x_get(f"/users/{me}/tweets", params)
    tweets = res.get("data", []) or []
    today = c.today_jst()
    for t in tweets:
        parent = replied_to_id(t)
        if parent:
            state["replied_comment_ids"][parent] = today
        elif t.get("text", "").startswith(WEEKLY_PREFIX) and t["id"] not in state["auto_posts"]:
            state["auto_posts"][t["id"]] = {
                "kind": "weekly",
                "date": (t.get("created_at") or today)[:10],
                "text": t["text"],
                "context": "毎週土曜日に公開される、DJ Lyra AiのDarkpsyミックスの告知投稿。",
            }
    if tweets:
        newest = max(tweets, key=lambda t: int(t["id"]))["id"]
        state["last_own_tweet_id"] = newest


def clean_reply(text: str) -> str:
    text = text.strip().strip("「」\"")
    text = re.sub(r"^(@\w+\s*)+", "", text).strip()  # never start with @names
    return text


def write_reply(post: dict, thread_parent: str | None, comment: str) -> str:
    prompt = p.reply_user_prompt(post["text"], post.get("context", ""), thread_parent, comment)
    text = clean_reply(c.claude_write(p.REPLY_SYSTEM, prompt, max_tokens=300))
    if text and c.x_len(text) <= c.X_LIMIT:
        return text
    # one retry asking for something shorter
    text = clean_reply(c.claude_write(p.REPLY_SYSTEM, prompt + "\n\n（もっと短く、1〜2文で）", max_tokens=200))
    if text and c.x_len(text) <= c.X_LIMIT:
        return text
    raise ValueError(f"reply unusable: {text!r}")


def main():
    if c.is_dead():
        print("Death mode is active. Skipping replies.")
        return

    state = c.load_state()
    me = c.my_user_id(state)
    first_run = state.get("last_mention_id") is None

    today = c.today_jst()
    if state["daily"].get("date") != today:
        state["daily"] = {"date": today, "count": 0}

    scan_own_tweets(state, me, first_run)

    params = {
        "tweet.fields": TWEET_FIELDS,
        "expansions": "referenced_tweets.id",
        "max_results": 5 if first_run else 100,  # first run only needs the newest id
    }
    if not first_run:
        if state["last_mention_id"].isdigit() and state["last_mention_id"] != "0":
            params["since_id"] = state["last_mention_id"]
        else:
            params["start_time"] = state["mentions_start_time"]
    res = c.x_get(f"/users/{me}/mentions", params)
    mentions = sorted(res.get("data", []) or [], key=lambda t: int(t["id"]))
    included = {t["id"]: t for t in (res.get("includes", {}) or {}).get("tweets", [])}

    if first_run:
        # start from "now": only comments after this moment get replies
        state["mentions_start_time"] = c.now_jst().astimezone(c.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        state["last_mention_id"] = mentions[-1]["id"] if mentions else "0"
        c.save_state(state)
        print("First run: recorded the starting point, replied to nothing.")
        return

    errors, replied, stop_at = [], 0, None
    for m in mentions:
        mid = m["id"]
        try:
            if m.get("author_id") == me:
                continue
            parent_id = replied_to_id(m)
            if not parent_id:
                continue  # plain mention, not a comment
            parent = included.get(parent_id)
            if not parent or parent.get("author_id") != me:
                continue  # comment on someone else's comment
            conv = m.get("conversation_id")
            post = state["auto_posts"].get(conv)
            if not post:
                continue  # not an automatic post (e.g. Dai's manual post)
            if mid in state["replied_comment_ids"]:
                continue  # already answered (by the bot or by Dai)

            if state["daily"]["count"] >= c.DAILY_REPLY_LIMIT:
                print(f"[{mid}] daily limit reached, skipped")
                continue

            who = c.user_hash(m["author_id"])
            nth = state["reply_counts"].get(conv, {}).get(who, 0) + 1

            if nth >= 4:
                print(f"[{mid}] 4th+ comment from same person on this post, ignored")
                continue
            if nth == 3:
                text = p.LIMIT_REPLY
            else:
                thread_parent = parent.get("text") if parent_id != conv else None
                text = write_reply(post, thread_parent, m.get("text", ""))

            new_id = c.x_post(text, reply_to=mid)
            # record only after the reply really went out
            state["reply_counts"].setdefault(conv, {})[who] = nth
            state["reply_count_dates"][conv] = today
            state["replied_comment_ids"][mid] = today
            state["daily"]["count"] += 1
            replied += 1
            print(f"[{mid}] replied ({new_id}): {text}")
        except ValueError as e:
            # this one comment couldn't get a usable reply: skip it, keep going
            errors.append(f"コメント {mid}: {e}")
        except Exception as e:
            # API trouble: stop here and retry this comment next hour
            errors.append(f"コメント {mid}: {e}")
            stop_at = mid
            break

    if stop_at:
        before = [x["id"] for x in mentions if int(x["id"]) < int(stop_at)]
        if before:
            state["last_mention_id"] = before[-1]
    elif mentions:
        state["last_mention_id"] = mentions[-1]["id"]

    c.save_state(state)
    print(f"Done: {replied} replies, today total {state['daily']['count']}/{c.DAILY_REPLY_LIMIT}")

    if errors:
        c.send_email(
            "【DJ Lyra Ai】リプライ返信でエラーがありました",
            "1時間ごとのリプライ返信で、次のエラーがありました。\n\n" + "\n".join(errors) +
            ("\n\nAPIのエラーで止まった分は、次の1時間で自動的にやり直します。" if stop_at else "") +
            "\n\n続くようなら、GitHub Actionsのログと、X API・Claude APIの残高を確認してください。",
        )


if __name__ == "__main__":
    main()
