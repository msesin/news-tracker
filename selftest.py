#!/usr/bin/env python3
"""Pre-deploy checks: env vars, both bots, delivery, healthcheck, peer-id logic.

Sends one real test message to the channel and one DM, so you can confirm
delivery end to end. Safe to run on the laptop or the server — it never opens
the Telethon session, so it can't clash with a running tracker.
"""
import os
import shutil
import subprocess
import sys

import requests
from dotenv import load_dotenv
from telethon import utils
from telethon.tl.types import Channel

load_dotenv()

REQUIRED = [
    "TELEGRAM_API_ID",
    "TELEGRAM_API_HASH",
    "NEWS_BOT_TOKEN",
    "NEWS_CHAT_ID",
    "ALERT_BOT_TOKEN",
    "ALERT_CHAT_ID",
    "HEALTHCHECK_URL",
    "LLM_API_KEY",
]

failures = []


def check(label: str, ok: bool, detail: str = "") -> bool:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{f'  — {detail}' if detail else ''}")
    if not ok:
        failures.append(label)
    return ok


def api(token: str, method: str, **payload):
    resp = requests.post(
        f"https://api.telegram.org/bot{token}/{method}", json=payload, timeout=15
    )
    return resp.json()


print("\n[1] Environment variables")
for name in REQUIRED:
    value = os.environ.get(name, "")
    if not value:
        check(name, False, "missing from .env")
    elif value.startswith("replace_with"):
        check(name, False, "still a placeholder")
    else:
        check(name, True, f"set ({len(value)} chars)")

if failures:
    print("\nFix the .env entries above before running the rest.\n")
    sys.exit(1)

print("\n[2] Bot tokens")
for label, token in (
    ("news bot", os.environ["NEWS_BOT_TOKEN"]),
    ("alert bot", os.environ["ALERT_BOT_TOKEN"]),
):
    data = api(token, "getMe")
    check(
        f"{label} token valid",
        data.get("ok", False),
        f"@{data['result']['username']}" if data.get("ok") else data.get("description", ""),
    )

print("\n[3] Message delivery")
news = api(
    os.environ["NEWS_BOT_TOKEN"],
    "sendMessage",
    chat_id=int(os.environ["NEWS_CHAT_ID"]),
    text="[TEST] Channel delivery works. Mobilization updates will arrive here.",
)
check(
    "news bot -> channel",
    news.get("ok", False),
    news.get("result", {}).get("chat", {}).get("title", "") or news.get("description", ""),
)

alert = api(
    os.environ["ALERT_BOT_TOKEN"],
    "sendMessage",
    chat_id=int(os.environ["ALERT_CHAT_ID"]),
    text="[TEST] Alert DM works. Error and downtime alerts will arrive here.",
)
check(
    "alert bot -> your DM",
    alert.get("ok", False),
    alert.get("result", {}).get("chat", {}).get("first_name", "") or alert.get("description", ""),
)

print("\n[4] Dead man's switch")
try:
    resp = requests.get(os.environ["HEALTHCHECK_URL"], timeout=10)
    check("healthchecks.io ping", resp.status_code == 200, f"HTTP {resp.status_code}")
except Exception as e:
    check("healthchecks.io ping", False, str(e))

print("\n[5] Channel id mapping (the broken-link fix)")
entity = Channel(id=1464070967, title="t", photo=None, date=None)
marked = utils.get_peer_id(entity)
check("get_peer_id returns marked id", marked == -1001464070967, str(marked))
check("map key matches event.chat_id format", str(marked).startswith("-100"))

print("\n[6] systemd (server only)")
if shutil.which("systemctl"):
    logs = subprocess.run(
        ["journalctl", "-u", "news-tracker.service", "-n", "1", "--no-pager"],
        capture_output=True,
        text=True,
    )
    check(
        "can read journal for alerts",
        logs.returncode == 0,
        "" if logs.returncode == 0 else "add user to 'adm' group",
    )
else:
    print("  SKIP  not on the server")

print(f"\n{'ALL CHECKS PASSED' if not failures else 'FAILED: ' + ', '.join(failures)}\n")
sys.exit(1 if failures else 0)
