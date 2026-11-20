import asyncio
import json
import os
import re
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


def parse_tg_web_app_data(web_url: str) -> tuple[str, dict]:
    parsed = urlparse(web_url)
    params = parse_qs(parsed.fragment or "")
    raw_init_data = unquote(params.get("tgWebAppData", [""])[0])
    unsafe: dict = {"start_param": TEST_NFT_URL.split("/")[-1]}

    if raw_init_data:
        inner = parse_qs(raw_init_data, keep_blank_values=True)
        for k, vals in inner.items():
            if not vals:
                continue
            v = vals[0]
            if k == "user":
                try:
                    unsafe["user"] = json.loads(v)
                except Exception:
                    unsafe["user"] = v
            else:
                unsafe[k] = v

    return raw_init_data, unsafe


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

    raw_init_data, init_data_unsafe = parse_tg_web_app_data(web_url)
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

        def on_pageerror(err):
            msg = f"{getattr(err, 'name', '')}: {getattr(err, 'message', str(err))}"
            stack = getattr(err, "stack", "") or ""
            if stack:
                msg += f" | {stack[:300]}"
            page_errors.append(msg)

        page.on("pageerror", on_pageerror)

        await page.add_init_script(
            f"""() => {{
                const rawInitData = {raw_init_data!r};
                const unsafeData = {json.dumps(init_data_unsafe)};
                const noop = () => {{}};
                const listeners = {{}};

                window.Telegram = window.Telegram || {{}};
                window.Telegram.WebApp = {{
                    initData: rawInitData,
                    initDataUnsafe: unsafeData,
                    version: "9.0",
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
                    onEvent: (ev, cb) => {{
                        listeners[ev] = listeners[ev] || [];
                        listeners[ev].push(cb);
                    }},
                    offEvent: (ev, cb) => {{
                        if (!listeners[ev]) return;
                        listeners[ev] = listeners[ev].filter(x => x !== cb);
                    }},
                    postEvent: (ev, data) => {{
                        if (ev === 'web_app_request_theme') {{
                            (listeners['theme_changed'] || []).forEach(cb => cb({{ theme_params: window.Telegram.WebApp.themeParams }}));
                        }} else if (ev === 'web_app_request_viewport') {{
                            (listeners['viewport_changed'] || []).forEach(cb => cb({{ height: 900, width: 1280, is_state_stable: true, is_expanded: true }}));
                        }}
                    }},
                    BackButton: {{ isVisible: false, show: noop, hide: noop, onClick: noop, offClick: noop }},
                    MainButton: {{ text: "", isVisible: false, isActive: true, show: noop, hide: noop, onClick: noop, offClick: noop, setText: noop }},
                    HapticFeedback: {{ impactOccurred: noop, notificationOccurred: noop, selectionChanged: noop }}
                }};
            }}"""
        )

        try:
            await page.goto(web_url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(7000)

            snippet_info = await page.evaluate(
                """async () => {
                    try {
                        const r = await fetch('https://cdn.tgmrkt.io/js/app-shared-BZgR15pj.js');
                        const txt = await r.text();
                        const idx = txt.indexOf('UnknownErrError');
                        return idx >= 0 ? txt.slice(Math.max(0, idx - 120), idx + 180) : 'Not found in shared';
                    } catch (e) {
                        return 'Fetch err: ' + e.message;
                    }
                }"""
            )
            await page.screenshot(path=OUT_SCREENSHOT, full_page=False)
            print("Page errors:", page_errors[:5])
            print("UnknownErrError context:", repr(snippet_info))
            print("Saved screenshot to:", OUT_SCREENSHOT)
        except Exception as e:
            print(f"Browser navigation/screenshot failed: {type(e).__name__}: {e}")
        finally:
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
