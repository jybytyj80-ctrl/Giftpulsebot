import os
import time
from typing import Optional
from telethon import TelegramClient
from telethon.sessions import StringSession

API_ID_ENV = (os.getenv("TG_API_ID") or os.getenv("API_ID") or "").strip()
API_HASH_ENV = (os.getenv("TG_API_HASH") or os.getenv("API_HASH") or "").strip()
TG_SESSION_STRING = os.getenv("TG_SESSION_STRING", "").strip()
MRKT_BOT_USERNAME = os.getenv("MRKT_BOT_USERNAME", "mrkt").strip().lstrip("@")

MRKT_API_BASE = "https://api.tgmrkt.io/api/v1"
_MRKT_TOKEN = {"value": None, "ts": 0.0}
_MRKT_ORDERS_ATTEMPT = {"idx": None}


async def _mrkt_http_post(path: str, payload: dict, token: Optional[str] = None):
    headers = {"Referer": "https://cdn.tgmrkt.io/", "Content-Type": "application/json"}
    if token:
        headers["Authorization"] = token
    url = f"{MRKT_API_BASE}/{path.lstrip('/')}"
    try:
        from curl_cffi.requests import AsyncSession
        async with AsyncSession(impersonate="chrome124") as sess:
            r = await sess.post(url, headers=headers, json=payload, timeout=12)
            try:
                return r.status_code, r.json()
            except Exception:
                return r.status_code, (r.text or "")[:400]
    except ImportError:
        import aiohttp
        async with aiohttp.ClientSession() as sess:
            async with sess.post(url, headers=headers, json=payload, timeout=aiohttp.ClientTimeout(total=12)) as r:
                try:
                    return r.status, await r.json(content_type=None)
                except Exception:
                    return r.status, (await r.text())[:400]


async def get_mrkt_api_token(force: bool = False) -> Optional[str]:
    now = time.time()
    if not force and _MRKT_TOKEN["value"] and now - _MRKT_TOKEN["ts"] < 6 * 3600:
        return _MRKT_TOKEN["value"]
    if not (API_ID_ENV.isdigit() and API_HASH_ENV and TG_SESSION_STRING):
        return None
    from urllib.parse import unquote
    from telethon.tl.functions.messages import RequestAppWebViewRequest
    from telethon.tl.types import InputBotAppShortName, InputUser
    async with TelegramClient(StringSession(TG_SESSION_STRING), int(API_ID_ENV), API_HASH_ENV) as tg:
        peer = await tg.get_input_entity(MRKT_BOT_USERNAME)
        bot_user = InputUser(user_id=peer.user_id, access_hash=peer.access_hash)
        web_view = await tg(RequestAppWebViewRequest(
            peer=peer,
            app=InputBotAppShortName(bot_id=bot_user, short_name="app"),
            platform="android",
        ))
    init_data = unquote(web_view.url.split("tgWebAppData=", 1)[1].split("&tgWebAppVersion", 1)[0])
    status, data = await _mrkt_http_post("auth", {"data": init_data})
    token = data.get("token") if isinstance(data, dict) else None
    if token:
        _MRKT_TOKEN["value"] = token
        _MRKT_TOKEN["ts"] = now
    return token


def _max_price_ton_from_json(obj) -> Optional[float]:
    found = []

    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if isinstance(v, (int, float)) and not isinstance(v, bool) and "price" in str(k).lower() and v > 0:
                    found.append(float(v))
                else:
                    walk(v)
        elif isinstance(o, list):
            for it in o:
                walk(it)

    walk(obj)
    if not found:
        return None
    top = max(found)
    if top >= 1_000_000:
        top = top / 1_000_000_000
    return round(top, 4)


async def fetch_top_order_ton(collection_name: str):
    """يرجع (أعلى طلب شراء بالتون أو None, نص تشخيصي). لا يخترع أي رقم."""
    debug = []
    try:
        token = await get_mrkt_api_token()
    except Exception as e:
        return None, f"فشل الحصول على التوكن: {type(e).__name__}: {e}"
    if not token:
        return None, "لا يوجد توكن (تحقق من TG_SESSION_STRING)."

    attempts = [
        ("orders/top", {"collectionName": collection_name}),
        ("orders/list", {"collectionNames": [collection_name], "count": 10}),
    ]
    order = list(range(len(attempts)))
    if _MRKT_ORDERS_ATTEMPT["idx"] is not None:
        order = [_MRKT_ORDERS_ATTEMPT["idx"]] + [i for i in order if i != _MRKT_ORDERS_ATTEMPT["idx"]]

    for i in order:
        path, payload = attempts[i]
        try:
            status, data = await _mrkt_http_post(path, payload, token)
            if status in (401, 403):
                token = await get_mrkt_api_token(force=True)
                status, data = await _mrkt_http_post(path, payload, token)
            raw = str(data)[:500]
            debug.append(f"{path} -> {status} | {raw}")
            if status == 200:
                price = _max_price_ton_from_json(data)
                if price is not None:
                    _MRKT_ORDERS_ATTEMPT["idx"] = i
                    return price, "\n".join(debug)
        except Exception as e:
            debug.append(f"{path} -> خطأ {type(e).__name__}: {e}")
    return None, "\n".join(debug)
