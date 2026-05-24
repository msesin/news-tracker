import html
import requests
from datetime import datetime
from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"


def send_notification(
    channel_name: str,
    username: str,
    message_id: int,
    timestamp: datetime,
    text: str,
) -> None:
    excerpt = text[:300] + ("…" if len(text) > 300 else "")
    link = f"https://t.me/{username}/{message_id}"
    time_str = timestamp.strftime("%Y-%m-%d %H:%M UTC")

    body = (
        f"<b>{html.escape(channel_name)}</b>\n"
        f"🕐 {time_str}\n\n"
        f"{html.escape(excerpt)}\n\n"
        f"🔗 {link}"
    )

    resp = requests.post(
        _API,
        json={"chat_id": TELEGRAM_CHAT_ID, "text": body, "parse_mode": "HTML"},
        timeout=10,
    )
    resp.raise_for_status()