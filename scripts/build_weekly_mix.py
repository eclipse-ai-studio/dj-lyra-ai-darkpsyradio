#!/usr/bin/env python3
"""
DJ Lyra Ai - Weekly Mix Pipeline
=================================
1. Generates TARGET_TRACKS dark psytrance tracks via Tunee AI (one at a time)
2. For each: waits, fetches the share page, extracts the mp3 URL, downloads it
3. Retries on failure; aborts after MAX_CONSECUTIVE_FAILURES in a row
   Each track is checked right away: silence at the start/end is trimmed off,
   and a track with a long silent gap inside (or almost empty) is thrown away
   and another one is made. Tunee is asked at most MAX_GENERATED times per run;
   if TARGET_TRACKS usable tracks still aren't there, the run stops with a
   "not enough tracks" email (before mixing)
4. Crossfades all successfully-downloaded tracks into one continuous mix
5. Checks the mix for minimum duration and long silent gaps (auto-fails if found)
6. Exports the final mix as MP3 128kbps

On any failure, writes a status file (pipeline_status.json) describing what
went wrong, for the notification step to read and email about.

Top up mode (TOP_UP=true, the "Top up" box in Run workflow):
the tracks of the previous Weekly Mix run are reused (their share URLs are read
from that run's log and downloaded again from Tunee, no credits), and only the
missing tracks are made (at most missing + TOP_UP_EXTRA). Then the mix is built
and saved as usual.

Usage:
    python scripts/build_weekly_mix.py
"""
import io
import os
import zipfile

import json
import re
import subprocess
import sys
import time
from pathlib import Path

import requests
from pydub import AudioSegment
from pydub.silence import detect_leading_silence, detect_silence

# ---- Fixed project settings ----
DARKPSY_PROMPT = (
    "darkpsy, underground darkpsy label style, instrumental, 148 bpm, "
    "rolling bassline, fast driving kick, squelchy FM synth leads, "
    "organic atmospheric textures, psychedelic soundscapes, "
    "mysterious alien sound effects, hypnotic, nocturnal, aggressive, fast-paced"
)
MODEL_ID = "mureka_v9_5"  # Mureka V9.5 (from 2026-10-04; was "mureka_v9"). 20 credits/track on the Basic plan

TARGET_TRACKS = 20     # 20 tracks ≈ 59 minutes with V9.5 (about 3:05 per track)
MAX_GENERATED = 25     # normal run: ask Tunee for at most this many tracks (failed and bad ones included)
TOP_UP_EXTRA = 5       # top up run: ask for at most (missing tracks + this)
DOWNLOAD_TRIES = 3     # the mp3 download itself is retried (no credits) before giving a track up
GENERATION_DEADLINE_MIN = 290  # stop making tracks after this (the job is killed at 350; mixing + upload need the rest)
STARTED = time.monotonic()
MAX_CONSECUTIVE_FAILURES = 10
GENERATION_WAIT_SECONDS = 300  # time to wait before checking the share page
EXTRA_CHECKS = 10              # if the track isn't ready yet, check again this many times...
EXTRA_CHECK_INTERVAL = 30      # ...every 30s (up to 5 more minutes), instead of paying for a new track
GENERATION_SUBPROCESS_TIMEOUT = 300  # seconds; kill a hung generate.py call

# 4 bars at 148 BPM: 60/148 * 4 beats/bar * 4 bars = ~6.49s
CROSSFADE_MS = int(60 / 148 * 4 * 4 * 1000)
MIN_MIX_MINUTES = 40
MAX_SILENCE_MS = 4000
SILENCE_THRESH_DB = -40
MIN_TRACK_SECONDS = 60        # a track shorter than this after trimming is treated as broken
EDGE_KEEP_MS = 50             # keep a hair of the silence at the edges so cuts aren't abrupt
SEEK_STEP_MS = 10             # silence scan step (1ms is very slow on a 60-minute mix; 10ms is plenty)

