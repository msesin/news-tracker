import asyncio
from telethon import TelegramClient, events
from config import TELEGRAM_API_ID, TELEGRAM_API_HASH
from channels import CHANNELS
from filters import keyword_match, llm_classify
from storage import init_db, log_decision, is_duplicate, mark_seen
from notifier import send_notification

SESSION_FILE = "news_tracker"

client = TelegramClient(SESSION_FILE, TELEGRAM_API_ID, TELEGRAM_API_HASH)


async def resolve_channels():
    # Returns {entity_id: {"name": ..., "username": ...}}
    resolved = {}
    for ch in CHANNELS:
        try:
            entity = await client.get_entity(ch["username"])
            resolved[entity.id] = {"name": ch["name"], "username": ch["username"]}
            print(f"  OK  {ch['name']:25s}  id={entity.id}  @{ch['username']}")
        except Exception as e:
            print(f"  FAIL  {ch['name']:25s}  @{ch['username']}  — {e}")
    return resolved


async def main():
    init_db()

    await client.start()
    print("\nLogged in successfully.\n")

    print("Resolving channels...")
    channel_map = await resolve_channels()
    print(f"\nMonitoring {len(channel_map)} channel(s). Waiting for new messages...\n")

    loop = asyncio.get_event_loop()

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