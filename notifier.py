import html
import requests
from datetime import datetime
from zoneinfo import ZoneInfo
from config import (
    NEWS_BOT_TOKEN,
    NEWS_CHAT_ID,
    ALERT_BOT_TOKEN,
    ALERT_CHAT_ID,
)

_API = f"https://api.telegram.org/bot{NEWS_BOT_TOKEN}/sendMessage"
_ALERT_API = f"https://api.telegram.org/bot{ALERT_BOT_TOKEN}/sendMessage"
_KYIV = ZoneInfo("Europe/Kyiv")


def send_notification(
    channel_name: str,
    username: str,
    message_id: int,
    timestamp: datetime,
    text: str,
) -> None:
    excerpt = text[:300] + ("…" if len(text) > 300 else "")
    link = f"https://t.me/{username}/{message_id}"
    time_str = timestamp.astimezone(_KYIV).strftime("%d %b, %H:%M")

    body = (
        f"<b>{html.escape(channel_name)}</b> · {time_str}\n\n"
        f"{html.escape(excerpt)}\n\n"
        f'<a href="{html.escape(link)}">Читати повний пост →</a>'
    )

    resp = requests.post(
        _API,
        json={"chat_id": NEWS_CHAT_ID, "text": body, "parse_mode": "HTML"},
        timeout=10,
    )
    resp.raise_for_status()


def send_text(message: str) -> None:
    resp = requests.post(
        _API,
        json={"chat_id": NEWS_CHAT_ID, "text": message},
        timeout=10,
    )
    resp.raise_for_status()


def send_alert(message: str) -> None:
    resp = requests.post(
        _ALERT_API,
        json={"chat_id": ALERT_CHAT_ID, "text": message, "parse_mode": "HTML",
              "disable_web_page_preview": True},
        timeout=10,
    )
    resp.raise_for_status()
