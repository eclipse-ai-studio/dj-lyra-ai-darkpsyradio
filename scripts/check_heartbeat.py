import json
import os
from datetime import datetime, timezone

MIXES_PATH = "docs/mixes.json"
HEARTBEAT_PATH = "docs/heartbeat.json"

# 死亡モードの閾値: 12週連続(約3ヶ月)新ミックスが公開されなければ death_mode = true に固定する
DEATH_THRESHOLD_WEEKS = 12
FRESHNESS_DAYS = 7

def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")

def main():
    heartbeat = load_json(HEARTBEAT_PATH)

    # 既に死亡モードなら何もしない(カウンターは12で固定、コミットも発生させない)
    if heartbeat.get("death_mode", False):
        print("Already in death mode. Nothing to do.")
        github_output = os.environ.get("GITHUB_OUTPUT")
        if github_output:
            with open(github_output, "a", encoding="utf-8") as f:
                f.write("newly_dead=false\n")
        return

    mixes = load_json(MIXES_PATH)
    published = [m for m in mixes if m.get("published")]
    now = datetime.now(timezone.utc)

    if published:
        latest = max(published, key=lambda m: m["date"])
        latest_date = datetime.fromisoformat(latest["date"]).replace(tzinfo=timezone.utc)
        days_since = (now - latest_date).days
        heartbeat["last_published_date"] = latest["date"]
    else:
        # プロジェクト開始直後などpublished実績が一度もない場合は未達成扱い
        days_since = FRESHNESS_DAYS + 1

    if days_since <= FRESHNESS_DAYS:
        heartbeat["missed_weeks"] = 0
    else:
        heartbeat["missed_weeks"] = heartbeat.get("missed_weeks", 0) + 1

    if heartbeat["missed_weeks"] >= DEATH_THRESHOLD_WEEKS:
        heartbeat["death_mode"] = True

    heartbeat["last_checked"] = now.isoformat()
    save_json(HEARTBEAT_PATH, heartbeat)

    newly_dead = heartbeat["death_mode"]

    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a", encoding="utf-8") as f:
            f.write(f"newly_dead={'true' if newly_dead else 'false'}\n")

if __name__ == "__main__":
    main()
