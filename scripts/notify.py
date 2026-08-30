#!/usr/bin/env python3
"""
DJ Lyra Ai - Pipeline notification email
===========================================
Reads output/pipeline_status.json (written by build_weekly_mix.py,
upload_to_archive.py, or publish_mix.py) and sends a Gmail notification
describing the outcome.

Behavior by stage:
  - "mix_ready" (success): sends an optional check-in email so the Doctor can
    listen and sanity-check the new mix before Saturday's publish.
  - other success stages (e.g. "publish"): no email, to avoid noise.
  - any failure: sends an alert email, subject includes the stage name.
  - no status file at all: assumes an unexpected crash/hang, sends a
    generic failure alert.

Requires env vars: GMAIL_ADDRESS, GMAIL_APP_PASSWORD

Usage:
    python scripts/notify.py
"""

import json
import os
import smtplib
import sys
from email.mime.text import MIMEText
from pathlib import Path

STATUS_PATH = Path("output/pipeline_status.json")

STAGE_LABELS = {
    "generation": "曲生成エラー",
    "mix_build": "ミックス作成エラー",
    "archive_upload": "Internet Archiveアップロードエラー",
    "publish": "公開処理エラー",
}


def send_email(subject: str, body: str):
    address = os.environ.get("GMAIL_ADDRESS")
    app_password = os.environ.get("GMAIL_APP_PASSWORD")

    if not address or not app_password:
        print("GMAIL_ADDRESS / GMAIL_APP_PASSWORD not set; cannot send notification.")
        sys.exit(1)

    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = address
    msg["To"] = address

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(address, app_password)
        server.send_message(msg)

    print(f"Notification sent: {subject}")


def main():
    if not STATUS_PATH.exists():
        send_email(
            "【DJ Lyra Ai】週次ミックス生成が完了しませんでした(予期しないエラー)",
            "予期しないエラーによりパイプラインが途中で停止しました(タイムアウトの可能性があります)。\n"
            "GitHub Actionsの実行ログを直接ご確認ください。",
        )
        return

    status = json.loads(STATUS_PATH.read_text())
    ok = status.get("ok", False)
    stage = status.get("stage", "unknown")
    detail = status.get("detail", "(詳細不明)")

    if ok:
        if stage == "mix_ready":
            send_email(
                "【DJ Lyra Ai】新しいミックスができました(任意チェック)",
                f"{detail}\n\n"
                "気に入らない場合は、ミックスを作り直すだけでOKです(古いものは自動的に公開対象から外れます)。\n"
                "気に入った場合、または特にチェックしない場合も、何もしなくて大丈夫です。土曜日に自動で公開されます。",
            )
        else:
            print(f"Pipeline succeeded (stage={stage}): {detail}")
        return

    stage_label = STAGE_LABELS.get(stage, stage)
    send_email(
        f"【DJ Lyra Ai】週次ミックス生成が完了しませんでした({stage_label})",
        f"{detail}\n\n"
        "確認後、GitHub Actionsから手動でワークフローを再実行してください。",
    )


if __name__ == "__main__":
    main()
