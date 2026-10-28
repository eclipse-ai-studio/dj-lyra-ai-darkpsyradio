#!/usr/bin/env python3
"""
DJ Lyra Ai - Morning image ("宇宙天気予報お姉さん")
==================================================
Makes the picture for the morning space-weather post.

- There are 4 base images (all in her signature purple outfit), each with a
  different composition. Every day one is picked, never the same as yesterday's:
    1 data/morning_base.jpg    knee-up, she stands on the left
    2 data/morning_base_2.jpg  knee-up, she stands on the right
    3 data/morning_base_3.jpg  close-up from the chest up
    4 data/morning_base_4.jpg  behind the broadcast desk (waist down hidden)
  Same studio, hologram, art style and face; the day's base decides the
  framing. Only her outfit, expression and pose change. She always holds
  the black cat pointer stick.
- On the 1st of every month she wears her signature purple outfit (the one
  in the base images); Claude designs only a new expression and pose.
- The outfit, expression and pose are designed fresh by Claude every time,
  avoiding everything used in the last 30 pictures (kept in the state file).
  If Claude can't answer, one is picked from data/lyra_image_looks.json instead.
- Made with OpenAI's image edit API (env OPENAI_API_KEY), 16:9.
- Each image is checked by Claude against the base image. The check is
  kept lenient on purpose: it only rejects clear failures (readable
  text/numbers, clearly a different person, badly broken face or hands,
  explicit content). Small fake UI marks, a hidden tattoo, short skirts
  are all fine.
- Up to 10 tries. If none passes, the morning post uses that day's base picture
  instead (no email). If the key/billing/verification is the problem, the
  caller emails Dai once a day.

Test from the command line (writes output/morning_image.jpg):
    python scripts/lyra_image.py [--base 1-4] [--signature]
"""
import base64
import json
import os
import random
import re
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
BASE_IMAGE = ROOT / "data" / "morning_base.jpg"   # base 1 (also the last-resort picture)
LOOKS_PATH = ROOT / "data" / "lyra_image_looks.json"

OPENAI_EDIT_URL = "https://api.openai.com/v1/images/edits"
IMAGE_MODEL = os.environ.get("LYRA_IMAGE_MODEL", "gpt-image-2")
IMAGE_QUALITY = os.environ.get("LYRA_IMAGE_QUALITY", "medium")  # low / medium / high
IMAGE_SIZE = "1536x864"      # 16:9 (gpt-image-2 accepts custom sizes divisible by 16)
MAX_TRIES = 10
TIME_BUDGET = 15 * 60        # seconds for all tries together (the workflow allows 25 min)
RECENT_LOOKS = 30            # Claude is shown the last 30 looks and must not repeat them

CLAUDE_URL = "https://api.anthropic.com/v1/messages"
CHECK_MODEL = os.environ.get("LYRA_CHECK_MODEL", "claude-haiku-4-5-20251001")


