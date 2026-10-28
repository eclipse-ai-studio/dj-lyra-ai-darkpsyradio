#!/usr/bin/env python3
"""
DJ Lyra Ai - Morning post (around 07:00 JST, "宇宙天気予報")
======================================================
    今日の宇宙天気予報☀️
    太陽フレア: R1-R2 ◯% / R3 ◯%
    磁気嵐: G1〜G5 / なし
    月: 満月🌕（スーパームーン）
    流星群: ◯◯流星群（極大・北半球向き）   <- only on peak days (data/sky.json)

    今日のエクリプス情報✨                  <- only on eclipse days (data/eclipses.json)
      (地域): (種類)

    (encouragement line)

- Forecast first: the forecast part is built first, and the encouragement
  line is added only if it still fits in X's 280 limit (otherwise it is
  left out). Checked for every day until 2100: the forecast part alone
  never goes over.
- Flare + geomagnetic storm come from NOAA SWPC 3-day forecast (no key).
  The forecast columns are UTC dates; we use the column for today's
  Japan-time date (same for both).
- Moon phase, supermoon and meteor showers come from data/sky.json, made
  once by astronomical calculation (checked against NAOJ's 2026 table).
- Encouragement: 90% Claude writes it, 10% one of the 50 fixed lines.
  If Claude fails, a fixed line is used.
- If part of the information can't be made: post without that part and
  email Dai (once a day).
- Does nothing in death mode.
- Started by cron-job.org at 07:00 / 07:30 / 08:00 / 08:30 JST (plus one
  GitHub backup at 08:20). The first run that happens posts; once today's
  post is out, later runs do nothing, so it never posts twice.

- Picture (2026-10): each morning a "宇宙天気予報お姉さん" picture of Lyra is
  made (scripts/lyra_image.py) and attached. It starts from one of 4 base
  pictures (a different one from yesterday's); on the 1st of every month she
  wears her signature purple outfit. If no picture can be made (or its
  upload fails), that day's base picture is attached instead.
  Key / credit / verification trouble is emailed once a day.

Usage:
    python scripts/lyra_morning.py               # post for real
    python scripts/lyra_morning.py --dry-run     # print only, no post/email/picture (still calls Claude)
    python scripts/lyra_morning.py --image-test  # only make a picture -> output/morning_image.jpg (no post)
"""
import difflib
import os
import random
import re
import sys
from datetime import datetime, timedelta

import requests

import lyra_common as c
import lyra_image
import lyra_persona as p

NOAA_URL = "https://services.swpc.noaa.gov/text/3-day-forecast.txt"

FIXED_CHANCE = 0.10   # 10% fixed line, 90% Claude (same as the evening post)
MAX_ATTEMPTS = 3
MAX_LINE_LEN = 25     # characters (prompt says ~20, at most 25)
LINE_ROOM = MAX_LINE_LEN * 2 + 2  # weighted length a line can need, incl. the blank line before it
SIMILAR_LIMIT = 0.8   # 80% or more alike = treated as a copy
BANNED = ["わかりません", "分かりません", "わからない", "分からない", "#", "http",
          "判断できません", "不明", "エラー", "故障", "不具合",
          "SPACIA", "スパシア", "放送", "フレア", "磁気嵐", "流星", "日食", "月食", "天気", "雨", "晴れ", "寒", "暑",
          "博士"]  # 2026-10-24: posts never bring up 博士 (only replies, when asked)

MOON_EMOJI = {"新月": "🌑", "上弦の月": "🌓", "満月": "🌕", "下弦の月": "🌗"}
# days between two of the four main phases (named after the phase that came before)
MOON_BETWEEN = {"新月": "三日月🌒", "上弦の月": "満月前🌔", "満月": "満月後🌖", "下弦の月": "新月前🌘"}


# ---------------------------------------------------------------- NOAA
def _date_columns(lines: list[str], start_marker: str) -> tuple[int, list[str]]:
    start = next(i for i, l in enumerate(lines) if start_marker in l)
    for i in range(start + 1, min(start + 6, len(lines))):
        if re.search(r"[A-Z][a-z]{2}\s+\d{1,2}", lines[i]) and "R1" not in lines[i] and "UT" not in lines[i]:
            cols = [f"{m} {int(d):02d}" for m, d in re.findall(r"([A-Z][a-z]{2})\s+(\d{1,2})", lines[i])]
            return i, cols
    raise ValueError(f"date header not found after '{start_marker}'")


