#!/usr/bin/env python3
"""
DJ Lyra Ai - Morning image ("宇宙天気予報お姉さん")
==================================================
Makes the picture for the morning space-weather post.

- There are 4 base images (all in her signature purple outfit), each with a
  different composition. Every day one is picked, never the same as yesterday's:
    1 data/morning_base.jpg    knee-up, she stands on the left
    2 data/morning_base_2.jpg  knee-up, she stands on the right
    3 data/morning_base_3.jpg  high angle from above, she stands on the left (window with Earth behind her)
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
  caller emails the Doctor once a day.

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
# The outfit is designed by a stronger model for better fashion sense (about 1 yen a call);
# if it can't be used, the check model designs it instead.
DESIGN_MODEL = os.environ.get("LYRA_DESIGN_MODEL", "claude-sonnet-5")


# The 4 base images (all 4 redrawn in a brighter neon style on 2026-11-03). "shot" goes into the stylist prompt, "framing" into the image
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
        "shot": "The picture is taken from a high angle above her, showing her from the knees up on the left side, "
                "so do not mention shoes, boots or socks below the knee: focus on tops, jackets, dresses, "
                "skirts or pants, and accessories.",
        "pose": "a natural presenter pose for a knee-up shot (no full-body or leg poses, no sitting, no jumping)",
        "framing": "The camera looks down at her from a high angle. She stands on the LEFT side, shown from "
                   "the knees up, with the window showing the Earth behind her and the hologram on the right. "
                   "Do not zoom out. Her feet and shoes must not be visible.",
        "too_wide": "The framing is clearly zoomed out to a full-body shot: her feet or shoes are visible.",
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

# Her signature outfit (the one in all the base images): worn on the 1st of every month.
SIGNATURE_OUTFIT = (
    "her signature outfit, exactly as in the base image: a sleeveless high-neck fitted mini dress in dark navy "
    "with a glowing blue and purple geometric pattern, black harness straps with silver buckles across the chest, "
    "a silver chain necklace with a ring, black opaque tights, a glossy purple bracelet on her wrist, purple nails, "
    "black headphones with a small headset microphone, and the blue dolphin tattoo on her upper arm"
)


# Style themes. Every day one theme is picked (never one of the last RECENT_GENRES days),
# so the looks don't drift into the same casual top-and-skirt every morning.
GENRES = [
    "rave / festival wear: neon colors, UV-reactive prints, glowing accessories",
    "cyberpunk techwear: functional straps, buckles, panels, futuristic black with neon accents",
    "gothic: dark lace, corset-style vest over a blouse, velvet, silver details",
    "elegant evening wear: a modest, graceful dress with refined accessories",
    "spaceship crew uniform: a sleek captain or pilot uniform with insignia-like patches (no text)",
    "Y2K pop: bright candy colors, shiny fabrics, playful accessories",
    "punk rock: plaid, studs, a leather jacket, chains, band-tee style (no text)",
    "space fantasy princess: a flowing star-patterned outfit with a cape and crystal accessories",
    "sweet lolita: pastel colors, layered frills, bows, a bell-shaped skirt",
    "business chic: a tailored blazer and skirt suit with a stylish twist",
    "bohemian: flowing layers, embroidery, fringe, natural colors",
    "DJ club wear: an oversized jacket or hoodie with bold graphics (no text), statement accessories",
    "gypsy dress: a flowing, colorful bohemian-gypsy dress with ruffled tiers, embroidery and coin jewelry",
    "tribal fusion belly-dance style: a modest high-neck top, layered flowing skirts, a coin hip belt, tribal jewelry",
    "rock casual: a band-style tee (no text), a leather or denim jacket, ripped details, silver rings",
    "kigurumi: a cute animal onesie-style outfit (cat, bear, rabbit...), with the hood pushed back so her purple hair shows",
    "gyaru: bright, bold Japanese gal fashion with a cute, cheerful vibe (not sexy), statement accessories",
    "yuru-fuwa: soft, fluffy, relaxed girly fashion in pastel colors, oversized knit, gentle layers",
    "psychedelic: 60s-70s psychedelic patterns, swirling kaleidoscope or tie-dye prints in vivid colors, trippy accessories",
    "dark fantasy: a mystical black-and-violet sorceress look, flowing layers, moon and star charms, an enchanted vibe",
    "techno minimal: clean futuristic minimalism in white, black and gray, sharp lines, no decoration beyond one metallic accent",
    "steampunk: brown leather, brass gears, a corset-style vest over a blouse, goggles hung around the neck",
]
RECENT_GENRES = 12


SHOE_WORDS = re.compile(r"\b(shoes?|boots?|booties|sneakers?|trainers?|heels?|high-heels?|platforms?|platform-soles?|"
                        r"sandals?|loafers?|pumps|mules|clogs|footwear|socks?|ankle)\b", re.I)


def pick_genre(recent: list[dict], tried: list[str]) -> str:
    """A theme not used in the last RECENT_GENRES looks, nor already tried today."""
    used = {r.get("genre") for r in recent[-RECENT_GENRES:]} | set(tried)
    choices = [g for g in GENRES if g not in used] or [g for g in GENRES if g not in tried] or GENRES
    return random.choice(choices)


def is_signature_day(date: str) -> bool:
    """date = 'YYYY-MM-DD' (Japan time). The 1st of every month."""
    return date.endswith("-01")


# Bases kept in BASES but not picked for now (base 3: the high angle makes her figure look bigger; on hold 2026-11-11).
PAUSED_BASES = {3}


def pick_base(last: int | None) -> int:
    """A random base, never the same as yesterday's (paused bases are skipped)."""
    active = [n for n in BASES if n not in PAUSED_BASES] or list(BASES)
    choices = [n for n in active if n != last] or active
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
  One short phrase, at most 12 words.
