#!/usr/bin/env python3
"""
DJ Lyra Ai - Pipeline notification email
===========================================
Reads output/pipeline_status.json (written by build_weekly_mix.py or
upload_to_archive.py) and sends a Gmail notification describing the outcome.

If no status file exists, assumes something crashed before it could even
write one, and sends a generic failure alert.

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
            "【DJ Lyra Ai】週次ミックス生成が完了しませんでした",
            "予期しないエラーによりパイプラインが途中で停止しました。\n"
            "GitHub Actionsの実行ログを直接ご確認ください。",
        )
        return

    status = json.loads(STATUS_PATH.read_text())
    ok = status.get("ok", False)
    detail = status.get("detail", "(詳細不明)")

    if ok:
        print(f"Pipeline succeeded: {detail}")
        return

    send_email(
        "【DJ Lyra Ai】週次ミックス生成が完了しませんでした",
        f"{detail}\n\n"
        "確認後、GitHub Actionsから手動でワークフローを再実行してください。",
    )


if __name__ == "__main__":
    main()