def _column(cols: list[str], jst_date: str) -> int:
    want = datetime.strptime(jst_date, "%Y-%m-%d").strftime("%b %d")
    if want not in cols:
        raise ValueError(f"{want} not in forecast columns {cols}")
    return cols.index(want)


def parse_flare_forecast(text: str, jst_date: str) -> tuple[str, str]:
    """Return (R1-R2 %, R3 %) for the given Japan date, e.g. ('10', '1')."""
    lines = text.splitlines()
    header_idx, cols = _date_columns(lines, "Radio Blackout Forecast")
    idx = _column(cols, jst_date)

    def row(prefix: str) -> str:
        for l in lines[header_idx + 1: header_idx + 6]:
            if l.strip().startswith(prefix):
                vals = re.findall(r"(\d{1,3})%", l)
                if len(vals) != len(cols):
                    raise ValueError(f"unexpected row: {l}")
                return vals[idx]
        raise ValueError(f"row {prefix} not found")

    return row("R1-R2"), row("R3")


def parse_storm_forecast(text: str, jst_date: str) -> tuple[int, float]:
    """Return (G level 0-5, max Kp) for the given Japan date from the
    'NOAA Kp index breakdown' table (eight 3-hour rows, one column per day).
    Kp is given in thirds (4.67 = 5-); like NOAA, the nearest whole number
    decides the G level (4.67 -> G1, 5.67 -> G2). A '(G2)' note in the
    table is also honored."""
    lines = text.splitlines()
    header_idx, cols = _date_columns(lines, "Kp index breakdown")
    idx = _column(cols, jst_date)
    kps, notes = [], []
    for l in lines[header_idx + 1: header_idx + 14]:
        m = re.match(r"\s*\d{2}-\d{2}UT\s+(.*)$", l)
        if not m:
            continue
        cells = re.findall(r"(\d+(?:\.\d+)?)(?:\s*\(G(\d)\))?", m.group(1))
        if len(cells) != len(cols):
            raise ValueError(f"unexpected Kp row: {l}")
        kp, note = cells[idx]
        kps.append(float(kp))
        if note:
            notes.append(int(note))
    if len(kps) != 8:
        raise ValueError(f"expected 8 Kp rows, found {len(kps)}")
    kp_max = max(kps)
    level = int(kp_max + 0.5) - 4   # nearest whole Kp: 5 -> G1 ... 9 -> G5
    level = max([level] + notes)
    return max(0, min(5, level)), kp_max


# ---------------------------------------------------------------- sky
def moon_today(sky: dict, date: str) -> tuple[str, dict | None]:
    """Return (text after '月: ', the main-phase event if today has one)."""
    ev = sky["moon"].get(date)
    if ev:
        name = ev["phase"] + MOON_EMOJI[ev["phase"]]
        if ev.get("supermoon"):
            name += "（スーパームーン）"
        return name, ev
    d = datetime.strptime(date, "%Y-%m-%d")
    for back in range(1, 10):  # main phases are ~7.4 days apart
        prev = sky["moon"].get((d - timedelta(days=back)).strftime("%Y-%m-%d"))
        if prev:
            if prev["phase"] == "新月" and back == 1:
                return "新月後🌒", None  # the day after new moon: too thin to call 三日月 yet
            return MOON_BETWEEN[prev["phase"]], None
    raise ValueError(f"no moon data near {date}")


# ---------------------------------------------------------------- encouragement
def clean(text: str) -> str:
    text = text.strip().strip("「」\"'")
    return re.sub(r"\s*\n\s*", "", text)


def is_usable(text: str, examples: list[str], recent: list[str]) -> str | None:
    """Return None if OK, else the reason it's rejected."""
    if not text:
        return "empty"
    if len(text) > MAX_LINE_LEN:
        return f"too long ({len(text)})"
    if re.search(r"[\U0001F000-\U0001FFFF☀-➿]", text):
        return "contains an emoji"
    for b in BANNED:
        if b in text:
            return f"contains banned phrase: {b}"
    for old in examples:
        if difflib.SequenceMatcher(None, text, old).ratio() >= SIMILAR_LIMIT:
            return f"too close to an example: {old}"
    for old in recent:
        if difflib.SequenceMatcher(None, text, old).ratio() >= SIMILAR_LIMIT:
            return f"too close to a recent line: {old}"
    return None


