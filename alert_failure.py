#!/usr/bin/env python3
"""Run by systemd (ExecStopPost=) every time the tracker stops. Sends a
plain-language DM only when the stop was NOT a clean, intentional stop.

A single crash is the tracker's own business: systemd restarts it in seconds
and subscribers never need to know. An outage that survives 15 minutes of
self-recovery is different — by then the channel's silence is indistinguishable
from "no news", which is exactly the wrong impression to leave — so the channel
gets a one-line notice too.
"""
import html
import json
import os
import subprocess
import sys
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

# Reads .env directly instead of importing config.py, so alerting still works
# when a broken config is the very thing that took the tracker down.
_HERE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(_HERE, ".env"))
_MARKER = os.path.join(_HERE, ".last_failure")
_DOWNTIME = os.path.join(_HERE, ".downtime")

TOKEN = os.environ.get("ALERT_BOT_TOKEN", "")
CHAT_ID = os.environ.get("ALERT_CHAT_ID", "")
NEWS_TOKEN = os.environ.get("NEWS_BOT_TOKEN", "")
NEWS_CHAT_ID = os.environ.get("NEWS_CHAT_ID", "")

# How long the tracker may keep failing on its own before subscribers are told.
# systemd retries every 5 s (RestartSec=), so 15 minutes of continuous failure
# is well past "transient blip". It also matches the dead man's switch's own
# ~15 min detection window, so both layers escalate at the same moment rather
# than at two confusingly different times.
_ESCALATE_AFTER = 15 * 60

CHANNEL_DOWN_MESSAGE = (
    "⚠️ Бот тимчасово не працює — стежте за новинами самостійно. "
    "Повідомимо, щойно він відновиться."
)

# systemd's terse result codes, in words a human can act on.
REASONS = {
    "exit-code": "the program hit an error and quit",
    "signal": "the program was stopped unexpectedly (crashed or was killed)",
    "oom-kill": "the server ran out of memory",
    "timeout": "the program stopped responding",
    "core-dump": "the program crashed",
    "watchdog": "the program froze",
}


def send(text: str) -> None:
    resp = requests.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML",
              "disable_web_page_preview": True},
        timeout=10,
    )
    resp.raise_for_status()


def send_to_channel(text: str) -> None:
    """Posts with the news bot, not the alert bot — the alert bot has no
    business in the public channel and isn't an admin there."""
    resp = requests.post(
        f"https://api.telegram.org/bot{NEWS_TOKEN}/sendMessage",
        json={"chat_id": NEWS_CHAT_ID, "text": text,
              "disable_web_page_preview": True},
        timeout=10,
    )
    resp.raise_for_status()


def sh(*cmd: str) -> str:
    """Never raises: collecting log context is a nice-to-have, and must not be
    the reason an outage alert goes unsent."""
    try:
        return subprocess.run(cmd, capture_output=True, text=True).stdout.strip()
    except Exception as e:
        return f"(could not read logs: {e})"


def _load_downtime() -> dict:
    try:
        with open(_DOWNTIME) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def escalate_if_still_down() -> None:
    """Post to the channel once, and only once, per outage.

    The clock starts at the first failed stop and is cleared by main.py only
    after the tracker has proven it can stay up — so a crash loop accumulates
    towards the 15 minutes instead of resetting the timer on every restart.
    """
    state = _load_downtime()
    now = datetime.now(timezone.utc)
    try:
        since = datetime.fromisoformat(state["since"])
    except (KeyError, ValueError):
        since = now
        state = {"since": now.isoformat(), "channel_notified": False}

    if (not state.get("channel_notified")
            and (now - since).total_seconds() >= _ESCALATE_AFTER):
        try:
            send_to_channel(CHANNEL_DOWN_MESSAGE)
            state["channel_notified"] = True
        except Exception as e:
            print(f"[ALERT] channel notice failed: {e}", file=sys.stderr)

    with open(_DOWNTIME, "w") as f:
        json.dump(state, f)


def main() -> None:
    unit = sys.argv[1] if len(sys.argv) > 1 else "news-tracker.service"

    # systemd sets this for ExecStopPost=. A clean `systemctl stop` reports
    # "success" here — skip alerting for those, only report real failures.
    result = os.environ.get("SERVICE_RESULT", "unknown")
    if result == "success":
        return

    why = REASONS.get(result, "the program stopped for an unknown reason")
    logs = "\n".join(sh("journalctl", "-u", unit, "-n", "8", "--no-pager", "-o", "cat").splitlines()[-8:])

    # Read by main.py on its next successful start, so it can confirm
    # recovery itself instead of waiting on healthchecks.io's ~15 min
    # detection window, which never notices a crash this quick.
    with open(_MARKER, "w") as f:
        f.write(why)

    # Wrapped so a failing DM (revoked alert token, Telegram rate limit)
    # can't stop the channel escalation below from being evaluated.
    try:
        send(
            f"⚠️ <b>TRACKER DOWN</b> — {why}.\n"
            f"Retrying automatically — I'll confirm once it's back.\n\n"
            f"<pre>{html.escape(logs[-1200:]) or 'no details available'}</pre>"
        )
    except Exception as e:
        print(f"[ALERT] DM failed: {e}", file=sys.stderr)

    escalate_if_still_down()


if __name__ == "__main__":
    main()
