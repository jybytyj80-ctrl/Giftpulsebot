import asyncio
import os
import re
from telethon import TelegramClient, events

API_ID_RAW = os.getenv("TG_API_ID", "").strip()
API_HASH = os.getenv("TG_API_HASH", "").strip()
SESSION_NAME = os.getenv("TG_SESSION_NAME", "mrkt_reader")
WATCH_CHAT_RAW = os.getenv("WATCH_CHAT", "").strip()

if not API_ID_RAW.isdigit() or not API_HASH:
    raise ValueError("Missing or invalid TG_API_ID / TG_API_HASH")

API_ID = int(API_ID_RAW)
WATCH_CHAT = int(WATCH_CHAT_RAW) if WATCH_CHAT_RAW.lstrip("-").isdigit() else (WATCH_CHAT_RAW or None)

client = TelegramClient(SESSION_NAME, API_ID, API_HASH)


def extract_numbers(text: str):
    return [float(x) for x in re.findall(r"\b\d+(?:\.\d+)?\b", text)]


@client.on(events.NewMessage(chats=WATCH_CHAT))
async def on_new_message(event):
    text = (event.raw_text or "").strip()
    if not text:
        return

    # تهدئة قصيرة لتجنب المعالجة السريعة المتكررة
    await asyncio.sleep(1.5)

    nums = extract_numbers(text)
    if nums:
        print(f"[READ] numbers={nums} | sample={text[:120]}")


async def main():
    print("Starting secondary account reader (read-only)...")
    await client.start()
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