def write_line(examples: list[str], recent: list[str]) -> str:
    system = p.morning_system(examples)
    last_reason = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        text = clean(c.claude_write(system, p.morning_user_prompt(recent), max_tokens=100))
        reason = is_usable(text, examples, recent)
        print(f"[claude] attempt {attempt}: {text!r} -> {reason or 'OK'}")
        if reason is None:
            return text
        last_reason = reason
    raise ValueError(f"Claude's lines did not pass the checks ({last_reason})")


def pick_line(fixed: list[str], recent: list[str], fresh: list[str]) -> tuple[str, str, str | None]:
    """Return (line, kind, problem). fresh = fixed lines not used yet this round
    (every fixed line is used once, in random order, before any repeats)."""
    if random.random() < FIXED_CHANCE:
        return random.choice(fresh), "fixed", None
    try:
        return write_line(fixed, recent), "claude", None
    except ValueError as e:
        # Claude answered, but 3 lines in a row didn't pass the checks: normal now and then, no email
        print(f"[line] {e}")
        return random.choice(fresh), "fixed (Claude's lines rejected)", None
    except Exception as e:
        # API trouble (balance, key, outage): worth telling Dai
        return random.choice(fresh), "fixed (fallback)", f"{type(e).__name__}: {e}"


# ---------------------------------------------------------------- post
def build_forecast(flare, storm, moon: str | None, meteor: dict | None) -> str:
    lines = []
    if flare:
        lines.append(f"太陽フレア: R1-R2 {flare[0]}% / R3 {flare[1]}%")
    if storm is not None:
        lines.append(f"磁気嵐: {'G' + str(storm[0]) if storm[0] else 'なし'}")
    if moon:
        lines.append(f"月: {moon}")
    if meteor:
        lines.append(f"流星群: {meteor['name']}（極大・{meteor['hemisphere']}）")
    if not lines:
        return ""
    return "今日の宇宙天気予報☀️\n" + "\n".join(lines)


def build_post(forecast: str, eclipse: dict | None, line: str | None) -> str:
    blocks = [b for b in (forecast, eclipse["text"] if eclipse else "") if b]
    if line:
        blocks.append(line)
    return "\n\n".join(blocks)


def room_for_line(forecast: str, eclipse: dict | None) -> bool:
    """Forecast first: is there room for the longest possible line?"""
    return c.x_len(build_post(forecast, eclipse, None)) + LINE_ROOM <= c.X_LIMIT


def image_test():
    """Make one picture (with the checks) and save it for Dai to look at. Nothing is posted or saved to state."""
    from pathlib import Path
    out = Path("output")
    out.mkdir(exist_ok=True)
    st = c.load_state()
    recent = st.get("recent_image_looks", [])
    args = sys.argv[1:]
    # --base N: test one base (1-4); otherwise a random one (not yesterday's)
    try:
        base = int(args[args.index("--base") + 1])
        assert base in lyra_image.BASES
    except (ValueError, IndexError, AssertionError):
        base = lyra_image.pick_base(st.get("last_image_base"))
    # --signature: test the 1st-of-the-month signature outfit
    signature = "--signature" in args or lyra_image.is_signature_day(c.today_jst())
    try:
        img, look, log = lyra_image.make_morning_image(recent, base, signature)
    except lyra_image.ImageFatal as e:
        log, img, look = [f"FATAL: {e}"], None, None
    report = "\n".join(log) + (f"\n\nlook: {look}" if look else "\n\nno picture passed")
    (out / "morning_image_log.txt").write_text(report + "\n")
    print(report)
    if img:
        (out / "morning_image.jpg").write_bytes(img)
        print(f"saved output/morning_image.jpg ({len(img)} bytes)")


