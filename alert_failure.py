#!/usr/bin/env python3
"""Run by systemd (ExecStopPost=) every time the tracker stops. Sends a
plain-language DM only when the stop was NOT a clean, intentional stop."""
import html
import os
import subprocess
import sys

import requests
from dotenv import load_dotenv

# Reads .env directly instead of importing config.py, so alerting still works
# when a broken config is the very thing that took the tracker down.
_HERE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(_HERE, ".env"))
_MARKER = os.path.join(_HERE, ".last_failure")

TOKEN = os.environ.get("ALERT_BOT_TOKEN", "")
CHAT_ID = os.environ.get("ALERT_CHAT_ID", "")

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


def sh(*cmd: str) -> str:
    return subprocess.run(cmd, capture_output=True, text=True).stdout.strip()


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

    send(
        f"🔴 <b>News tracker stopped</b>\n\n"
        f"The bot that watches for mobilization news is not running, so "
        f"<b>you will not receive updates</b> until it is back.\n\n"
        f"<b>What happened:</b> {why}\n\n"
        f"It should restart itself automatically within a few seconds. "
        f"You'll get a separate \"back up\" message the moment that's confirmed.\n\n"
        f"<b>Details (for troubleshooting):</b>\n"
        f"<pre>{html.escape(logs[-1200:]) or 'no details available'}</pre>"
    )


if __name__ == "__main__":
    main()
