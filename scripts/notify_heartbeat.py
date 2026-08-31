#!/usr/bin/env python3
"""
DJ Lyra Ai - Heartbeat notification email
=============================================
Called from death-check.yml whenever missed_weeks increases (counter > 0),
or once when death_mode newly flips from false to true.

Usage:
    python scripts/notify_heartbeat.py <missed_weeks> <newly_dead: true|false>
"""
import os
import smtplib
import sys
from email.mime.text import MIMEText

DEATH_THRESHOLD_WEEKS = 12


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
    missed_weeks = int(sys.argv[1])
    newly_dead = sys.argv[2] == "true"

    if newly_dead:
        send_email(
            "【DJ Lyra Ai】死亡モードに入りました",
            "12週連続で新しいミックスが公開されなかったため、死亡モードに入りました。\n"
            "今後、生成・公開・通知は全て自動的に停止します。GitHub Pagesのアーカイブは引き続き閲覧可能です。",
        )
        return

    send_email(
        f"【DJ Lyra Ai】デスカウンターが{missed_weeks}になりました",
        f"デスカウンターが{missed_weeks}になりました。"
        f"カウンター{DEATH_THRESHOLD_WEEKS}に達するとサイトはデスモードになり、"
        "Xに最終投稿が公開されます。状況を確認してください。",
    )


if __name__ == "__main__":
    main()
