#!/usr/bin/env python3
"""Run by systemd (ExecStopPost=) every time the tracker stops. Sends a
plain-language DM only when the stop was NOT a clean, intentional stop.

Telling the *channel* about this class of outage (process crashed / server
unreachable) is deliberately not this script's job — healthchecks.io's own
webhook integration covers it, since it detects the same thing (no
heartbeat) even in cases where this script can't run at all (e.g. the whole
server is down). Doing it twice would mean two independent 15-minute clocks
racing to post the same notice.
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
_STATE = os.path.join(_HERE, ".downtime")

TOKEN = os.environ.get("ALERT_BOT_TOKEN", "")
CHAT_ID = os.environ.get("ALERT_CHAT_ID", "")

# systemd retries every 5 s, so a tracker that cannot start at all fails ~150
# times before 15 minutes are up. Without a cooldown that is ~150 DMs for a
# single outage, which buries the one message that mattered — the first. Old
# timestamps left over from a past, already-resolved outage don't need to be
# cleared for this to work: `_due()` compares against "now", so a last_dm from
# days ago is just as stale as no last_dm at all.
_DM_COOLDOWN = 10 * 60

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
    """Never raises: collecting log context is a nice-to-have, and must not be
    the reason an outage alert goes unsent."""
    try:
        return subprocess.run(cmd, capture_output=True, text=True).stdout.strip()
    except Exception as e:
        return f"(could not read logs: {e})"


def _load_state() -> dict:
    try:
        with open(_STATE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _due(last: str | None, now: datetime, cooldown: int) -> bool:
    """True if `cooldown` has passed since `last` — or if there is no `last` yet,
    so the first DM of an outage always goes out immediately."""
    try:
        return (now - datetime.fromisoformat(last)).total_seconds() >= cooldown
    except (TypeError, ValueError):
        return True


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

    state = _load_state()
    now = datetime.now(timezone.utc)
    if _due(state.get("last_dm"), now, _DM_COOLDOWN):
        try:
            send(
                f"⚠️ <b>TRACKER DOWN</b> — {why}.\n"
                f"Retrying automatically — I'll confirm once it's back.\n\n"
                f"<pre>{html.escape(logs[-1200:]) or 'no details available'}</pre>"
            )
            state["last_dm"] = now.isoformat()
            with open(_STATE, "w") as f:
                json.dump(state, f)
        except Exception as e:
            print(f"[ALERT] DM failed: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
