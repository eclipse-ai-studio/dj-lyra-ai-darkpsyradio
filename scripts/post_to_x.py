#!/usr/bin/env python3
"""
DJ Lyra Ai - Post to X
=========================
Posts a message to X (Twitter) using OAuth 1.0a User Context authentication.
Used for:
  - Weekly publish announcement (called from publish.yml after a successful publish)
  - Final death-mode announcement (called from death-check.yml, once only)

All posts are marked with X's native "Made with AI" disclosure label
(made_with_ai: true), since DJ Lyra Ai's posts are written and sent
autonomously by the pipeline.

Requires env vars: X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_TOKEN_SECRET

Usage:
    python scripts/post_to_x.py "message text here"
    python scripts/post_to_x.py "message text here" --image data/weekly_post.jpg

With --image, the picture is uploaded and attached. If the upload fails,
the message is still posted, without the picture.
"""
import base64
import os
import sys

import requests
from requests_oauthlib import OAuth1

X_POST_URL = "https://api.x.com/2/tweets"
X_MEDIA_URL = "https://api.x.com/2/media/upload"


def upload_image(path: str, auth) -> str | None:
    """Upload a picture and return its media id, or None if it didn't work."""
    try:
        with open(path, "rb") as f:
            data = base64.b64encode(f.read()).decode()
        r = requests.post(X_MEDIA_URL, auth=auth, json={"media": data, "media_category": "tweet_image"}, timeout=120)
        if r.status_code not in (200, 201):
            print(f"Image upload failed ({r.status_code} {r.text[:300]}); posting without the picture.")
            return None
        return r.json()["data"]["id"]
    except Exception as e:
        print(f"Image upload failed ({type(e).__name__}: {e}); posting without the picture.")
        return None


def main():
    if len(sys.argv) < 2:
        print("Usage: python scripts/post_to_x.py \"message text\"")
        sys.exit(1)

    message = sys.argv[1]
    image_path = None
    if "--image" in sys.argv:
        i = sys.argv.index("--image")
        image_path = sys.argv[i + 1] if i + 1 < len(sys.argv) else None

    api_key = os.environ.get("X_API_KEY")
    api_secret = os.environ.get("X_API_SECRET")
    access_token = os.environ.get("X_ACCESS_TOKEN")
    access_token_secret = os.environ.get("X_ACCESS_TOKEN_SECRET")

    missing = [
        name for name, val in [
            ("X_API_KEY", api_key),
            ("X_API_SECRET", api_secret),
            ("X_ACCESS_TOKEN", access_token),
            ("X_ACCESS_TOKEN_SECRET", access_token_secret),
        ] if not val
    ]
    if missing:
        print(f"Missing required env vars: {', '.join(missing)}")
        sys.exit(1)

    auth = OAuth1(
        api_key,
        client_secret=api_secret,
        resource_owner_key=access_token,
        resource_owner_secret=access_token_secret,
    )

    body = {"text": message, "made_with_ai": True}
    if image_path:
        media_id = upload_image(image_path, auth)
        if media_id:
            body["media"] = {"media_ids": [media_id]}

    response = requests.post(X_POST_URL, auth=auth, json=body, timeout=30)

    if response.status_code not in (200, 201):
        print(f"Failed to post to X: {response.status_code} {response.text}")
        sys.exit(1)

    print(f"Posted to X successfully: {response.json()}")


if __name__ == "__main__":
    main()