- Pose: {pose}. Describe only her upper body, arms, hands and head, in one short sentence (at most 25 words).
  Never mention her legs, feet, knees, weight, stance or walking.
- The black cat pointer stick: she points it at the hologram (at the Sun, Saturn, the Earth or another planet)
  with her arm stretched out, like in the base image, so the cat face at its tip is clearly visible.
  Vary which planet she points at and what her other hand, head and shoulders do.
- She is a friendly TV weather presenter: graceful, polished presenter gestures. Her free hand stays open and soft
  (an open palm toward the hologram, a small wave, a hand lightly at her waist or near her chin).
  No fists, no fist pumps or cheering arms, no arms thrown up, no dancing, no rough or sporty gestures.
  Never hold it upright beside her face, twirl it, spin it, hold it behind her or hide the tip.

Recent looks (do NOT repeat or closely copy any of them):
{recent}

Keep it short: outfit at most 60 words, expression at most 12 words, pose at most 25 words.
Answer with JSON only: {answer}"""

NEW_OUTFIT_TASK = """Design today's look in today's style theme: {genre}.
The theme must be obvious at a glance; do not fall back to plain everyday basics (a simple knit top and skirt).
Make the outfit completely new: a different style, colors and items from every recent look below.
Be specific and fashionable (2-4 items, colors, materials). Short skirts and bare shoulders are fine.
Style it like a professional stylist:
- Limit the palette to 2-3 colors that work together, plus one accent color.
- Pick one statement piece and let the other items support it.
- Balance the silhouette (e.g. a volume top with a slim bottom, or the other way around).
- Choose colors that flatter her purple hair and yellow eyes and stand out against the blue-purple studio.
It is a morning show and she is cute and stylish, never sexy: no plunging or deep V necklines, no cleavage,
no lingerie-like, bra-like or see-through tops, and no deep armholes or side openings that show the side of the chest.
Do not describe her body or figure; only describe the clothes.
Jackets and cardigans are worn normally on the shoulders (never "draped", "slipping off" or "off the shoulders").
{shot}"""

SIGNATURE_TASK = """Today is the 1st of the month: she wears her signature outfit (already decided, do not design an outfit).
Design only today's expression and pose.
{shot}"""


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


def design_look(recent: list[dict], base: int = 1, signature: bool = False, genre: str = "") -> dict:
    """Claude designs a brand-new outfit / expression / pose, unlike the recent ones.
    On signature days the outfit is fixed and Claude designs only expression and pose."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return fallback_look(recent, signature)
    b = BASES[base]
    if signature:
        task = SIGNATURE_TASK.format(shot=b["shot"].split(", so ")[0] + ".")
        answer = '{"expression": "...", "pose": "..."}'
        need = ("expression", "pose")
    else:
        task = NEW_OUTFIT_TASK.format(shot=b["shot"], genre=genre or random.choice(GENRES))
        answer = '{"outfit": "...", "expression": "...", "pose": "..."}'
        need = ("outfit", "expression", "pose")
    prompt = DESIGN_PROMPT.format(task=task, pose=b["pose"], recent="{recent}", answer=answer)
    lines = "\n".join(
        f"- outfit: {r.get('outfit')} | expression: {r.get('expression')} | pose: {r.get('pose')}"
        for r in recent[-RECENT_LOOKS:]
    ) or "(none yet)"
    errors = []
    tries = [(m, n) for m in dict.fromkeys((DESIGN_MODEL, CHECK_MODEL)) for n in range(2)]  # stronger model first
    extra = ""
    for model, n in tries:
        try:
            r = requests.post(
                CLAUDE_URL,
                headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                json={"model": model, "max_tokens": 600, "temperature": 1.0,
                      "messages": [{"role": "user", "content": prompt.replace("{recent}", lines) + extra}]},
                timeout=60,
            )
            if r.status_code != 200:
                raise RuntimeError(f"Claude {r.status_code}: {r.text[:200]}")
            text = "".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text")
            m = re.search(r"\{.*\}", text, re.S)
            look = json.loads(m.group(0)) if m else {}
            look = {k: str(look.get(k, "")).strip() for k in need}
            if not all(look.values()) or any(len(v) > 900 for v in look.values()):
                raise ValueError(f"unusable answer: {text[:200]}")
            if signature:
                look["outfit"] = SIGNATURE_OUTFIT
            elif re.search(r"\b(bag|backpack|fanny pack|purse)\b", look["outfit"], re.I):
                raise ValueError(f"outfit has a bag: {look['outfit'][:150]}")
            elif not signature and SHOE_WORDS.search(look["outfit"]):
                # shoes in the description tend to make the picture zoom out to full body
                extra = ("\n\nYour last answer mentioned footwear. The picture never shows her feet: "
                         "do not mention shoes, boots, sneakers, heels, platforms, sandals or socks at all.")
                raise ValueError(f"outfit mentions footwear: {SHOE_WORDS.search(look['outfit']).group(0)}")
            look["by"] = model
            if genre and not signature:
                look["genre"] = genre
            if errors:
                look["note"] = "; ".join(errors)
            return look
        except Exception as e:
            errors.append(f"{model}: {type(e).__name__}: {str(e)[:200]}")
            print(f"[image] design with {model} failed ({errors[-1]})")
    look = fallback_look(recent, signature)
    look["note"] = "; ".join(errors)
    return look


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
        "yellow eyes, piercings and headset microphone exactly the same. "
        "Keep her body shape and proportions exactly the same as the base image: the same slim build "
        "and the same bust size. The new outfit must not change her figure.\n\n"
        "Framing: keep exactly the same camera distance, angle and 16:9 framing as the base image. "
        f"{BASES[base]['framing']}\n\n"
        + change + items +
        "She still holds the slim pointer stick with a cute black cat face at its tip. "
        "The black cat face at the tip is clearly visible and large enough to recognize.\n"
        "Cute and stylish, not sexy: a modest neckline with no cleavage, and armholes that sit close to the body "
        "(no side openings showing the side of the chest).\n"
        "She has exactly two arms and two hands: one hand holds the pointer stick, the other makes the new gesture. "
        "The free hand moves to the new gesture; it does not also stay where it was in the base image.\n"
        "Any jacket, cardigan or coat is worn properly: both arms in the sleeves and sitting on her shoulders, "
        "not slipping off the shoulders or hanging off the arms.\n"
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
C. Her face, hands or body are badly broken or deformed (melted face, twisted body). Small imperfections are fine.
   Also count her arms and hands carefully: she must have exactly two arms and two hands. A third arm or hand
   anywhere (for example one on her chest, one holding the stick and another resting on the desk or console) = fail.
