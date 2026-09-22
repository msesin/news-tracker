import asyncio
import html
import json
import os
import requests
from telethon import TelegramClient, events, utils
from config import TELEGRAM_API_ID, TELEGRAM_API_HASH, HEALTHCHECK_URL
from channels import CHANNELS
from filters import keyword_match, llm_classify
from storage import init_db, log_decision, is_duplicate, mark_seen
from notifier import send_notification, send_alert, send_text

_HERE = os.path.dirname(os.path.abspath(__file__))
_FAILURE_MARKER = os.path.join(_HERE, ".last_failure")
_DOWNTIME = os.path.join(_HERE, ".downtime")

CHANNEL_UP_MESSAGE = "✅ Бот знову працює — моніторинг відновлено."

SESSION_FILE = "news_tracker"
PING_INTERVAL = 300

client = TelegramClient(SESSION_FILE, TELEGRAM_API_ID, TELEGRAM_API_HASH)


async def resolve_channels():
    # Keyed by the marked peer id (-100…) so lookups by event.chat_id match.
    # The display name comes from Telegram's own entity.title, not a
    # hand-typed translation in channels.py, so it always matches the
    # channel's real name and never drifts out of sync.
    resolved = {}
    for ch in CHANNELS:
        try:
            entity = await client.get_entity(ch["username"])
            peer_id = utils.get_peer_id(entity)
            name = getattr(entity, "title", None) or ch["name"]
            resolved[peer_id] = {"name": name, "username": ch["username"]}
            print(f"  OK  {name:25s}  id={peer_id}  @{ch['username']}")
        except Exception as e:
            print(f"  FAIL  {ch['name']:25s}  @{ch['username']}  — {e}")
    return resolved


async def announce_recovery_when_stable(loop: asyncio.AbstractEventLoop) -> None:
    """Clears the outage clock, and tells the channel it's back if the channel
    was told it was down.

    Waits one full healthcheck period first: a tracker that starts, posts "we're
    back", then dies again is a crash loop, and announcing each lap of it would
    spam subscribers with down/up pairs. Staying up this long is the cheapest
    available proof that the restart actually took. Leaving the file in place
    until then is also what lets alert_failure.py accumulate towards its
    15-minute threshold across restarts instead of restarting the clock.
    """
    await asyncio.sleep(PING_INTERVAL)
    try:
        with open(_DOWNTIME) as f:
            state = json.load(f)
    except (OSError, ValueError):
        return

    # Removed before the post, not after: at worst the channel misses one
    # "we're back", which beats announcing it twice.
    os.remove(_DOWNTIME)
    if not state.get("channel_notified"):
        return
    try:
        await loop.run_in_executor(None, send_text, CHANNEL_UP_MESSAGE)
        print("[RECOVERY] posted channel recovery notice")
    except Exception as e:
        print(f"[RECOVERY] failed to post channel recovery notice: {e}")


async def healthcheck_ping(loop: asyncio.AbstractEventLoop) -> None:
    """Check in to healthchecks.io — skipped while Telegram is down, so a
    silently disconnected client trips the alarm instead of looking healthy."""
    while True:
        if client.is_connected():
            try:
                await loop.run_in_executor(
                    None, lambda: requests.get(HEALTHCHECK_URL, timeout=10)
                )
            except Exception as e:
                print(f"[PING] failed to reach healthchecks.io: {e}")
        else:
            print("[PING] skipped — Telegram client disconnected")
        await asyncio.sleep(PING_INTERVAL)


async def main():
    init_db()

    await client.start()
    print("\nLogged in successfully.\n")

    # If the last run ended in a real failure (written by alert_failure.py),
    # confirm recovery now instead of waiting on healthchecks.io, which
    # won't notice a crash-and-restart faster than its detection window.
    if os.path.exists(_FAILURE_MARKER):
        with open(_FAILURE_MARKER) as f:
            cause = f.read().strip()
        os.remove(_FAILURE_MARKER)
        try:
            send_alert(f"✅ <b>Back to normal</b> — recovered from: {html.escape(cause)}")
        except Exception as e:
            print(f"[RECOVERY] failed to send recovery alert: {e}")

    print("Resolving channels...")
    channel_map = await resolve_channels()
    print(f"\nMonitoring {len(channel_map)} channel(s). Waiting for new messages...\n")

    loop = asyncio.get_event_loop()
    asyncio.create_task(healthcheck_ping(loop))
    asyncio.create_task(announce_recovery_when_stable(loop))

    @client.on(events.NewMessage(chats=list(channel_map.keys())))
    async def handler(event):
        ch = channel_map.get(event.chat_id, {})
        channel_name = ch.get("name", str(event.chat_id))
        username = ch.get("username", "")
        text = (event.raw_text or "").strip()
        preview = text[:120].replace("\n", " ")

        if not keyword_match(text):
            print(f"[SKIP] [{channel_name}] {preview}")
            return

        decision, reason = await loop.run_in_executor(
            None, llm_classify, text, channel_name
        )
        log_decision(channel_name, text, decision, reason)

        label = "YES " if decision else "NO  "
        print(f"[{label}] [{channel_name}] {reason} | {preview}")

        if not decision:
            return

        if is_duplicate(text):
            print(f"[DUP ] [{channel_name}] skipping notification — seen in last 24h")
            return

        mark_seen(text)
        try:
            await loop.run_in_executor(
                None,
                lambda: send_notification(
                    channel_name,
                    username,
                    event.id,
                    event.message.date,
                    text,
                ),
            )
            print(f"[SENT] [{channel_name}] notification delivered")
        except Exception as e:
            print(f"[ERR ] [{channel_name}] notification failed: {e}")
            try:
                await loop.run_in_executor(
                    None,
                    send_alert,
                    f"⚠️ <b>Update may be missed</b> — publish failed for "
                    f"{html.escape(channel_name)}.\n\n"
                    f'<a href="{html.escape(f"https://t.me/{username}/{event.id}")}">Read it directly →</a>\n\n'
                    f"Monitoring continues normally.",
                )
            except Exception as alert_error:
                print(f"[ERR ] alert delivery also failed: {alert_error}")

    await client.run_until_disconnected()


if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(main())
    except KeyboardInterrupt:
        pass
    finally:
        loop.close()
