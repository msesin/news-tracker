import asyncio
from telethon import TelegramClient, events
from config import TELEGRAM_API_ID, TELEGRAM_API_HASH
from channels import CHANNELS

SESSION_FILE = "news_tracker"

client = TelegramClient(SESSION_FILE, TELEGRAM_API_ID, TELEGRAM_API_HASH)


async def resolve_channels():
    """Return a dict of {entity_id: channel_name} for all configured channels."""
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
    await client.start()
    print("\nLogged in successfully.\n")

    print("Resolving channels...")
    channel_map = await resolve_channels()
    print(f"\nMonitoring {len(channel_map)} channel(s). Waiting for new messages...\n")

    @client.on(events.NewMessage(chats=list(channel_map.keys())))
    async def handler(event):
        channel_name = channel_map.get(event.chat_id, str(event.chat_id))
        text = (event.raw_text or "").strip()
        preview = text[:120].replace("\n", " ")
        print(f"[{channel_name}] {preview}")

    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