MIX_NOTES_PATH = Path("output/mix_notes.txt")  # extra lines for the "mix ready" email

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

    print(f"[{index:03d}] share_url: {share_url}", flush=True)
    print(f"[{index:03d}] waiting {GENERATION_WAIT_SECONDS}s for generation to finish...", flush=True)
    time.sleep(GENERATION_WAIT_SECONDS)
    return download_from_share_page(index, share_url, EXTRA_CHECKS)


def download_from_share_page(index: int, share_url: str, extra_checks: int) -> Path | None:
    """Find the mp3 on a Tunee share page and download it. Returns the local path, or None."""
    try:
        for check in range(extra_checks + 1):
            resp = requests.get(share_url, timeout=30, headers=HEADERS)
            print(f"[{index:03d}] share page status: {resp.status_code}, length: {len(resp.text)} chars", flush=True)
            mp3_urls = re.findall(r'https?://[^\s"\'\\]+\.mp3[^\s"\'\\]*', resp.text)
            if mp3_urls:
                break
            if check < extra_checks:
                # slower models (e.g. V9.5) may need longer: wait a bit more rather than
                # giving up and spending credits on a brand-new track
                print(f"[{index:03d}] not ready yet, checking again in {EXTRA_CHECK_INTERVAL}s "
                      f"({check + 1}/{extra_checks})", flush=True)
                time.sleep(EXTRA_CHECK_INTERVAL)
        if not mp3_urls:
            print(f"[{index:03d}] no mp3 URL found on share page", flush=True)
            print(f"[{index:03d}] share page preview (first 1000 chars):\n{resp.text[:1000]}", flush=True)
            return None

        audio_resp = None
        for attempt in range(1, DOWNLOAD_TRIES + 1):
            try:
                audio_resp = requests.get(mp3_urls[0], timeout=60, headers=HEADERS)
                if audio_resp.status_code == 200 and len(audio_resp.content) >= 10_000:
                    break
                print(f"[{index:03d}] download failed or file too small "
                      f"(status={audio_resp.status_code}, bytes={len(audio_resp.content)}), try {attempt}/{DOWNLOAD_TRIES}", flush=True)
            except requests.RequestException as e:
                print(f"[{index:03d}] download network error: {e}, try {attempt}/{DOWNLOAD_TRIES}", flush=True)
            audio_resp = None
            if attempt < DOWNLOAD_TRIES:
                time.sleep(10)
        if audio_resp is None:
            return None

        out_path = TRACKS_DIR / f"track_{index:03d}.mp3"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(audio_resp.content)
        print(f"[{index:03d}] downloaded OK ({len(audio_resp.content)} bytes)", flush=True)
        return out_path

    except requests.RequestException as e:
        print(f"[{index:03d}] network error: {e}", flush=True)
        return None


def mmss(ms: int) -> str:
    return f"{ms // 60000}:{ms // 1000 % 60:02d}"


EDGE_STATS: dict[str, tuple[int, int]] = {}  # track file -> (silence at start, at end) in ms


def load_trimmed(path: Path) -> AudioSegment:
    """Load a track and cut off the silence at its start and end.
    (Some models leave a few seconds of silence there; inside a mix that
    becomes a silent gap even after the crossfade.)"""
    seg = AudioSegment.from_file(path)
    start = detect_leading_silence(seg, silence_threshold=SILENCE_THRESH_DB, chunk_size=10)
    end = detect_leading_silence(seg.reverse(), silence_threshold=SILENCE_THRESH_DB, chunk_size=10)
    if path.name not in EDGE_STATS:
        EDGE_STATS[path.name] = (start, end)
        print(f"[edges] {path.name}: silence at start {start / 1000:.1f}s, at end {end / 1000:.1f}s", flush=True)
    start = max(0, start - EDGE_KEEP_MS)
    stop = len(seg) - max(0, end - EDGE_KEEP_MS)
    return seg[start:stop] if stop > start else seg[:0]


def gap_level(seg: AudioSegment, a: int, b: int) -> str:
    """Average loudness of the middle of a silent gap (the edges still hold the fade of the music)."""
    mid = seg[a + 500:b - 500] if b - a > 1500 else seg[a:b]
    level = mid.dBFS
    return "完全な無音" if level == float("-inf") else f"平均{level:.0f}dB"


