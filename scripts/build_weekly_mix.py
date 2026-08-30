#!/usr/bin/env python3
"""
DJ Lyra Ai - Weekly Mix Pipeline
=================================
1. Generates TARGET_TRACKS dark psytrance tracks via Tunee AI (one at a time)
2. For each: waits, fetches the share page, extracts the mp3 URL, downloads it
3. Retries on failure; aborts after MAX_CONSECUTIVE_FAILURES in a row
4. Crossfades all successfully-downloaded tracks into one continuous mix
5. Checks the mix for minimum duration and long silent gaps (auto-fails if found)
6. Exports the final mix as MP3 128kbps

On any failure, writes a status file (pipeline_status.json) describing what
went wrong, for the notification step to read and email about.

Usage:
    python scripts/build_weekly_mix.py
"""

import json
import re
import subprocess
import sys
import time
from pathlib import Path

import requests
from pydub import AudioSegment
from pydub.silence import detect_silence

# ---- Fixed project settings ----
DARKPSY_PROMPT = (
    "darkpsy, underground darkpsy label style, instrumental, 148 bpm, "
    "rolling bassline, fast driving kick, squelchy FM synth leads, "
    "organic atmospheric textures, psychedelic soundscapes, "
    "mysterious alien sound effects, hypnotic, nocturnal, aggressive, fast-paced"
)
MODEL_ID = "mureka_v9"

TARGET_TRACKS = 20
MAX_CONSECUTIVE_FAILURES = 10
GENERATION_WAIT_SECONDS = 90  # time to wait before checking the share page
GENERATION_SUBPROCESS_TIMEOUT = 180  # seconds; kill a hung generate.py call

# 4 bars at 148 BPM: 60/148 * 4 beats/bar * 4 bars = ~6.49s
CROSSFADE_MS = int(60 / 148 * 4 * 4 * 1000)
MIN_MIX_MINUTES = 40
MAX_SILENCE_MS = 4000
SILENCE_THRESH_DB = -40

TRACKS_DIR = Path("output/tracks")
MIX_PATH = Path("output/weekly_mix.mp3")
STATUS_PATH = Path("output/pipeline_status.json")

GENERATE_SCRIPT = Path("skills/free-music-generator/scripts/generate.py")

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}


def write_status(stage: str, ok: bool, detail: str):
    """Write a small JSON file describing pipeline outcome, for the notify step."""
    STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATUS_PATH.write_text(json.dumps({
        "stage": stage,
        "ok": ok,
        "detail": detail,
    }, ensure_ascii=False, indent=2))


def generate_and_download_one(index: int) -> Path | None:
    """Generate one track via Tunee, then download the resulting mp3. Returns the local path, or None on failure."""
    title = f"DJ Lyra Ai - Darkpsy Fragment {index:03d}"

    cmd = [
        sys.executable, str(GENERATE_SCRIPT),
        "--title", title,
        "--prompt", DARKPSY_PROMPT,
        "--model", MODEL_ID,
    ]

    print(f"[{index:03d}] requesting generation: {title}", flush=True)
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, cwd="tunee-skill",
            timeout=GENERATION_SUBPROCESS_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        print(f"[{index:03d}] generation request timed out after {GENERATION_SUBPROCESS_TIMEOUT}s", flush=True)
        return None

    if proc.returncode != 0:
        print(f"[{index:03d}] generation request failed:\n{proc.stderr}", flush=True)
        return None

    try:
        gen_output = json.loads(proc.stdout.strip())
        share_url = gen_output[0]["url"]
    except (json.JSONDecodeError, KeyError, IndexError) as e:
        print(f"[{index:03d}] could not parse generate.py output: {e}\nraw: {proc.stdout}", flush=True)
        return None

    print(f"[{index:03d}] waiting {GENERATION_WAIT_SECONDS}s for generation to finish...", flush=True)
    time.sleep(GENERATION_WAIT_SECONDS)

    try:
        resp = requests.get(share_url, timeout=30, headers=HEADERS)
        mp3_urls = re.findall(r'https?://[^\s"\'\\]+\.mp3[^\s"\'\\]*', resp.text)
        if not mp3_urls:
            print(f"[{index:03d}] no mp3 URL found on share page", flush=True)
            return None

        audio_resp = requests.get(mp3_urls[0], timeout=60, headers=HEADERS)
        if audio_resp.status_code != 200 or len(audio_resp.content) < 10_000:
            print(f"[{index:03d}] download failed or file too small "
                  f"(status={audio_resp.status_code}, bytes={len(audio_resp.content)})", flush=True)
            return None

        out_path = TRACKS_DIR / f"track_{index:03d}.mp3"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(audio_resp.content)
        print(f"[{index:03d}] downloaded OK ({len(audio_resp.content)} bytes)", flush=True)
        return out_path

    except requests.RequestException as e:
        print(f"[{index:03d}] network error: {e}", flush=True)
        return None


