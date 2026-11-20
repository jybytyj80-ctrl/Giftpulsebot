import asyncio
import os
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.functions.messages import RequestWebViewRequest
from playwright.async_api import async_playwright

API_ID = int((os.getenv("TG_API_ID") or os.getenv("API_ID") or "0").strip())
API_HASH = (os.getenv("TG_API_HASH") or os.getenv("API_HASH") or "").strip()
SESSION_STRING = (os.getenv("TG_SESSION_STRING") or "").strip()
BOT_USERNAME = (os.getenv("MRKT_BOT_USERNAME") or "mrkt").strip().lstrip("@")
TEST_NFT_URL = os.getenv("TEST_NFT_URL", "https://t.me/nft/FlyingBroom-3349").strip()
OUT_SCREENSHOT = os.getenv("OUT_SCREENSHOT", "stage2_view.png").strip()


async def get_webview_url() -> str | None:
    client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            print("ERROR: Session is not authorized")
            return None
        bot_entity = await client.get_input_entity(BOT_USERNAME)
        res = await client(
            RequestWebViewRequest(
                peer=bot_entity,
                bot=bot_entity,
                platform="android",
                url=f"https://t.me/{BOT_USERNAME}/app",
                start_param=TEST_NFT_URL.split("/")[-1],
            )
        )
        return getattr(res, "url", None)
    finally:
        await client.disconnect()


async def main():
    if not (API_ID and API_HASH and SESSION_STRING):
        print("ERROR: Missing TG_API_ID, TG_API_HASH, or TG_SESSION_STRING")
        return

    web_url = await get_webview_url()
    if not web_url:
        print("ERROR: Failed to obtain WebView URL")
        return

    print("Obtained WebView URL successfully. Launching headless browser...")

    page_errors: list[str] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent="Mozilla/5.0 (Linux; Android 13; Mobile) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36",
        )
        page = await context.new_page()
        page.on("pageerror", lambda err: page_errors.append(str(err)))

        try:
            await page.goto(web_url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(5000)

            vn_code = await page.evaluate(
                """async () => {
                    const r = await fetch('https://cdn.tgmrkt.io/js/app-shared-BZgR15pj.js');
                    const txt = await r.text();
                    const idx = txt.indexOf('function Vn(');
                    if (idx === -1) return 'function Vn( not found';
                    return txt.slice(idx, idx + 900);
                }"""
            )
            await page.screenshot(path=OUT_SCREENSHOT, full_page=False)
            print("Page errors:", page_errors[:5])
            print("VN_CODE:", vn_code)
            print("Saved screenshot to:", OUT_SCREENSHOT)
        except Exception as e:
            print(f"Browser navigation/screenshot failed: {type(e).__name__}: {e}")
        finally:
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
