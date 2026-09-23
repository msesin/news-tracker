import asyncio
import html
import os
import subprocess
import requests
from telethon import TelegramClient, events, utils
from config import TELEGRAM_API_ID, TELEGRAM_API_HASH, HEALTHCHECK_URL
from channels import CHANNELS
from filters import keyword_match, llm_classify
from storage import init_db, log_decision, is_duplicate, mark_seen
from notifier import send_notification, send_alert

_HERE = os.path.dirname(os.path.abspath(__file__))
_FAILURE_MARKER = os.path.join(_HERE, ".last_failure")
_BOOT_ID_FILE = os.path.join(_HERE, ".boot_id")

SESSION_FILE = "news_tracker"
PING_INTERVAL = 300

client = TelegramClient(SESSION_FILE, TELEGRAM_API_ID, TELEGRAM_API_HASH)


def _current_boot_id() -> str | None:
    """The kernel's own ID for this boot session — changes on every reboot,
    including a crash of the OS itself, unlike a mere process restart under
    the same kernel. Not available outside Linux (e.g. local development on
    macOS), so the reboot-detection below quietly does nothing there instead
    of failing."""
    try:
        with open("/proc/sys/kernel/random/boot_id") as f:
            return f.read().strip()
    except OSError:
        return None


def _previous_boot_tail(lines: int = 20) -> str:
    """Best-effort tail of the log from the boot *before* this one — whatever
    the kernel/journald managed to write right up to the crash or shutdown.
    Empty if persistent journal storage isn't enabled on this box (the
    default on some minimal server images — see README), which the caller
    must treat as "no forensic detail available", not as an error."""
    try:
        result = subprocess.run(
            ["journalctl", "-b", "-1", "-n", str(lines), "--no-pager"],
            capture_output=True, text=True, timeout=10,
        )
        return result.stdout.strip()
    except Exception as e:
        return f"(не вдалося прочитати журнал попереднього завантаження: {e})"


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

    # Two independent, and independently optional, signals of "something
    # happened while we were away" — combined into one DM so an outage that
    # was both an app crash and a reboot doesn't produce two confusing
    # messages:
    #  - .last_failure: alert_failure.py saw the process itself die (written
    #    via ExecStopPost=, which only runs if the OS stayed up long enough
    #    to run it) — confirms recovery sooner than healthchecks.io's ~15 min
    #    detection window would.
    #  - boot id change: the OS itself restarted since our last successful
    #    start — the one case alert_failure.py structurally can't report,
    #    since a dead OS can't run ExecStopPost= to explain its own death.
    #    This is the detail a total-server-outage recovery previously had
    #    none of.
    parts = []

    if os.path.exists(_FAILURE_MARKER):
        with open(_FAILURE_MARKER) as f:
            cause = f.read().strip()
        os.remove(_FAILURE_MARKER)
        parts.append(f"<b>Процес:</b> {html.escape(cause)}")

    current_boot = _current_boot_id()
    if current_boot is not None:
        previous_boot = None
        if os.path.exists(_BOOT_ID_FILE):
            with open(_BOOT_ID_FILE) as f:
                previous_boot = f.read().strip()
        with open(_BOOT_ID_FILE, "w") as f:
            f.write(current_boot)

        # No previous_boot means this is the first run since this feature was
        # deployed — nothing to compare against, so say nothing rather than
        # imply a reboot that we have no actual evidence of.
        if previous_boot and previous_boot != current_boot:
            tail = _previous_boot_tail()
            detail = (f"<pre>{html.escape(tail[-1000:])}</pre>" if tail
                      else "(журнал попереднього завантаження порожній або недоступний — "
                           "можливо, на сервері не увімкнено persistent journal storage)")
            parts.append(f"<b>Сервер перезавантажився.</b> Останні рядки журналу перед цим:\n{detail}")

    if parts:
        try:
            send_alert("✅ <b>Back to normal</b>\n\n" + "\n\n".join(parts))
        except Exception as e:
            print(f"[RECOVERY] failed to send recovery alert: {e}")

    print("Resolving channels...")
    channel_map = await resolve_channels()
    print(f"\nMonitoring {len(channel_map)} channel(s). Waiting for new messages...\n")

    loop = asyncio.get_event_loop()
    asyncio.create_task(healthcheck_ping(loop))

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
