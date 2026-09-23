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

# strftime's "%b" depends on the server's locale, which on a fresh Ubuntu box
# is usually "C" — that renders as "Sep", not a Ukrainian month, in every
# single post header. Spelled out here instead of relying on a uk_UA locale
# being installed on the server. Genitive case, as Ukrainian dates read
# ("22 вересня", not "22 вересень").
_MONTHS_GENITIVE = {
    1: "січня", 2: "лютого", 3: "березня", 4: "квітня",
    5: "травня", 6: "червня", 7: "липня", 8: "серпня",
    9: "вересня", 10: "жовтня", 11: "листопада", 12: "грудня",
}


def _format_kyiv(timestamp: datetime) -> str:
    local = timestamp.astimezone(_KYIV)
    return f"{local.day} {_MONTHS_GENITIVE[local.month]}, {local.strftime('%H:%M')}"


def send_notification(
    channel_name: str,
    username: str,
    message_id: int,
    timestamp: datetime,
    text: str,
) -> None:
    excerpt = text[:300] + ("…" if len(text) > 300 else "")
    link = f"https://t.me/{username}/{message_id}"
    time_str = _format_kyiv(timestamp)

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
