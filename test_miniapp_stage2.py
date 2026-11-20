import asyncio
import json
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


def log(msg: str) -> None:
    print(msg, flush=True)


def parse_tg_web_app_data(web_url: str) -> tuple[str, dict]:
    parsed = urlparse(web_url)
    params = parse_qs(parsed.fragment or "")
    raw_init_data = unquote(params.get("tgWebAppData", [""])[0])
    unsafe: dict = {"start_param": TEST_NFT_URL.split("/")[-1]}
    if raw_init_data:
        for k, vals in parse_qs(raw_init_data, keep_blank_values=True).items():
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
            log("ERROR: Session is not authorized")
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
        log("ERROR: Missing TG_API_ID, TG_API_HASH, or TG_SESSION_STRING")
        return

    web_url = await get_webview_url()
    if not web_url:
        log("ERROR: Failed to obtain WebView URL")
        return

    raw_init_data, init_data_unsafe = parse_tg_web_app_data(web_url)
    log("Obtained WebView URL successfully. Launching headless browser...")

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

        await page.add_init_script(
            f"""() => {{
                const rawInitData = {raw_init_data!r};
                const unsafeData = {json.dumps(init_data_unsafe)};
                const noop = () => {{}};
                window.__posted = [];

                const sendBack = (eventType, eventData) => {{
                    const raw = JSON.stringify({{ eventType, eventData }});
                    window.dispatchEvent(new MessageEvent('message', {{ data: raw }}));
                    if (window.TelegramGameProxy_receiveEvent) {{
                        window.TelegramGameProxy_receiveEvent(eventType, eventData);
                    }}
                    if (window.Telegram?.WebView?.receiveEvent) {{
                        window.Telegram.WebView.receiveEvent(eventType, eventData);
                    }}
                }};

                const postFn = (eventType, data) => {{
                    window.__posted.push(String(eventType));
                    let parsed = {{}};
                    try {{ parsed = typeof data === 'string' ? JSON.parse(data) : (data || {{}}); }} catch (_) {{}}
                    const reqId = parsed.req_id;
                    setTimeout(() => {{
                        if (reqId !== undefined) sendBack('custom_method_invoked', {{ req_id: reqId, result: {{}} }});
                        sendBack('finalized', {{ req_id: reqId }});
                        if (eventType === 'web_app_request_theme') {{
                            sendBack('theme_changed', {{ theme_params: {{ bg_color: '#18222d', text_color: '#ffffff' }} }});
                        }} else if (eventType === 'web_app_request_viewport') {{
                            sendBack('viewport_changed', {{ height: 900, width: 1280, is_state_stable: true, is_expanded: true }});
                        }} else if (eventType === 'web_app_request_safe_area') {{
                            sendBack('safe_area_changed', {{ top: 0, bottom: 0, left: 0, right: 0 }});
                        }} else if (eventType === 'web_app_request_content_safe_area') {{
                            sendBack('content_safe_area_changed', {{ top: 0, bottom: 0, left: 0, right: 0 }});
                        }}
                    }}, 0);
                }};

                window.TelegramWebviewProxy = {{ postEvent: postFn, unwrap: () => window }};
                window.TelegramGameProxy = {{ receiveEvent: noop, unwrap: () => window }};
                window.Telegram = window.Telegram || {{}};
                window.Telegram.WebView = {{ postEvent: postFn, receiveEvent: noop, unwrap: () => window }};
                window.Telegram.WebApp = {{
                    initData: rawInitData,
                    initDataUnsafe: unsafeData,
                    version: "8.0",
                    platform: "android",
                    colorScheme: "dark",
                    themeParams: {{ bg_color: "#18222d", text_color: "#ffffff" }},
                    isExpanded: true,
                    viewportHeight: 900,
                    viewportStableHeight: 900,
                    ready: noop,
                    expand: noop,
                    close: noop,
                    onEvent: noop,
                    offEvent: noop,
                    postEvent: postFn,
                    unwrap: () => window,
                    BackButton: {{ isVisible: false, show: noop, hide: noop, onClick: noop, offClick: noop }},
                    MainButton: {{ text: "", isVisible: false, isActive: true, show: noop, hide: noop, onClick: noop, offClick: noop, setText: noop }},
                    HapticFeedback: {{ impactOccurred: noop, notificationOccurred: noop, selectionChanged: noop }}
                }};
            }}"""
        )

        try:
            await page.goto(web_url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(6000)

            info = await page.evaluate(
                """async () => {
                    const r = await fetch('https://cdn.tgmrkt.io/js/app-shared-BZgR15pj.js');
                    const txt = await r.text();
                    const uIdx = txt.indexOf('"unwrap"');
                    const unwrapCtx = uIdx >= 0 ? txt.slice(Math.max(0, uIdx - 180), uIdx + 220) : 'no unwrap';
                    const clone = document.body ? document.body.cloneNode(true) : null;
                    if (clone) clone.querySelectorAll('style, script').forEach(el => el.remove());
                    const text = (clone && (clone.innerText || clone.textContent) || '').replace(/\\s+/g, ' ').trim();
                    return {
                        posted: (window.__posted || []).slice(0, 15),
                        textPreview: text.slice(0, 180),
                        elements: document.querySelectorAll('*').length,
                        unwrapCtx
                    };
                }"""
            )
            await page.screenshot(path=OUT_SCREENSHOT, full_page=False)
            log(f"Page errors: {page_errors[:3]}")
            log(f"Posted: {info['posted']} | Elements: {info['elements']}")
            log(f"Text: {info['textPreview']!r}")
            log(f"Unwrap ctx: {info['unwrapCtx']}")
            log(f"Saved screenshot to: {OUT_SCREENSHOT}")
        except Exception as e:
            log(f"Browser navigation/screenshot failed: {type(e).__name__}: {e}")
        finally:
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