D. Nudity, or one of exactly these four: clearly visible cleavage, a deep plunging V-neckline, a top that looks like a bra or lingerie,
   or deep armholes / side openings that clearly show the side of her chest (side-boob).
   Everything else is fine: crop tops, bare midriff, sleeveless tops, bare shoulders, mini skirts, shorts, tights,
   sheer cardigans or jackets worn over a normal top, and a small glimpse of underwear.
E. The scene is no longer the spaceship studio with the solar system hologram.
F. {too_wide}
G. Her signature prop is wrong: she is not holding the pointer stick, or there is no black cat face at the tip of the stick
   (a plain stick, a ball, a star or anything else at the tip = fail). The cat face may be small, but it must be recognizable as a black cat's face.
   Judge only image 2 for this; do not compare the stick's shape or length with image 1.

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
    tried = []
    for attempt in range(1, MAX_TRIES + 1):
        if time.monotonic() - started > TIME_BUDGET:
            log.append(f"time budget used up after {attempt - 1} tries")
            break
        genre = "" if signature else pick_genre(recent, tried)  # a new theme and look every try
        tried.append(genre)
        look = design_look(recent, base, signature, genre)
        try:
            image = generate(build_prompt(look, base, signature), base)
        except ImageFatal:
            raise
        except Exception as e:
            log.append(f"[{attempt}] generation failed: {type(e).__name__}: {e}")
            time.sleep(5)
            continue
        ok, reason = check(image, base, signature)
        outfit = "signature outfit" if signature else f"[{genre.split(':')[0]}] {look['outfit']}"
        log.append(f"[{attempt}] ({look['by']}) {outfit} / {look['expression']} / {look['pose']} -> {reason}")
        if look.get("note"):
            log.append(f"    design note: {look['note']}")
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