# The 4 base images. "shot" goes into the stylist prompt, "framing" into the image
# prompt, "too_wide" is the checker's rule F for that composition.
BASES = {
    1: {
        "file": ROOT / "data" / "morning_base.jpg",
        "shot": "The picture shows her from the knees up, standing on the left side next to the hologram, "
                "so do not mention shoes, boots or socks below the knee: focus on tops, jackets, dresses, "
                "skirts or pants, and accessories.",
        "pose": "a natural presenter pose for a knee-up shot (no full-body or leg poses, no sitting, no jumping)",
        "framing": "She is shown from the knees up (a knee-up shot), standing on the LEFT side of the frame, "
                   "with the hologram on the right. Do not zoom out. Her feet and shoes must not be visible.",
        "too_wide": "The framing is clearly zoomed out to a full-body shot: her feet or shoes are visible.",
    },
    2: {
        "file": ROOT / "data" / "morning_base_2.jpg",
        "shot": "The picture shows her from the knees up, standing on the right side next to the hologram, "
                "so do not mention shoes, boots or socks below the knee: focus on tops, jackets, dresses, "
                "skirts or pants, and accessories.",
        "pose": "a natural presenter pose for a knee-up shot (no full-body or leg poses, no sitting, no jumping)",
        "framing": "She is shown from the knees up (a knee-up shot), standing on the RIGHT side of the frame, "
                   "with the hologram on the left. Do not zoom out. Her feet and shoes must not be visible.",
        "too_wide": "The framing is clearly zoomed out to a full-body shot: her feet or shoes are visible.",
    },
    3: {
        "file": ROOT / "data" / "morning_base_3.jpg",
        "shot": "The picture is a close-up from the chest up, so only her top, collar, hair accessories and "
                "jewelry are visible: design the outfit around the top (no skirts, pants or shoes needed).",
        "pose": "an upper-body pose for a chest-up close-up (no leg poses, no full-body poses); she holds the "
                "black cat pointer near her face or shoulder so the cat face is inside the close-up",
        "framing": "This is a close-up from the chest up, the same as the base image. Do not zoom out: "
                   "her waist and legs must not be visible.",
        "too_wide": "The framing is zoomed out: her waist, hips or legs are visible (it must stay a chest-up close-up).",
    },
    4: {
        "file": ROOT / "data" / "morning_base_4.jpg",
        "shot": "She stands behind a broadcast desk like a news anchor, so the desk hides her from the waist down: "
                "design the outfit around the top and jacket (skirts, pants and shoes will not be seen).",
        "pose": "a news-anchor pose behind the desk using only her upper body (she may rest one hand on the desk); "
                "no leg poses, she does not step out from behind the desk",
        "framing": "She stands behind the same futuristic broadcast desk as in the base image, on the left side, "
                   "with the hologram on the right. The desk hides her body from the waist down. "
                   "Keep the desk exactly as it is, with no text or logos on it.",
        "too_wide": "The broadcast desk is gone, or she is in front of / beside the desk so her legs are visible.",
    },
}

# Her signature outfit (the one in all 4 base images): worn on the 1st of every month.
SIGNATURE_OUTFIT = (
    "her signature outfit, exactly as in the base image: a sleeveless high-neck fitted mini dress in dark navy "
    "with a glowing blue and purple geometric pattern, black harness straps with silver buckles across the chest, "
    "a silver chain necklace with a ring, black opaque tights, a glossy purple bracelet on her wrist, purple nails, "
    "black headphones with a small headset microphone, and the blue dolphin tattoo on her upper arm"
)


def is_signature_day(date: str) -> bool:
    """date = 'YYYY-MM-DD' (Japan time). The 1st of every month."""
    return date.endswith("-01")


def pick_base(last: int | None) -> int:
    """A random base, never the same as yesterday's."""
    choices = [n for n in BASES if n != last] or list(BASES)
    return random.choice(choices)


def base_bytes(base: int) -> bytes:
    """The day's base picture; base 1 if that file is missing."""
    f = BASES.get(base, BASES[1])["file"]
    return (f if f.exists() else BASE_IMAGE).read_bytes()


class ImageFatal(Exception):
    """Trying again won't help (bad key, no credit, account not verified...)."""


# ---------------------------------------------------------------- the look of the day
DESIGN_PROMPT = """You are the stylist of DJ Lyra Ai, an anime AI DJ girl (purple bob hair, yellow eyes).
Every morning she presents the space weather forecast from a futuristic spaceship studio,
standing next to a holographic solar system map and holding a slim pointer stick with a cute black cat face at its tip.
She loves fashion: rave, cyber, street, Y2K, idol, gothic, elegant, sporty, cozy, retro-futuristic, and more.

{task}
Rules:
- No bag, no backpack, no fanny pack.
- No words, letters, numbers or logos printed on the clothes.
- No hats or helmets that hide her purple bob hair, no masks or glasses covering her eyes.
- Expression: a cheerful, friendly one (smile, wink, laugh, excited, shy, confident, dreamy...), different from the recent ones.
- Pose: {pose}. She must hold the black cat pointer stick
  (pointing at the hologram, at a planet, at the viewer, twirling it, resting it on her shoulder, etc.). Different from the recent ones.

Recent looks (do NOT repeat or closely copy any of them):
{recent}

Answer with JSON only: {answer}"""

