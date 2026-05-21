import asyncio
from telethon import TelegramClient, events
from config import TELEGRAM_API_ID, TELEGRAM_API_HASH
from channels import CHANNELS
from filters import keyword_match, llm_classify
from storage import init_db, log_decision

SESSION_FILE = "news_tracker"

client = TelegramClient(SESSION_FILE, TELEGRAM_API_ID, TELEGRAM_API_HASH)


async def resolve_channels():
    resolved = {}
    for ch in CHANNELS:
        try:
            entity = await client.get_entity(ch["username"])
            resolved[entity.id] = ch["name"]
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
        channel_name = channel_map.get(event.chat_id, str(event.chat_id))
        text = (event.raw_text or "").strip()
        preview = text[:120].replace("\n", " ")

        if not keyword_match(text):
            print(f"[SKIP] [{channel_name}] {preview}")
            return

        # Run the blocking Gemini call in a thread so the event loop stays responsive
        decision, reason = await loop.run_in_executor(
            None, llm_classify, text, channel_name
        )
        log_decision(channel_name, text, decision, reason)

        label = "YES " if decision else "NO  "
        print(f"[{label}] [{channel_name}] {reason} | {preview}")

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
