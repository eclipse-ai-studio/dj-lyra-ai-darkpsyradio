#!/usr/bin/env python3
"""
DJ Lyra Ai - Post to X
=========================
Posts a message to X (Twitter) using OAuth 1.0a User Context authentication.
Used for:
  - Weekly publish announcement (called from publish.yml after a successful publish)
  - Final death-mode announcement (called from death-check.yml, once only)

Requires env vars: X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_TOKEN_SECRET

Usage:
    python scripts/post_to_x.py "message text here"
"""
import os
import sys

import requests
from requests_oauthlib import OAuth1

X_POST_URL = "https://api.x.com/2/tweets"


def main():
    if len(sys.argv) < 2:
        print("Usage: python scripts/post_to_x.py \"message text\"")
        sys.exit(1)

    message = sys.argv[1]

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

    response = requests.post(
        X_POST_URL,
        auth=auth,
        json={"text": message},
        timeout=30,
    )

    if response.status_code not in (200, 201):
        print(f"Failed to post to X: {response.status_code} {response.text}")
        sys.exit(1)

    print(f"Posted to X successfully: {response.json()}")


if __name__ == "__main__":
    main()