NEW_OUTFIT_TASK = """Design today's look. Make the outfit completely new: a different style, colors and items from every recent look below.
Be specific and fashionable (2-4 items, colors, materials). Short skirts and bare shoulders are fine.
It is a morning show and she is cute and stylish, never sexy: no plunging or deep V necklines, no cleavage,
no lingerie-like, bra-like or see-through tops.
{shot}"""

SIGNATURE_TASK = """Today is the 1st of the month: she wears her signature outfit (already decided, do not design an outfit).
Design only today's expression and pose."""


def fallback_look(recent: list[dict], signature: bool = False) -> dict:
    """Used only if Claude can't design one: random pick from data/lyra_image_looks.json."""
    looks = json.loads(LOOKS_PATH.read_text())
    used = {r.get("outfit") for r in recent}
    outfits = [o for o in looks["outfits"] if o not in used] or looks["outfits"]
    return {
        "outfit": SIGNATURE_OUTFIT if signature else random.choice(outfits),
        "expression": random.choice(looks["expressions"]),
        "pose": random.choice(looks["poses"]),
        "by": "fallback list",
    }


def design_look(recent: list[dict], base: int = 1, signature: bool = False) -> dict:
    """Claude designs a brand-new outfit / expression / pose, unlike the recent ones.
    On signature days the outfit is fixed and Claude designs only expression and pose."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return fallback_look(recent, signature)
    b = BASES[base]
    if signature:
        task = SIGNATURE_TASK
        answer = '{"expression": "...", "pose": "..."}'
        need = ("expression", "pose")
    else:
        task = NEW_OUTFIT_TASK.format(shot=b["shot"])
        answer = '{"outfit": "...", "expression": "...", "pose": "..."}'
        need = ("outfit", "expression", "pose")
    prompt = DESIGN_PROMPT.format(task=task, pose=b["pose"], recent="{recent}", answer=answer)
    lines = "\n".join(
        f"- outfit: {r.get('outfit')} | expression: {r.get('expression')} | pose: {r.get('pose')}"
        for r in recent[-RECENT_LOOKS:]
    ) or "(none yet)"
    try:
        r = requests.post(
            CLAUDE_URL,
            headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": CHECK_MODEL, "max_tokens": 400, "temperature": 1.0,
                  "messages": [{"role": "user", "content": prompt.replace("{recent}", lines)}]},
            timeout=60,
        )
        if r.status_code != 200:
            raise RuntimeError(f"Claude {r.status_code}: {r.text[:200]}")
        text = "".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text")
        m = re.search(r"\{.*\}", text, re.S)
        look = json.loads(m.group(0)) if m else {}
        look = {k: str(look.get(k, "")).strip() for k in need}
        if not all(look.values()) or any(len(v) > 400 for v in look.values()):
            raise ValueError(f"unusable answer: {text[:200]}")
        if signature:
            look["outfit"] = SIGNATURE_OUTFIT
        elif re.search(r"\b(bag|backpack|fanny pack|purse)\b", look["outfit"], re.I):
            raise ValueError(f"outfit has a bag: {look['outfit']}")
        look["by"] = "claude"
        return look
    except Exception as e:
        print(f"[image] Claude could not design a look ({type(e).__name__}: {e}); using the fallback list")
        return fallback_look(recent, signature)


def build_prompt(look: dict, base: int = 1, signature: bool = False) -> str:
    if signature:
        change = (
            "Keep her outfit exactly the same as the base image: " + SIGNATURE_OUTFIT.split(": ", 1)[1] + ".\n"
            "Change only her expression and pose:\n"
        )
        items = f"- Expression: {look['expression']}\n- Pose: {look['pose']}\n\n"
    else:
        change = "Change only her outfit, expression and pose:\n"
        items = (f"- Outfit: {look['outfit']}\n"
                 f"- Expression: {look['expression']}\n"
                 f"- Pose: {look['pose']}\n\n")
    return (
        "Use the attached image as the base. Keep the same character, the same futuristic "
        "spaceship broadcast studio background, the same holographic solar system map, "
        "the same anime art style and lighting. Keep her face, purple bob hair, "
        "yellow eyes, piercings and headset microphone exactly the same.\n\n"
        "Framing: keep exactly the same camera distance, angle and 16:9 framing as the base image. "
        f"{BASES[base]['framing']}\n\n"
        + change + items +
        "She still holds the slim pointer stick with a cute black cat face at its tip. "
        "The black cat face at the tip is clearly visible and large enough to recognize.\n"
        "Cute and stylish, not sexy: a modest neckline with no cleavage.\n"
        + "No text, no letters, no numbers, no logos, no watermark. No bag."
    )


# ---------------------------------------------------------------- OpenAI
def generate(prompt: str, base: int = 1) -> bytes:
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        raise ImageFatal("OPENAI_API_KEY is not set")
    files = {"image": ("base.jpg", base_bytes(base), "image/jpeg")}
    data = {
        "model": IMAGE_MODEL,
        "prompt": prompt,
        "size": IMAGE_SIZE,
        "quality": IMAGE_QUALITY,
        "output_format": "jpeg",
        "output_compression": "90",
        "n": "1",
    }
    r = requests.post(OPENAI_EDIT_URL, headers={"Authorization": f"Bearer {key}"},
                      data=data, files=files, timeout=300)
    if r.status_code != 200:
        msg = r.text[:500]
        # 401 = bad key, 403 = organization not verified / no access,
        # 429 with "quota"/"billing" = no credit left. Retrying won't help.
        if r.status_code in (401, 403) or (r.status_code == 429 and re.search(r"quota|billing", msg, re.I)):
            raise ImageFatal(f"OpenAI {r.status_code}: {msg}")
        if r.status_code == 404 and "model" in msg.lower():
            raise ImageFatal(f"OpenAI model not available ({IMAGE_MODEL}): {msg}")
        # 400 (often the safety filter), 429 rate limit, 5xx: try again with a new look
        raise RuntimeError(f"OpenAI {r.status_code}: {msg}")
    item = r.json()["data"][0]
    return base64.b64decode(item["b64_json"])


# ---------------------------------------------------------------- Claude check
CHECK_PROMPT = """Image 1 is the official base picture of an anime character, DJ Lyra Ai.
Image 2 is a new picture made from it, where only her outfit, expression and pose were meant to change.
{outfit_note}

