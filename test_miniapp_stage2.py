import asyncio
import os
from urllib.parse import parse_qs, unquote, urlparse
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


def extract_init_data(web_url: str) -> str:
    parsed = urlparse(web_url)
    fragment = parsed.fragment or ""
    params = parse_qs(fragment)
    raw_tg_data = params.get("tgWebAppData", [""])[0]
    return unquote(raw_tg_data)


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

    init_data = extract_init_data(web_url)
    print("Obtained WebView URL successfully. Launching headless browser...")

    console_errors: list[str] = []
    page_errors: list[str] = []
    failed_requests: list[str] = []

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

        page.on(
            "console",
            lambda msg: console_errors.append(f"[{msg.type}] {msg.text}")
            if msg.type == "error"
            else None,
        )
        page.on("pageerror", lambda err: page_errors.append(str(err)))
        page.on(
            "requestfailed",
            lambda req: failed_requests.append(
                f"{req.method} {req.url} -> {req.failure or 'failed'}"
            ),
        )

        await page.add_init_script(
            f"""() => {{
                const rawInitData = {init_data!r};
                const noop = () => {{}};
                window.Telegram = window.Telegram || {{}};
                window.Telegram.WebApp = {{
                    initData: rawInitData,
                    initDataUnsafe: {{
                        start_param: {TEST_NFT_URL.split("/")[-1]!r}
                    }},
                    version: "7.10",
                    platform: "android",
                    colorScheme: "dark",
                    themeParams: {{
                        bg_color: "#18222d",
                        text_color: "#ffffff",
                        hint_color: "#b1c3d5",
                        link_color: "#6ab2f2",
                        button_color: "#5288c1",
                        button_text_color: "#ffffff",
                        secondary_bg_color: "#131c26"
                    }},
                    isExpanded: true,
                    viewportHeight: 900,
                    viewportStableHeight: 900,
                    ready: noop,
                    expand: noop,
                    close: noop,
                    enableClosingConfirmation: noop,
                    disableClosingConfirmation: noop,
                    onEvent: noop,
                    offEvent: noop,
                    sendData: noop,
                    BackButton: {{ isVisible: false, show: noop, hide: noop, onClick: noop, offClick: noop }},
                    MainButton: {{ text: "", isVisible: false, isActive: true, show: noop, hide: noop, onClick: noop, offClick: noop, setText: noop }},
                    HapticFeedback: {{ impactOccurred: noop, notificationOccurred: noop, selectionChanged: noop }}
                }};
            }}"""
        )

        try:
            await page.goto(web_url, wait_until="domcontentloaded", timeout=30000)
            try:
                await page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass

            # انتظر حتى يختفي preloader أو يظهر محتوى نصي حقيقي (بحد أقصى 12 ثانية)
            for _ in range(12):
                await page.wait_for_timeout(1000)
                ready_state = await page.evaluate(
                    """() => {
                        const preloader = document.querySelector('#preloader');
                        const preloaderHidden = !preloader || preloader.classList.contains('preloader-hidden') || getComputedStyle(preloader).display === 'none';
                        const text = (document.body && document.body.innerText || '').trim();
                        const meaningfulText = text.replace(/You need to enable JavaScript to run this app\\.?/gi, '').trim();
                        return preloaderHidden && meaningfulText.length > 20;
                    }"""
                )
                if ready_state:
                    break

            title = await page.title()
            current_url = page.url
            stats = await page.evaluate(
                """() => {
                    const text = (document.body && (document.body.innerText || document.body.textContent) || '').trim();
                    const htmlLen = document.documentElement ? document.documentElement.outerHTML.length : 0;
                    const elementsCount = document.querySelectorAll('*').length;
                    return {
                        textPreview: text.slice(0, 400),
                        textLen: text.length,
                        htmlLen,
                        elementsCount
                    };
                }"""
            )
            await page.screenshot(path=OUT_SCREENSHOT, full_page=False)
            print("Final URL:", current_url)
            print("Page title:", repr(title))
            print("Stats:", stats)
            print("Body text preview:", repr(stats["textPreview"]))
            print("Console errors:", console_errors[:10])
            print("Page errors:", page_errors[:10])
            print("Failed requests:", failed_requests[:10])
            print("Saved screenshot to:", OUT_SCREENSHOT)
        except Exception as e:
            print(f"Browser navigation/screenshot failed: {type(e).__name__}: {e}")
        finally:
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