def track_problem(path: Path) -> str | None:
    """None if the track is usable, else why not (silent gap inside / too short)."""
    seg = load_trimmed(path)
    if len(seg) < MIN_TRACK_SECONDS * 1000:
        return f"短すぎ（無音を除くと{len(seg) / 1000:.0f}秒）"
    gaps = detect_silence(seg, min_silence_len=MAX_SILENCE_MS, silence_thresh=SILENCE_THRESH_DB, seek_step=SEEK_STEP_MS)
    if gaps:
        # also say how loud the "silent" part really was, to check the threshold
        return "途中に無音 " + ", ".join(f"{mmss(a)}-{mmss(b)}（{gap_level(seg, a, b)}）" for a, b in gaps)
    return None


def build_crossfaded_mix(track_paths: list[Path]) -> AudioSegment:
    """Combine tracks into one continuous mix with crossfades."""
    mix = load_trimmed(track_paths[0])
    for path in track_paths[1:]:
        next_track = load_trimmed(path)
        mix = mix.append(next_track, crossfade=CROSSFADE_MS)
    return mix


def previous_run_share_urls() -> list[str]:
    """Read the share URLs of the tracks that were downloaded OK in the
    previous Weekly Mix run (from that run's log, via the GitHub API)."""
    repo = os.environ["GITHUB_REPOSITORY"]
    token = os.environ["GH_TOKEN"]
    this_run = os.environ.get("GITHUB_RUN_ID", "")
    api = "https://api.github.com"
    h = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    runs = requests.get(f"{api}/repos/{repo}/actions/workflows/weekly-mix.yml/runs",
                        params={"per_page": 10}, headers=h, timeout=30)
    runs.raise_for_status()
    prev = [r for r in runs.json()["workflow_runs"] if str(r["id"]) != this_run and r["status"] == "completed"]
    if not prev:
        raise TopUpRefused("前回のWeekly Mixの実行が見つかりませんでした。")
    run = prev[0]
    print(f"[reuse] reading the log of run {run['id']} ({run['created_at']}, {run['conclusion']})", flush=True)
    if run["conclusion"] == "success":
        # the last run made a mix already: topping up would only rebuild a copy from the same tracks
        raise TopUpRefused("前回の実行は成功していて、ミックスはできています（Top upは不要です）。"
                           "ミックスを作り直したいときは、チェックを入れずに Run workflow を実行してください。")
    logs = requests.get(f"{api}/repos/{repo}/actions/runs/{run['id']}/logs", headers=h, timeout=60)
    logs.raise_for_status()
    text = ""
    with zipfile.ZipFile(io.BytesIO(logs.content)) as z:
        for name in sorted(z.namelist()):
            if "Build weekly mix" in name or name.count("/") == 0:
                text += z.read(name).decode("utf-8", "replace") + "\n"
    current, ok = {}, {}
    for line in text.splitlines():
        m = re.search(r"\[(\d{3})\] share_url: (\S+)", line)
        if m:
            current[m.group(1)] = m.group(2)
            continue
        m = re.search(r"\[(\d{3})\] downloaded OK", line)
        if m and m.group(1) in current:
            ok[m.group(1)] = current[m.group(1)]
    urls = [ok[k] for k in sorted(ok)]
    # the same index can appear in more than one pass of the log file set; keep the order, drop repeats
    seen, out = set(), []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    print(f"[reuse] found {len(out)} tracks in the previous run", flush=True)
    if not out:
        # nothing to reuse (e.g. the last run stopped right away): don't silently make 25 new tracks
        raise TopUpRefused("前回の実行には使い回せる曲がありませんでした（すぐ止まった回など）。"
                           "曲を一から作る場合は、チェックを入れずに Run workflow を実行してください。")
    return out


class TopUpRefused(Exception):
    """Top up should not run; nothing was generated (no credits used)."""