Be lenient. Only fail image 2 for one of these CLEAR problems:
A. Readable words, letters or numbers stand out in the picture (tiny unreadable fake UI marks, symbols and icons are fine).
B. She is clearly a different person: hair not purple, hair much longer than a bob, eye color not yellow/gold, or a clearly different face.
C. Her face, hands or body are badly broken or deformed (extra limbs, melted face). Small imperfections are fine.
D. Nudity, or one of exactly these three: clearly visible cleavage, a deep plunging V-neckline, or a top that looks like a bra or lingerie.
   Everything else is fine: crop tops, bare midriff, sleeveless tops, bare shoulders, mini skirts, shorts, tights,
   sheer cardigans or jackets worn over a normal top, and a small glimpse of underwear.
E. The scene is no longer the spaceship studio with the solar system hologram.
F. {too_wide}
G. Her signature prop is wrong: she is not holding the pointer stick, or there is no black cat face at the tip of the stick
   (a plain stick, a ball, a star or anything else at the tip = fail). The cat face may be small, but it must be recognizable as a black cat's face.

Everything else is fine: a different outfit, a hidden tattoo, missing accessories, a changed pose,
small background differences, framing a little closer or a little wider (as long as rule F is kept).
{signature_rule}
Answer with JSON only, nothing else: {{"ok": true}} or {{"ok": false, "reason": "<letter>: <short reason>"}}"""

SIGNATURE_RULE = """
Today she must wear her signature outfit, the same one as in image 1. Extra rule:
H. The outfit is clearly a different outfit from image 1 (different dress, different main colors, no harness straps).
   Small detail differences are fine (pattern lines, a missing bracelet or necklace, the tattoo hidden).
