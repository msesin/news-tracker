import asyncio
import html
import os
import subprocess
import requests
from telethon import TelegramClient, events, utils
from config import TELEGRAM_API_ID, TELEGRAM_API_HASH, HEALTHCHECK_URL
from channels import CHANNELS
from filters import keyword_match, llm_classify, classifier_healthy
from storage import (init_db, log_decision, is_duplicate, mark_seen,
                     park_post, pending_posts, pending_count, unpark,
                     note_park_attempt, MAX_PARK_ATTEMPTS)
from notifier import send_notification, send_alert

_HERE = os.path.dirname(os.path.abspath(__file__))
_FAILURE_MARKER = os.path.join(_HERE, ".last_failure")
_BOOT_ID_FILE = os.path.join(_HERE, ".boot_id")

SESSION_FILE = "news_tracker"
PING_INTERVAL = 300
DRAIN_INTERVAL = 60

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


async def _publish(loop, channel_name, username, event_id, text, late=False):
    """Send one YES post to the channel. Shared by the live handler and the
    drain, so a post rescued from the parking table is delivered by exactly
    the same path as one classified on arrival."""
    if is_duplicate(text):
        print(f"[DUP ] [{channel_name}] skipping notification — seen in last 24h")
        return
    mark_seen(text)
    await loop.run_in_executor(
        None, send_notification, channel_name, username, event_id, text
    )
    print(f"[SENT] [{channel_name}] notification delivered{' (late)' if late else ''}")


async def drain_pending(loop: asyncio.AbstractEventLoop) -> None:
    """Replay posts the classifier never judged, once it answers again.

    Retries inside llm_classify() cover about ninety seconds. Anything longer
    and the post lands here instead of being logged NO unreviewed. A successful
    classification here also resolves the outage the normal way, since
    llm_classify() reports its own success — so the first rescued post can be
    what triggers the recovery notices, with no post needing to arrive live."""
    while True:
        await asyncio.sleep(DRAIN_INTERVAL)
        parked = pending_posts()
        if not parked:
            continue

        # Never replay into a classifier that is still down. Watching for
        # recovery is the probe's job; a failure here while the API is out
        # would be counted against the post, and five such passes would drop
        # the post minutes into an outage it was parked to survive.
        if not classifier_healthy():
            print(f"[DRAIN] {len(parked)} parked, classifier still down — waiting")
            continue

        print(f"[DRAIN] {len(parked)} parked post(s) — retrying")
        for post_id, channel_name, text, _attempts in parked:
            decision, reason = await loop.run_in_executor(
                None, llm_classify, text, channel_name
            )

            if reason.startswith("LLM error:"):
                attempts = await loop.run_in_executor(None, note_park_attempt, post_id)
                if attempts >= MAX_PARK_ATTEMPTS:
                    # Not an outage any more - this specific post fails every
                    # time. Drop it rather than retry it on every recovery
                    # forever, and say so, since it is a post nobody judged.
                    await loop.run_in_executor(None, unpark, post_id)
                    log_decision(channel_name, text, False, f"dropped after {attempts} attempts: {reason}")
                    try:
                        await loop.run_in_executor(
                            None, send_alert,
                            f"⚠️ <b>Post could not be classified</b> after {attempts} attempts "
                            f"in {html.escape(channel_name)} — dropped without review.\n\n"
                            f"<pre>{html.escape(text[:300])}</pre>")
                    except Exception as e:
                        print(f"[DRAIN] alert failed: {e}")
                else:
                    # Healthy classifier, yet this post failed - genuinely
                    # post-specific, so the strike is earned. Stop the pass
                    # anyway in case the API went down again mid-drain; the
                    # next pass re-checks health before touching anything.
                    print(f"[DRAIN] post failed ({attempts}/{MAX_PARK_ATTEMPTS}) — leaving parked")
                    break
                continue

            await loop.run_in_executor(None, unpark, post_id)
            log_decision(channel_name, text, decision, f"(late) {reason}")
            print(f"[{'YES ' if decision else 'NO  '}] [{channel_name}] (late) {reason}")
            if decision:
                try:
                    await _publish(loop, channel_name, "", 0, text, late=True)
                except Exception as e:
                    print(f"[DRAIN] publish failed: {e}")


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
    asyncio.create_task(drain_pending(loop))

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

        # A post the classifier never judged is parked, not discarded. Logging
        # it NO here would be recording a verdict nobody reached.
        if reason.startswith("LLM error:"):
            await loop.run_in_executor(None, park_post, channel_name, text)
            print(f"[PARK] [{channel_name}] classifier unavailable — parked for retry | {preview}")
            return

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