def write_mix_notes(tracks: list[Path], dropped: list[str]):
    """A few lines for the 'mix ready' email: how much silence was trimmed (to check the cause)."""
    used = [EDGE_STATS[p.name] for p in tracks if p.name in EDGE_STATS]
    if not used:
        return
    starts = [s for s, _ in used]
    ends = [e for _, e in used]
    lines = [
        f"使った曲: {len(tracks)}曲",
        f"曲の頭の無音: 最大{max(starts) / 1000:.1f}秒、平均{sum(starts) / len(starts) / 1000:.1f}秒",
        f"曲の終わりの無音: 最大{max(ends) / 1000:.1f}秒、平均{sum(ends) / len(ends) / 1000:.1f}秒",
        f"頭と終わりの無音の合計が6.5秒を超えていた曲のつなぎ目: "
        f"{sum(1 for a, b in zip(ends, starts[1:]) if a + b > CROSSFADE_MS)}か所（カット済み）",
    ]
    if dropped:
        lines.append("外した曲: " + " / ".join(dropped))
    MIX_NOTES_PATH.parent.mkdir(parents=True, exist_ok=True)
    MIX_NOTES_PATH.write_text("\n".join(lines) + "\n")
    print("[notes]\n" + "\n".join(lines), flush=True)


def finish_mix(successful_tracks: list[Path], dropped: list[str]):
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
    silent_ranges = detect_silence(mix, min_silence_len=MAX_SILENCE_MS, silence_thresh=SILENCE_THRESH_DB, seek_step=SEEK_STEP_MS)
    if silent_ranges:
        where = ", ".join(f"{mmss(a)}-{mmss(b)}" for a, b in silent_ranges)
        detail = f"ミックスの生成に失敗しました({len(silent_ranges)}箇所の無音区間を検出: {where})"
        write_status("mix_build", False, detail)
        print(f"ABORTING: {detail}", flush=True)
        sys.exit(2)

    # --- Phase 5: export ---
    MIX_PATH.parent.mkdir(parents=True, exist_ok=True)
    mix.export(MIX_PATH, format="mp3", bitrate="128k")
    print(f"[OK] exported {MIX_PATH} ({duration_min:.1f} minutes, {len(successful_tracks)} tracks)", flush=True)
    write_mix_notes(successful_tracks, dropped)

    write_status("complete", True, f"{duration_min:.1f}分のミックスを正常に生成しました")


TIMED_OUT = [False]  # set when make_tracks stopped because of the time limit
LOG_INDEX = [0]  # running number printed in the log as [NNN]; unique per run so a later top up can read the log


def next_index() -> int:
    LOG_INDEX[0] += 1
    return LOG_INDEX[0]


def check_track(index: int, path: Path) -> str | None:
    try:
        problem = track_problem(path)
    except Exception as e:  # file can't be decoded
        problem = f"音声ファイルを読めない（{type(e).__name__}: {e}）"
    if problem:
        print(f"[{index:03d}] track left out: {problem}", flush=True)
        path.unlink(missing_ok=True)
    return problem


def make_tracks(tracks: list[Path], dropped: list[str], max_new: int) -> bool:
    """Make new tracks until there are TARGET_TRACKS usable ones, asking Tunee
    at most max_new times (every request counts, even if the download failed,
    because it may have used credits). Returns True if the target was reached."""
    made = 0
    consecutive_failures = 0
    while len(tracks) < TARGET_TRACKS:
        if made >= max_new:
            return False
        if (time.monotonic() - STARTED) / 60 > GENERATION_DEADLINE_MIN:
            print(f"[deadline] {GENERATION_DEADLINE_MIN} minutes used, stopping so the job isn't killed", flush=True)
            TIMED_OUT[0] = True
            return False
        index = next_index()
        made += 1
        path = generate_and_download_one(index)
        if path is None:
            consecutive_failures += 1
            print(f"consecutive failures: {consecutive_failures}/{MAX_CONSECUTIVE_FAILURES}", flush=True)
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                detail = (
                    f"{TARGET_TRACKS}曲の生成が完了しませんでした"
                    f"(連続{MAX_CONSECUTIVE_FAILURES}回の生成失敗、"
                    f"成功{len(tracks)}/{TARGET_TRACKS}曲)。以下をご確認ください:\n"
                    f"1. Tunee AIのクレジット残量\n"
                    f"2. APIキーの有効期限\n"
                    f"3. Tunee AI側の障害・メンテナンス情報\n"
                    f"4. GitHub Actions内で指定している生成モデルがTuneeで使用可能か"
                )
                write_status("generation", False, detail)
                print(f"ABORTING: {detail}", flush=True)
                sys.exit(1)
            continue
        consecutive_failures = 0
        problem = check_track(index, path)
        if problem:
            dropped.append(f"{index:03d}（{problem}）")
        else:
            tracks.append(path)
        print(f"[progress] usable {len(tracks)}/{TARGET_TRACKS}, requests {made}/{max_new} this run", flush=True)
    return True