"""


def check_prompt(base: int = 1, signature: bool = False) -> str:
    note = ("Today the outfit was meant to stay the same as image 1." if signature else
            "A different outfit is expected and is never a problem by itself. "
            "Do not compare how much skin the outfit shows with image 1.")
    text = CHECK_PROMPT.format(outfit_note=note, too_wide=BASES[base]["too_wide"],
                               signature_rule=SIGNATURE_RULE if signature else "")
    if signature:
        text = (text.replace("only her outfit, expression and pose were meant to change",
                             "only her expression and pose were meant to change")
                    .replace("Everything else is fine: a different outfit, a hidden tattoo",
                             "Everything else is fine: a hidden tattoo"))
    return text


def check(image: bytes, base: int = 1, signature: bool = False) -> tuple[bool, str]:
    """True = good enough to post. If the checker itself fails, the image is accepted
    (OpenAI's own safety filter already ran; better a picture than none)."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return True, "not checked (no ANTHROPIC_API_KEY)"
    b64 = lambda b: base64.b64encode(b).decode()
    content = [
        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64(base_bytes(base))}},
        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64(image)}},
        {"type": "text", "text": check_prompt(base, signature)},
    ]
    try:
        r = requests.post(
            CLAUDE_URL,
            headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": CHECK_MODEL, "max_tokens": 120, "messages": [{"role": "user", "content": content}]},
            timeout=90,
        )
        if r.status_code != 200:
            return True, f"not checked (Claude {r.status_code})"
        text = "".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text")
        m = re.search(r"\{.*\}", text, re.S)
        verdict = json.loads(m.group(0)) if m else {}
        if verdict.get("ok") is False:
            return False, str(verdict.get("reason", "no reason given"))
        return True, "ok"
    except Exception as e:
        return True, f"not checked ({type(e).__name__}: {e})"


# ---------------------------------------------------------------- all together
def make_morning_image(recent: list[dict], base: int = 1, signature: bool = False
                       ) -> tuple[bytes | None, dict | None, list[str]]:
    """Returns (jpeg bytes or None, the look used, log lines).
    Raises ImageFatal if the key / credit / account is the problem."""
    log = [f"base {base}" + (" / signature outfit day" if signature else "")]
    started = time.monotonic()
    for attempt in range(1, MAX_TRIES + 1):
        if time.monotonic() - started > TIME_BUDGET:
            log.append(f"time budget used up after {attempt - 1} tries")
            break
        look = design_look(recent, base, signature)  # a new look every try
        try:
            image = generate(build_prompt(look, base, signature), base)
        except ImageFatal:
            raise
        except Exception as e:
            log.append(f"[{attempt}] generation failed: {type(e).__name__}: {e}")
            time.sleep(5)
            continue
        ok, reason = check(image, base, signature)
        outfit = "signature outfit" if signature else look["outfit"]
        log.append(f"[{attempt}] ({look['by']}) {outfit} / {look['expression']} / {look['pose']} -> {reason}")
        if ok:
            return image, look, log
    return None, None, log


if __name__ == "__main__":
    # Manual test: make one picture (with checks) and save it. Nothing is posted.
    try:
        args = sys.argv[1:]
        base = int(args[args.index("--base") + 1]) if "--base" in args else pick_base(None)
        img, look, lines = make_morning_image([], base, "--signature" in args)
    except ImageFatal as e:
        print(f"FATAL: {e}")
        sys.exit(1)
    print("\n".join(lines))
    if img:
        out = ROOT / "output"
        out.mkdir(exist_ok=True)
        (out / "morning_image.jpg").write_bytes(img)
        (out / "morning_image_log.txt").write_text("\n".join(lines) + f"\n\nlook: {look}\n")
        print(f"saved output/morning_image.jpg ({len(img)} bytes)")
    else:
        print("no picture passed the check")
        sys.exit(1)
