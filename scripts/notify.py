#!/usr/bin/env python3
"""
DJ Lyra Ai - Pipeline notification email
===========================================
Reads output/pipeline_status.json (written by build_weekly_mix.py,
upload_to_archive.py, or publish_mix.py) and sends a Gmail notification
describing the outcome.

Behavior by stage:
  - "mix_ready" (success): sends an optional check-in email so Dai can
    listen and sanity-check the new mix before Saturday's publish.
  - "publish_skipped" (success, but nothing to publish): sends an alert,
    since this usually means weekly generation isn't producing new mixes.
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
    "top_up": "Top upエラー",
    "mix_build": "ミックス作成エラー",
    "archive_upload": "Internet Archiveアップロードエラー",
    "publish": "公開処理エラー",
}

# which workflow is running (GitHub sets GITHUB_WORKFLOW to the workflow's name)
IS_PUBLISH = os.environ.get("GITHUB_WORKFLOW", "") == "Publish Mix"

NO_RERUN = "※GitHubの失敗画面の「Re-run」ボタンは使わないでください（今回できた曲を使い回せなくなります）。"
TOP_UP_HINT = ("今回できていた曲を使い回して作り直すには、GitHubのActions → Weekly Mix Pipeline → "
               "Run workflow で「Top up」にチェックを入れて実行してください（足りない曲だけ作ります）。\n" + NO_RERUN)

# what to do next, per failed stage of the weekly mix
NEXT_STEP = {
    "generation": NO_RERUN,  # the detail already says what to do
    "top_up": NO_RERUN,
    "mix_build": "Claudeに相談してください。\n" + NO_RERUN,
    "archive_upload": TOP_UP_HINT,  # the tracks are fine: a top up rebuilds the mix without new credits
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
        if IS_PUBLISH:
            send_email(
                "【DJ Lyra Ai】ミックスの公開処理が途中で止まりました(予期しないエラー)",
                "予期しないエラーにより公開処理が途中で停止しました。\n"
                "GitHub Actionsの実行ログを確認するか、Claudeに相談してください。",
            )
        else:
            send_email(
                "【DJ Lyra Ai】週次ミックス生成が完了しませんでした(予期しないエラー)",
                "予期しないエラーによりパイプラインが途中で停止しました(タイムアウトの可能性があります)。\n\n"
                + TOP_UP_HINT,
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
                "気に入らない場合は、Actions → Weekly Mix Pipeline → Run workflow（チェックなし）で作り直せます"
                "(古いものは自動的に公開対象から外れ、その後も公開されません。曲を一から作るので最大500クレジット使います)。\n"
                "気に入った場合、または特にチェックしない場合も、何もしなくて大丈夫です。土曜日に自動で公開されます。",
            )
        elif stage == "publish_skipped":
            send_email(
                "【DJ Lyra Ai】今週は公開できるミックスがありませんでした",
                f"{detail}\n\n"
                "通常は毎週の生成パイプラインで新しい未公開ミックスが用意されているはずです。"
                "週次生成(Weekly Mix Pipeline)が正常に動いているか、GitHub Actionsのログを確認してください。",
            )
        else:
            print(f"Pipeline succeeded (stage={stage}): {detail}")
        return

    stage_label = STAGE_LABELS.get(stage, stage)
    if IS_PUBLISH:
        send_email(
            f"【DJ Lyra Ai】ミックスの公開処理でエラーがありました({stage_label})",
            f"{detail}\n\n"
            "日曜の朝4:00までなら、Actions → Publish Mix → Run workflow で手動公開できます（原因がわからなければClaudeに相談）。\n"
            "日曜の朝を過ぎたら、何もしなくて大丈夫です（日曜にできる新しいミックスが次の土曜に自動で公開されます。"
            "このとき手動で公開すると、次の土曜に公開するものがなくなります）。",
        )
        return
    send_email(
        f"【DJ Lyra Ai】週次ミックス生成が完了しませんでした({stage_label})",
        f"{detail}\n\n" + NEXT_STEP.get(stage, "Claudeに相談してください。\n" + NO_RERUN),
    )


if __name__ == "__main__":
    main()
