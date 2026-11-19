import asyncio
import os
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.functions.messages import RequestWebViewRequest

API_ID = int((os.getenv("TG_API_ID") or os.getenv("API_ID") or "0").strip())
API_HASH = (os.getenv("TG_API_HASH") or os.getenv("API_HASH") or "").strip()
SESSION_STRING = (os.getenv("TG_SESSION_STRING") or "").strip()
BOT_USERNAME = (os.getenv("MRKT_BOT_USERNAME") or "mrkt").strip().lstrip("@")
TEST_NFT_URL = os.getenv("TEST_NFT_URL", "https://t.me/nft/FlyingBroom-3349").strip()


async def main():
    if not (API_ID and API_HASH and SESSION_STRING):
        print("ERROR: Missing TG_API_ID, TG_API_HASH, or TG_SESSION_STRING")
        return

    client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            print("ERROR: Session is not authorized")
            return

        bot_entity = await client.get_input_entity(BOT_USERNAME)
        print(f"Resolved bot entity: @{BOT_USERNAME}")

        try:
            res = await client(
                RequestWebViewRequest(
                    peer=bot_entity,
                    bot=bot_entity,
                    platform="android",
                    url=f"https://t.me/{BOT_USERNAME}/app",
                    start_param=TEST_NFT_URL.split("/")[-1],
                )
            )
            print("RequestWebViewRequest SUCCESS!")
            print("WebView URL:", getattr(res, "url", None))
            print("Query ID:", getattr(res, "query_id", None))
        except Exception as e:
            print(f"RequestWebViewRequest failed: {type(e).__name__}: {e}")

        msgs = await client.get_messages(bot_entity, limit=3)
        print(f"Recent messages count: {len(msgs)}")
        for m in msgs:
            txt = (getattr(m, "message", "") or "").strip().replace("\n", " ")
            print(f" - Msg id={m.id}: {txt[:120]}")
            if getattr(m, "buttons", None):
                for r_idx, row in enumerate(m.buttons):
                    for b_idx, btn in enumerate(row):
                        print(f"   Btn[{r_idx},{b_idx}] text={getattr(btn, 'text', None)!r} url={getattr(btn, 'url', None)!r}")
    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