def not_enough(tracks: list[Path], dropped: list[str], detail: str):
    if TIMED_OUT[0]:
        detail = "（Tuneeの生成が遅く、時間切れで途中までになりました）\n" + detail
    if dropped:
        detail += "\n\n使えなかった曲: " + " / ".join(dropped)
    write_status("generation", False, detail)
    print(f"ABORTING: {detail}", flush=True)
    sys.exit(1)


def main():
    TRACKS_DIR.mkdir(parents=True, exist_ok=True)
    tracks: list[Path] = []
    dropped: list[str] = []

    if os.environ.get("TOP_UP", "").lower() == "true":
        # --- top up: reuse the previous run's tracks, make only the missing ones ---
        try:
            urls = previous_run_share_urls()
        except TopUpRefused as e:
            write_status("top_up", False, f"Top upを中止しました（曲は作っていません。クレジットは使っていません）。\n{e}")
            print(f"ABORTING: {e}", flush=True)
            sys.exit(1)
        except Exception as e:
            write_status("top_up", False, f"前回の曲の読み込みに失敗しました（{type(e).__name__}: {e}）。"
                                          f"曲は作っていません。Claudeに相談してください。")
            print(f"ABORTING: {e}", flush=True)
            sys.exit(1)
        for url in urls:
            if len(tracks) >= TARGET_TRACKS:
                break
            index = next_index()
            print(f"[{index:03d}] share_url: {url}", flush=True)  # same format, so a later top up can read this log too
            path = download_from_share_page(index, url, 0)
            if path is None:
                dropped.append(f"{index:03d}（ダウンロード失敗）")
                continue
            problem = check_track(index, path)
            if problem:
                dropped.append(f"{index:03d}（{problem}）")
            else:
                tracks.append(path)
        missing = TARGET_TRACKS - len(tracks)
        print(f"[top up] reused {len(tracks)} tracks, making up to {missing + TOP_UP_EXTRA} new ones for the {missing} missing", flush=True)
        if missing > 0 and not make_tracks(tracks, dropped, missing + TOP_UP_EXTRA):
            not_enough(tracks, dropped,
                       f"曲生成のリトライに失敗し、{TARGET_TRACKS}曲そろいませんでした"
                       f"（使える曲 {len(tracks)}/{TARGET_TRACKS}曲、足りない{missing}曲＋{TOP_UP_EXTRA}回までTuneeに頼みました）。\n"
                       f"何かがおかしい可能性があります（Tuneeの生成モデルの不調など）。Claudeに相談してください。")
    else:
        # --- normal weekly run ---
        if not make_tracks(tracks, dropped, MAX_GENERATED):
            not_enough(tracks, dropped,
                       f"曲がそろいませんでした（Tuneeに{MAX_GENERATED}回頼んで、使える曲は{len(tracks)}/{TARGET_TRACKS}曲）。\n"
                       f"足りない曲を作ってミックスまで仕上げるには、GitHubのActions → Weekly Mix Pipeline → "
                       f"Run workflow で「Top up」にチェックを入れて実行してください"
                       f"（そろった曲はそのまま使い、足りない分だけ作ります）。")

    print(f"\nAll {len(tracks)} tracks ready.", flush=True)
    finish_mix(tracks, dropped)


if __name__ == "__main__":
    main()
