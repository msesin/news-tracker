#!/usr/bin/env python3
"""Run by systemd (OnFailure=) when the tracker dies. Sends a plain-language DM
with the cause, then re-checks later and reports whether it came back."""
import html
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

# Reads .env directly instead of importing config.py, so alerting still works
# when a broken config is the very thing that took the tracker down.
_HERE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(_HERE, ".env"))

TOKEN = os.environ.get("ALERT_BOT_TOKEN", "")
CHAT_ID = os.environ.get("ALERT_CHAT_ID", "")
FOLLOWUP_DELAY = 900

# systemd's terse result codes, in words a human can act on.
REASONS = {
    "exit-code": "the program hit an error and quit",
    "signal": "the program was stopped unexpectedly",
    "oom-kill": "the server ran out of memory",
    "timeout": "the program stopped responding",
    "core-dump": "the program crashed",
    "watchdog": "the program froze",
    "success": "the program was stopped normally",
}


def send(text: str) -> None:
    resp = requests.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML",
              "disable_web_page_preview": True},
        timeout=10,
    )
    resp.raise_for_status()


def sh(*cmd: str) -> str:
    return subprocess.run(cmd, capture_output=True, text=True).stdout.strip()


def main() -> None:
    unit = sys.argv[1] if len(sys.argv) > 1 else "news-tracker.service"
    when = datetime.now(timezone.utc).strftime("%d %b %Y, %H:%M UTC")
    result = sh("systemctl", "show", unit, "-p", "Result", "--value")
    why = REASONS.get(result, "the program stopped for an unknown reason")
    logs = "\n".join(sh("journalctl", "-u", unit, "-n", "8", "--no-pager", "-o", "cat").splitlines()[-8:])

    send(
        f"🔴 <b>News tracker stopped</b>\n\n"
        f"The bot that watches for mobilization news is not running, so "
        f"<b>you will not receive updates</b> until it is back.\n\n"
        f"<b>When:</b> {when}\n"
        f"<b>What happened:</b> {why}\n\n"
        f"It should restart itself automatically. I'll check again in "
        f"{FOLLOWUP_DELAY // 60} minutes and tell you either way.\n\n"
        f"<b>Details (for troubleshooting):</b>\n"
        f"<pre>{html.escape(logs[-1200:]) or 'no details available'}</pre>"
    )

    time.sleep(FOLLOWUP_DELAY)

    if sh("systemctl", "is-active", unit) == "active":
        send(
            f"🟢 <b>News tracker is back</b>\n\n"
            f"It restarted on its own and is watching the news channels again. "
            f"You will receive mobilization updates as normal.\n\n"
            f"<b>Nothing for you to do.</b>"
        )
    else:
        send(
            f"🔴 <b>News tracker is still down</b>\n\n"
            f"It did not come back on its own after {FOLLOWUP_DELAY // 60} minutes. "
            f"<b>You are not receiving mobilization updates right now.</b>\n\n"
            f"<b>What to do:</b> connect to the server and run:\n"
            f"<code>sudo systemctl restart news-tracker</code>\n\n"
            f"If that doesn't fix it, paste this message to Claude and ask for help."
        )


if __name__ == "__main__":
    main()
