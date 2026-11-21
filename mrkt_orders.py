import os
import time
from typing import Optional, Any
from telethon import TelegramClient
from telethon.sessions import StringSession

API_ID_ENV = (os.getenv("TG_API_ID") or os.getenv("API_ID") or "").strip()
API_HASH_ENV = (os.getenv("TG_API_HASH") or os.getenv("API_HASH") or "").strip()
TG_SESSION_STRING = os.getenv("TG_SESSION_STRING", "").strip()
MRKT_BOT_USERNAME = os.getenv("MRKT_BOT_USERNAME", "mrkt").strip().lstrip("@")

# ===================== سعر الكويك سيل الحقيقي (أعلى طلب شراء من MRKT) =====================
MRKT_API_BASE = "https://api.tgmrkt.io/api/v1"
_MRKT_TOKEN = {"value": None, "ts": 0.0}
_MRKT_ORDERS_ATTEMPT = {"idx": None}


async def _mrkt_http_request(method: str, path: str, payload: dict, token: Optional[str] = None):
    headers = {"Referer": "https://cdn.tgmrkt.io/", "Content-Type": "application/json"}
    if token:
        headers["Authorization"] = token
    url = f"{MRKT_API_BASE}/{path.lstrip('/')}"
    is_get = method.upper() == "GET"
    try:
        from curl_cffi.requests import AsyncSession
        async with AsyncSession(impersonate="chrome124") as sess:
            if is_get:
                r = await sess.get(url, headers=headers, params=payload, timeout=12)
            else:
                r = await sess.post(url, headers=headers, json=payload, timeout=12)
            try:
                return r.status_code, r.json()
            except Exception:
                return r.status_code, (r.text or "")[:400]
    except ImportError:
        import aiohttp
        async with aiohttp.ClientSession() as sess:
            tmo = aiohttp.ClientTimeout(total=12)
            if is_get:
                ctx = sess.get(url, headers=headers, params=payload, timeout=tmo)
            else:
                ctx = sess.post(url, headers=headers, json=payload, timeout=tmo)
            async with ctx as r:
                try:
                    return r.status, await r.json(content_type=None)
                except Exception:
                    return r.status, (await r.text())[:400]


async def _mrkt_http_post(path: str, payload: dict, token: Optional[str] = None):
    return await _mrkt_http_request("POST", path, payload, token)


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


_ORDERS_COOLDOWN = {"ts": 0.0}
ORDERS_COOLDOWN_SECONDS = 600


def _norm_ton(v: float) -> float:
    return round(v / 1_000_000_000, 4) if v >= 1_000_000 else round(v, 4)


def _iter_dicts(o):
    if isinstance(o, dict):
        yield o
        for v in o.values():
            yield from _iter_dicts(v)
    elif isinstance(o, list):
        for it in o:
            yield from _iter_dicts(it)


def _price_of(d: dict) -> Optional[float]:
    for k, v in d.items():
        lk = str(k).lower()
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0:
            continue
        if "floor" in lk:
            continue
        if "price" in lk or lk in ("amount", "bid", "offer"):
            return _norm_ton(float(v))
    return None


def _attrs_of(d: dict) -> dict:
    out = {"model": None, "backdrop": None, "symbol": None}
    for k, v in d.items():
        lk = str(k).lower()
        if any(bad in lk for bad in ("rarity", "count", "percent", "permille")):
            continue
        for name in out:
            if name in lk and v:
                if isinstance(v, str):
                    out[name] = [v]
                elif isinstance(v, list) and all(isinstance(x, str) for x in v):
                    out[name] = v
    return out


def _applies(attrs: dict, ours: dict) -> bool:
    for name in ("model", "backdrop", "symbol"):
        wanted = attrs.get(name)
        if not wanted:
            continue
        mine = (ours.get(name) or "").strip().lower()
        if not mine or mine not in [str(x).strip().lower() for x in wanted]:
            return False
    return True


def _scope_of(attrs: dict) -> str:
    if attrs.get("model"):
        return "Model"
    if attrs.get("backdrop"):
        return "Backdrop"
    if attrs.get("symbol"):
        return "Symbol"
    return "Any gift of this collection"


def _best_order(data: Any, ours: dict):
    """أعلى طلب شراء ينطبق على هذه الهدية. يرجع (السعر, النطاق) أو (None, None). لا يخترع أي رقم."""
    best_price, best_scope = None, None
    for dct in _iter_dicts(data):
        price = _price_of(dct)
        if price is None:
            continue
        attrs = _attrs_of(dct)
        if not _applies(attrs, ours):
            continue
        if best_price is None or price > best_price:
            best_price, best_scope = price, _scope_of(attrs)
    return best_price, best_scope


async def fetch_quick_sell(collection_name: str, model: Optional[str] = None, backdrop: Optional[str] = None,
                           symbol: Optional[str] = None, force: bool = False):
    """يرجع (سعر الكويك سيل بالتون أو None, نطاق الطلب, نص تشخيصي)."""
    now = time.time()
    if not force and now - _ORDERS_COOLDOWN["ts"] < ORDERS_COOLDOWN_SECONDS:
        return None, None, "تم تخطي الطلب مؤقتاً (فشل قريب)، لتخفيف الضغط على ماركت."
    debug = []
    try:
        token = await get_mrkt_api_token()
    except Exception as e:
        return None, None, f"فشل الحصول على التوكن: {type(e).__name__}: {e}"
    if not token:
        return None, None, "لا يوجد توكن (تحقق من TG_SESSION_STRING)."

    ours = {"model": model, "backdrop": backdrop, "symbol": symbol}
    attempts = [
        ("POST", "orders/top", {"collectionName": collection_name}),
        ("POST", "orders/list", {"collectionNames": [collection_name], "count": 10}),
        ("GET", "orders/top", {"collectionName": collection_name}),
        ("GET", "orders/list", {"collectionNames": collection_name, "count": 10}),
        ("GET", "orders", {"collectionNames": collection_name, "count": 10}),
    ]
    order = list(range(len(attempts)))
    if _MRKT_ORDERS_ATTEMPT["idx"] is not None:
        order = [_MRKT_ORDERS_ATTEMPT["idx"]] + [i for i in order if i != _MRKT_ORDERS_ATTEMPT["idx"]]

    for i in order:
        method, path, payload = attempts[i]
        try:
            status, data = await _mrkt_http_request(method, path, payload, token)
            if status in (401, 403):
                token = await get_mrkt_api_token(force=True)
                status, data = await _mrkt_http_request(method, path, payload, token)
            debug.append(f"{method} {path} -> {status} | {str(data)[:400]}")
            if status == 200:
                price, scope = _best_order(data, ours)
                if price is not None:
                    _MRKT_ORDERS_ATTEMPT["idx"] = i
                    _ORDERS_COOLDOWN["ts"] = 0.0
                    return price, scope, "\n".join(debug)
        except Exception as e:
            debug.append(f"{method} {path} -> خطأ {type(e).__name__}: {e}")
    _ORDERS_COOLDOWN["ts"] = time.time()
    return None, None, "\n".join(debug)


async def fetch_top_order_ton(collection_name: str):
    """للفحص اليدوي (فحص_اوردر): يرجع (السعر, نص تشخيصي) ويتجاوز فترة الانتظار."""
    price, scope, dbg = await fetch_quick_sell(collection_name, force=True)
    return price, (f"[{scope}]\n" if scope else "") + dbg
