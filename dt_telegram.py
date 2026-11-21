"""Telegram alerts. Needs DT_TELEGRAM_BOT_TOKEN and DT_TELEGRAM_CHAT_ID in .env.

    python dt_telegram.py "test message"
"""
from __future__ import annotations

import json
import os
import sys
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import dt_config as cfg
from dt_broker import load_env


def configured() -> bool:
    load_env()
    return bool(cfg.TELEGRAM_ENABLED and os.environ.get("DT_TELEGRAM_BOT_TOKEN") and os.environ.get("DT_TELEGRAM_CHAT_ID"))


def send(text: str) -> bool:
    """Best effort: never raises, returns True if Telegram accepted the message."""
    if not configured():
        return False
    token, chat = os.environ["DT_TELEGRAM_BOT_TOKEN"], os.environ["DT_TELEGRAM_CHAT_ID"]
    body = urlencode({"chat_id": chat, "text": text[:4000], "disable_web_page_preview": "true"}).encode()
    try:
        with urlopen(Request(f"https://api.telegram.org/bot{token}/sendMessage", data=body), timeout=10) as r:
            return bool(json.loads(r.read().decode()).get("ok"))
    except Exception:
        return False


if __name__ == "__main__":
    msg = " ".join(sys.argv[1:]) or "DT_Iris test message"
    print("configured:", configured(), "sent:", send(msg))