def main():
    if "--image-test" in sys.argv:
        image_test()
        return
    dry = "--dry-run" in sys.argv
    if c.is_dead():
        print("Death mode is active. Skipping morning post.")
        return

    date = c.today_jst()
    force = "--force" in sys.argv
    if not dry and c.too_late("morning"):
        print("This backup run started too late in the day (GitHub's timer was late). Not posting.")
        return
    if not dry and not force:
        c.sync_repo()
        st = c.load_state()
        c.resolve_unfinished(st, "morning")  # an earlier run never heard back from X?
        if c.already_posted(st, "morning", c.today_jst()):
            print("Today's morning post is already out (or being checked). Nothing to do.")
            return
    texts = c.load_texts()
    eclipse = c.load_eclipses().get(date)
    problems = []

    # --- NOAA: flare and geomagnetic storm (each on its own, so one bad table doesn't drop the other)
    flare = storm = None
    try:
        resp = requests.get(NOAA_URL, timeout=30)
        resp.raise_for_status()
        noaa = resp.text
    except Exception as e:
        noaa = None
        problems.append(f"NOAAの予報を取得できませんでした（太陽フレア・磁気嵐なし）: {type(e).__name__}: {e}")
    if noaa:
        try:
            flare = parse_flare_forecast(noaa, date)
        except Exception as e:
            problems.append(f"太陽フレア予報を読み取れませんでした: {type(e).__name__}: {e}")
        try:
            storm = parse_storm_forecast(noaa, date)
        except Exception as e:
            problems.append(f"磁気嵐（Kp）予報を読み取れませんでした: {type(e).__name__}: {e}")

    # --- sky: moon and meteor showers
    moon = moon_event = meteor = sky = None
    try:
        sky = c.load_sky()
    except Exception as e:
        problems.append(f"月・流星群のデータ（data/sky.json）を読めませんでした: {type(e).__name__}: {e}")
    if sky:
        try:
            moon, moon_event = moon_today(sky, date)
        except Exception as e:
            problems.append(f"月の満ち欠けを出せませんでした: {type(e).__name__}: {e}")
        try:
            meteor = sky["meteors"].get(date)
        except Exception as e:
            problems.append(f"流星群のデータを読めませんでした: {type(e).__name__}: {e}")

    forecast = build_forecast(flare, storm, moon, meteor)

    # --- encouragement: only if it fits after the forecast
    state_now = c.load_state()
    recent = state_now.get("recent_morning_lines", [])
    line, line_kind = None, "left out (no room)"
    if room_for_line(forecast, eclipse):
        fresh = c.fixed_pool(state_now, "morning", texts["morning_encouragement"])
        line, line_kind, problem = pick_line(texts["morning_encouragement"], recent, fresh)
        if problem:
            problems.append(f"励ましの一言をClaudeで作れなかったため、固定文を使いました: {problem}")

    post = build_post(forecast, eclipse, line)
    if c.x_len(post) > c.X_LIMIT and line:
        # should never happen (LINE_ROOM covers the longest line), but never post over the limit
        line, line_kind = None, "left out (over limit)"
        post = build_post(forecast, eclipse, None)
    print(f"[line] {line_kind}: {line}")
    print(f"[post] ({c.x_len(post)}/{c.X_LIMIT})\n{post}")
    if problems:
        print("[problems]\n" + "\n".join(problems))
    if dry:
        return
    if not post:
        raise RuntimeError("nothing to post: " + " / ".join(problems))

    context = []
    if flare:
        context.append(f"今日の太陽フレア予報（NOAA）: R1-R2 {flare[0]}%、R3 {flare[1]}%")
    if storm is not None:
        context.append(f"今日の磁気嵐予報（NOAA）: 最大Kp {storm[1]:.2f} → {'G' + str(storm[0]) if storm[0] else 'G1未満（なし）'}")
    if moon:
        if moon_event:
            extra = f"（瞬間は日本時間 {moon_event['jst']}"
            if moon_event.get("distance_km"):
                extra += f"、地球との距離 約{moon_event['distance_km']:,}km"
            extra += "）"
        else:
            extra = ""
        context.append(f"今日の月: {moon}{extra}")
    if meteor:
        context.append(f"今日は{meteor['name']}の極大日（{meteor['hemisphere']}、極大は日本時間 {meteor['peak_jst']}ごろ、IMOの予測から計算）")
    if eclipse:
        context.append(f"今日の{eclipse['type']}: 見える地域 {eclipse['regions']}、最大になる時刻（日本時間）{eclipse['greatest_jst']}")
    record = {"kind": "morning", "date": date, "text": post, "context": "\n".join(context)}

    # --- picture: made before the "posting" flag (it takes a few minutes)
    # one of the 4 base pictures, never the same as yesterday's; the 1st of the month = signature outfit
    image, look, image_problem = None, None, None
    base = lyra_image.pick_base(c.load_state().get("last_image_base"))
    signature = lyra_image.is_signature_day(date)
    if os.environ.get("OPENAI_API_KEY"):
        try:
            image, look, log = lyra_image.make_morning_image(
                c.load_state().get("recent_image_looks", []), base, signature)
            print("[image]\n" + "\n".join(log))
            if not image:
                print("[image] no picture passed the check; using the base picture")
        except lyra_image.ImageFatal as e:
            image_problem = str(e)
            print(f"[image] {e}")
    else:
        print("[image] OPENAI_API_KEY not set; using the base picture")

    state = c.begin_post("morning", record, force)
    if state is None:
        return
    # today's picture if there is one; otherwise (or if its upload fails) the day's base picture;
    # only if even that can't be uploaded does the post go out without a picture
    media_ids, used_todays = None, False
    for pic, name in ((image, "today's picture"), (lyra_image.base_bytes(base), f"base picture {base}")):
        if not pic:
            continue
        try:
            media_ids = [c.x_upload_image(pic)]
            used_todays = pic is image
            print(f"[image] uploaded {name}")
            break
        except Exception as e:
            print(f"[image] upload of {name} to X failed: {e}")
    try:
        tweet_id = c.x_post(post, media_ids=media_ids)
    except c.XPostUncertain as e:
        # X didn't answer clearly: it may be posted. The "posting" flag stays;
        # the next run checks X for this exact text before doing anything.
        if line:
            state.setdefault("recent_morning_lines", []).append(line)
            if line_kind.startswith("fixed"):
                c.remember_fixed(state, "morning", line, texts["morning_encouragement"])
            c.save_state(state)
        print(f"[post] {e}")
        return
    except Exception as e:
        # no email here: the next run (30 minutes later) tries again.
        # If nothing has gone out by the end of the window, lyra_replies.py emails Dai.
        c.finish_post(state, "morning", None)
        print(f"[post] failed, the next run will try again: {e}")
        return

    if line:
        state.setdefault("recent_morning_lines", []).append(line)
        if line_kind.startswith("fixed"):
            c.remember_fixed(state, "morning", line, texts["morning_encouragement"])
    state["last_image_base"] = base
    if used_todays and look:
        keep = {k: look[k] for k in ("outfit", "expression", "pose")}
        if signature:
            keep["outfit"] = "signature outfit (1st of the month)"
        state["recent_image_looks"] = (state.get("recent_image_looks", []) + [keep])[-lyra_image.RECENT_LOOKS:]
    c.finish_post(state, "morning", tweet_id)
    print(f"[post] OK id={tweet_id}" + (" (today's picture)" if used_todays else " (base picture)" if media_ids else " (no picture)"))

    if image_problem:
        c.notify_once(
            state, "morning_image",
            "【DJ Lyra Ai】朝の画像を作れませんでした（投稿は基本画像で出ています）",
            "今朝の宇宙天気予報は出ましたが、OpenAIの画像作成が使えなかったため、基本画像を付けて投稿しました"
            "（このメールは1日1回まで）。\n\n"
            f"エラー: {image_problem}\n\n"
            "確認すること: OpenAIの残高・自動チャージ、本人確認（Organization verification）、"
            "GitHubのSecretsのOPENAI_API_KEY。直れば翌朝から自動で画像付きに戻ります。",
        )

    if problems:
        c.notify_once(
            state, "morning_partial",
            "【DJ Lyra Ai】朝の投稿で一部の情報を作れませんでした",
            "今朝の投稿は出ましたが、一部の情報を作れなかったため、その部分なしで投稿しました"
            "（このメールは1日1回まで）。\n\n" + "\n".join(problems) +
            f"\n\n投稿した文:\n{post}\n\n"
            "一時的なものなら、明日は自動で元に戻ります。続くようならGitHub Actionsのログと、"
            "Claude APIの残高・キーを確認してください。",
        )  # notify_once saves and pushes the state itself


if __name__ == "__main__":
    c.run_main(main, "Lyra Morning Post")