def build_crossfaded_mix(track_paths: list[Path]) -> AudioSegment:
    """Combine tracks into one continuous mix with crossfades."""
    mix = AudioSegment.from_file(track_paths[0])
    for path in track_paths[1:]:
        next_track = AudioSegment.from_file(path)
        mix = mix.append(next_track, crossfade=CROSSFADE_MS)
    return mix


def main():
    TRACKS_DIR.mkdir(parents=True, exist_ok=True)

    # --- Phase 1: generate + download tracks, with retry on failure ---
    successful_tracks: list[Path] = []
    consecutive_failures = 0

    while len(successful_tracks) < TARGET_TRACKS:
        track_path = generate_and_download_one(len(successful_tracks) + 1)

        if track_path is not None:
            successful_tracks.append(track_path)
            consecutive_failures = 0
        else:
            consecutive_failures += 1
            print(f"consecutive failures: {consecutive_failures}/{MAX_CONSECUTIVE_FAILURES}", flush=True)
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                detail = (
                    f"{TARGET_TRACKS}曲の生成が完了しませんでした"
                    f"(連続{MAX_CONSECUTIVE_FAILURES}回の生成失敗、"
                    f"成功{len(successful_tracks)}/{TARGET_TRACKS}曲)。以下をご確認ください:\n"
                    f"1. Tunee AIのクレジット残量\n"
                    f"2. APIキーの有効期限\n"
                    f"3. Tunee AI側の障害・メンテナンス情報\n"
                    f"4. GitHub Actions内で指定している生成モデルがTuneeで使用可能か"
                )
                write_status("generation", False, detail)
                print(f"ABORTING: {detail}", flush=True)
                sys.exit(1)

    print(f"\nAll {len(successful_tracks)} tracks downloaded successfully.", flush=True)

    # --- Phase 2: crossfade into one mix ---
    print("[mix] combining tracks with crossfade...", flush=True)
    mix = build_crossfaded_mix(successful_tracks)
    duration_min = len(mix) / 1000 / 60

    # --- Phase 3: duration check ---
    if duration_min < MIN_MIX_MINUTES:
        detail = f"ミックスの生成に失敗しました(合計時間が{duration_min:.1f}分、最低{MIN_MIX_MINUTES}分に届きませんでした)"
        write_status("mix_build", False, detail)
        print(f"ABORTING: {detail}", flush=True)
        sys.exit(2)

    # --- Phase 4: silence check ---
    silent_ranges = detect_silence(mix, min_silence_len=MAX_SILENCE_MS, silence_thresh=SILENCE_THRESH_DB)
    if silent_ranges:
        detail = f"ミックスの生成に失敗しました({len(silent_ranges)}箇所の無音区間を検出)"
        write_status("mix_build", False, detail)
        print(f"ABORTING: {detail}", flush=True)
        sys.exit(2)

    # --- Phase 5: export ---
    MIX_PATH.parent.mkdir(parents=True, exist_ok=True)
    mix.export(MIX_PATH, format="mp3", bitrate="128k")
    print(f"[OK] exported {MIX_PATH} ({duration_min:.1f} minutes)", flush=True)

    write_status("complete", True, f"{duration_min:.1f}分のミックスを正常に生成しました")


if __name__ == "__main__":
    main()
