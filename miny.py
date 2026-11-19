import asyncio
import base64
from datetime import datetime
import gzip
import importlib
import inspect
import io
import os
import pkgutil
import re
import sqlite3
import sys
import time
from typing import Optional
from aiohttp import web
from aiogram import Bot, Dispatcher, types
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup
from groq import Groq
from telethon import TelegramClient
from telethon.sessions import StringSession
import amrkt
from amrkt import MarketClient

try:
    from PIL import Image, ImageDraw, ImageFont
    PIL_AVAILABLE = True
except Exception:
    PIL_AVAILABLE = False

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_MODEL = os.getenv("GROQ_MODEL")
ADMIN_ID_ENV = os.getenv("ADMIN_ID", "").strip()
ADMIN_ID = int(ADMIN_ID_ENV) if ADMIN_ID_ENV.isdigit() else None

API_ID_ENV = (os.getenv("TG_API_ID") or os.getenv("API_ID") or "").strip()
API_HASH_ENV = (os.getenv("TG_API_HASH") or os.getenv("API_HASH") or "").strip()
TG_SESSION_STRING = os.getenv("TG_SESSION_STRING", "").strip()
MRKT_BOT_USERNAME = os.getenv("MRKT_BOT_USERNAME", "mrkt").strip().lstrip("@")
DB_PATH = os.getenv("DB_PATH", "gifts_market.db").strip()
API_PORT = int(os.getenv("PORT", "8080"))

MRKT_SESSION_DIR = os.getenv("MRKT_SESSION_DIR", ".").strip()
MRKT_SESSION_B64 = os.getenv("MRKT_SESSION_B64", "").strip()
SYNC_EVERY = int(os.getenv("SYNC_EVERY_SECONDS", "300"))
FETCH_GIFT_NUMBERS = os.getenv("FETCH_GIFT_NUMBERS", "1").strip().lower() in ("1", "true", "yes")
TON_USD_RATE = float(os.getenv("TON_USD_RATE", "1.027"))

DEVELOPER_USERNAME = "vvopnm8"
DEVELOPER_NAME = "عباس"
DEFAULT_MARKET_URL = "https://t.me/mrkt/app"

if not TELEGRAM_BOT_TOKEN or not GROQ_API_KEY:
    raise ValueError("Missing TELEGRAM_BOT_TOKEN or GROQ_API_KEY in Railway Variables")


def get_db_conn(row_factory: bool = False) -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.execute("PRAGMA busy_timeout=30000")
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except Exception:
        pass
    if row_factory:
        conn.row_factory = sqlite3.Row
    return conn


def patch_amrkt_gift_collection_model():
    try:
        if hasattr(amrkt, "__path__"):
            for _, mod_name, _ in pkgutil.walk_packages(amrkt.__path__, amrkt.__name__ + "."):
                try:
                    importlib.import_module(mod_name)
                except Exception:
                    pass
    except Exception:
        pass

    patched_count = 0
    for mod_name, mod in list(sys.modules.items()):
        if not mod_name.startswith("amrkt") or mod is None:
            continue
        for attr in dir(mod):
            obj = getattr(mod, attr, None)
            if isinstance(obj, type) and hasattr(obj, "model_fields"):
                fields = getattr(obj, "model_fields", {})
                if "floorPriceNanoTons" in fields or "floor_price_nano_tons" in fields:
                    for fname in ("floorPriceNanoTons", "floor_price_nano_tons"):
                        if fname in fields:
                            finfo = fields[fname]
                            finfo.annotation = Optional[int]
                            finfo.default = 0
                    if hasattr(obj, "model_rebuild"):
                        try:
                            obj.model_rebuild(force=True)
                        except Exception:
                            pass
                    patched_count += 1
    print(f"Patched {patched_count} amrkt models.")


patch_amrkt_gift_collection_model()

client = Groq(api_key=GROQ_API_KEY)
bot = Bot(token=TELEGRAM_BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

_CACHED_MODEL: Optional[str] = None
_SYNC_LOCK = asyncio.Lock()
_GIFTS_CACHE = {"rows": [], "candidates": [], "updated_at": 0.0}
_SYNC_STATUS = {
    "last_sync_at": None,
    "last_synced_count": 0,
    "last_numbers_count": 0,
    "last_error": None,
}

AR_TO_SLUG = {
    "آيس كريم مخروطي": "IceCreamCone",
    "نودلز": "InstantRamen",
    "آيس كريم شوكولاتة": "ChocolateIceCream",
    "شعلة": "Torch",
    "تمثال الحرية": "LibertyFigure",
    "كلب الهيب هوب": "HipHopDog",
    "فانوس": "GenieLamp",
    "مصاصة ملونة": "LolPop",
    "عوامة فلامنجو": "FlamingoFloat",
    "حقيبة تيليجرام": "TelegramBag",
    "قناع": "Mask",
    "مكنسة طائرة": "FlyingBroom",
}

INITIAL_GIFTS = [
    ("آيس كريم مخروطي", "IceCreamCone", 1.2, 300, 1.2, 1.1, None, None, None, None, None),
    ("نودلز", "InstantRamen", 1.8, 438, 1.8, 1.7, None, None, None, None, None),
    ("آيس كريم شوكولاتة", "ChocolateIceCream", 2.0, 449, 2.0, 1.9, None, None, None, None, None),
    ("شعلة", "Torch", 2.5, 460, 2.5, 2.4, None, None, None, None, None),
    ("Liberty Figure", "LibertyFigure", 13.24, 480, 13.24, None, None, None, None, None, None),
    ("كلب الهيب هوب", "HipHopDog", 4.0, 480, 4.0, 3.8, None, None, None, None, None),
    ("فانوس", "GenieLamp", 4.5, 480, 4.5, 4.3, None, None, None, None, None),
    ("مصاصة ملونة", "LolPop", 5.0, 480, 5.0, 4.8, None, None, None, None, None),
    ("عوامة فلامنجو", "FlamingoFloat", 6.5, 480, 6.5, 6.2, None, None, None, None, None),
    ("حقيبة تيليجرام", "TelegramBag", 8.0, 480, 8.0, 7.6, None, None, None, None, None),
    ("قناع", "Mask", 13.8, 843, 13.81, 13.30, None, None, None, None, None),
]

EXPERT_SYSTEM_PROMPT = """
You are "Giftpulse", a warm, lively, and sharp advisor on Telegram Gifts, Telegram Stars, collectible attributes (Model, Backdrop, Symbol), and trading in the MRKT marketplace.

STRICT RULES:
1. Keep replies VERY SHORT (1 or 2 concise lines MAXIMUM, never more). Speak with warmth, natural energy, and human touch—never dry or robotic—while keeping every word tight and free of fluff. When presenting gift results, start with: هذا أفضل النتائج التي وجدتها على <a href="https://t.me/mrkt/app">ماركت</a>: followed by items formatted as Gift Name #Number — Price TON (or Gift Name — Price TON if no number is given), including exact price/floor/quick/model/number details from the database entries when relevant.
2. NEVER invent NFT gift numbers (#...) if a gift number is not explicitly given in the database entry. Only mention a number if it is explicitly shown in the database entry.
3. NEVER say "لا يمكنني عرض روابط" or "لا أستطيع إرسال روابط". Gift names in replies are automatically turned into clickable links by the bot, and there is a "فتح سوق MRKT" button below.
4. Never use ** or markdown asterisks. Plain text only (except the exact <a href="https://t.me/mrkt/app">ماركت</a> link when presenting results).
5. Prices in MRKT are in TON (not thousands of Stars). Floor = floor price; Quick = instant sale price (slightly below floor).
6. Answer ONLY about Telegram Gifts, Telegram Stars, collectibles, and MRKT. Outside scope: reply in one short line:
   مختص بالهدايا ومايدور حولها فقط
"""

user_conversations = {}


def init_db():
    conn = get_db_conn(row_factory=False)
    try:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS gifts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT UNIQUE NOT NULL,
                slug TEXT,
                ton REAL DEFAULT 0.0,
                stars INTEGER DEFAULT 0,
                floor_price REAL,
                quick_price REAL,
                gift_number INTEGER,
                model TEXT,
                backdrop TEXT,
                symbol TEXT,
                custom_url TEXT
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS mrkt_feed (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_type TEXT,
                content TEXT,
                url TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("SELECT COUNT(*) FROM gifts")
        if cur.fetchone()[0] == 0:
            cur.executemany("""
                INSERT INTO gifts (
                    title, slug, ton, stars, floor_price, quick_price,
                    gift_number, model, backdrop, symbol, custom_url
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, INITIAL_GIFTS)
        conn.commit()
    finally:
        conn.close()
    refresh_gifts_cache(force=True)


def clean_text(text: str) -> str:
    return text.replace("**", "").strip()


def normalize_slug(name: str) -> str:
    words = re.findall(r"[A-Za-z0-9]+", name)
    return "".join(w.capitalize() for w in words) if words else ""


def prettify_slug_title(slug_or_title: str) -> str:
    s = (slug_or_title or "").strip()
    if not s:
        return "Gift"
    if re.fullmatch(r"[A-Za-z0-9]+", s):
        spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", s)
        return spaced.strip()
    return s


def resolve_slug(title: str, explicit_slug=None) -> str:
    if explicit_slug and explicit_slug.strip():
        cleaned = normalize_slug(explicit_slug)
        if cleaned:
            return cleaned
    if title in AR_TO_SLUG:
        return AR_TO_SLUG[title]
    return normalize_slug(title)


def build_auto_gift_url(title: str, gift_number=None, explicit_url=None, slug=None) -> str:
    if explicit_url and explicit_url.strip():
        clean_url = explicit_url.strip()
        if re.search(r"https?://t\.me/nft/[A-Za-z0-9_]+-\d+", clean_url, re.IGNORECASE):
            return clean_url
    if gift_number is not None:
        num_str = str(gift_number).strip().lstrip("#")
        if num_str.isdigit() and int(num_str) > 0:
            resolved = resolve_slug(title, slug)
            if resolved:
                return f"https://t.me/nft/{resolved}-{num_str}"
    return DEFAULT_MARKET_URL


def build_nft_link(title: str, gift_number=None, explicit_url=None, slug=None) -> str:
    return build_auto_gift_url(title=title, gift_number=gift_number, explicit_url=explicit_url, slug=slug)


def _query_all_gifts_from_db():
    conn = get_db_conn(row_factory=True)
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT title, slug, ton, stars, floor_price, quick_price,
                   gift_number, model, backdrop, symbol, custom_url
            FROM gifts
            ORDER BY ton DESC
        """)
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def refresh_gifts_cache(force: bool = False, max_age: float = 30.0):
    now = time.monotonic()
    if not force and _GIFTS_CACHE["rows"] and (now - _GIFTS_CACHE["updated_at"]) < max_age:
        return _GIFTS_CACHE["rows"]

    rows = _query_all_gifts_from_db()
    candidates = []
    for r in rows:
        gift_url = build_auto_gift_url(
            title=r.get("title", ""),
            gift_number=r.get("gift_number"),
            explicit_url=r.get("custom_url"),
            slug=r.get("slug"),
        )
        title = (r.get("title") or "").strip()
        slug = (r.get("slug") or "").strip()
        if len(title) >= 2:
            candidates.append((title, gift_url))
        if len(slug) >= 2 and slug.lower() != title.lower():
            candidates.append((slug, gift_url))

    candidates.sort(key=lambda x: len(x[0]), reverse=True)
    _GIFTS_CACHE["rows"] = rows
    _GIFTS_CACHE["candidates"] = candidates
    _GIFTS_CACHE["updated_at"] = now
    return rows


def get_all_gifts_rows():
    return refresh_gifts_cache(force=False)


def get_gifts_within_budget(max_ton: float, limit: int = 5):
    rows = get_all_gifts_rows()
    return [r for r in rows if r.get("ton") is not None and float(r["ton"]) <= max_ton][:limit]


def _format_gift_entry(r: dict) -> str:
    ton_val = r.get("ton")
    ton_str = f"{float(ton_val):g} TON" if ton_val is not None else "غير محدد"
    name_with_num = (
        f"{r['title']} #{r['gift_number']}"
        if r.get("gift_number") is not None and int(r["gift_number"]) > 0
        else r["title"]
    )
    s = f"{name_with_num} — {ton_str}"
    if r.get("floor_price") is not None:
        s += f" | فلور {r['floor_price']} TON"
    if r.get("quick_price") is not None:
        s += f" | كويك {r['quick_price']} TON"
    if r.get("model"):
        s += f" | موديل {r['model']}"
    if r.get("backdrop"):
        s += f" | خلفية {r['backdrop']}"
    if r.get("symbol"):
        s += f" | رمز {r['symbol']}"
    return s


def get_relevant_gifts_summary(user_text: str) -> str:
    rows = get_all_gifts_rows()
    if not rows:
        return "لا توجد هدايا مسجلة."

    q_low = user_text.lower()
    matched = []
    for r in rows:
        title = (r.get("title") or "").strip()
        slug = (r.get("slug") or "").strip()
        if (title and title.lower() in q_low) or (slug and len(slug) >= 2 and slug.lower() in q_low):
            matched.append(r)

    budget_match = re.search(r"(\d+(?:\.\d+)?)\s*(?:تون|ton)", q_low)
    if budget_match and not matched:
        budget_val = float(budget_match.group(1))
        matched = [r for r in rows if r.get("ton") is not None and float(r["ton"]) <= budget_val][:8]

    target_rows = matched[:8] if matched else rows[:15]
    return " || ".join(_format_gift_entry(r) for r in target_rows)


def get_db_status_report() -> str:
    rows = refresh_gifts_cache(force=True)
    total = len(rows)
    with_nums = sum(1 for r in rows if r.get("gift_number") is not None and int(r["gift_number"]) > 0)
    top_names = ", ".join(r.get("title", "") for r in rows[:5] if r.get("title"))
    lines = [
        "حالة قاعدة البيانات والمزامنة:",
        f"- إجمالي الهدايا المحفوظة: {total} (أرقام مؤكدة: {with_nums})",
        f"- آخر مزامنة: {_SYNC_STATUS['last_sync_at'] or 'لم تتم بعد'} (كولكشنز: {_SYNC_STATUS['last_synced_count']} | أرقام: {_SYNC_STATUS['last_numbers_count']})",
        f"- الفاصل الزمني: {SYNC_EVERY} ثانية",
        f"- أبرز الأسماء: {top_names or 'لا يوجد'}",
    ]
    if _SYNC_STATUS["last_error"]:
        lines.append(f"- آخر خطأ: {_SYNC_STATUS['last_error']}")
    return "\n".join(lines)


def upsert_gift_in_db(
    title,
    slug=None,
    ton_val=None,
    stars_val=None,
    floor_val=None,
    quick_val=None,
    num_val=None,
    model_val=None,
    backdrop_val=None,
    symbol_val=None,
    url_val=None,
):
    conn = get_db_conn(row_factory=True)
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM gifts WHERE LOWER(title) = LOWER(?)", (title,))
        existing = cur.fetchone()

        resolved_slug = resolve_slug(title, slug or (existing["slug"] if existing else None))
        effective_num = num_val if num_val is not None else (existing["gift_number"] if existing else None)
        effective_url = url_val if url_val is not None else (existing["custom_url"] if existing else None)
        auto_url = build_auto_gift_url(title=title, gift_number=effective_num, explicit_url=effective_url, slug=resolved_slug)

        if existing:
            cur.execute("""
                UPDATE gifts
                SET slug = ?, ton = ?, stars = ?, floor_price = ?, quick_price = ?,
                    gift_number = ?, model = ?, backdrop = ?, symbol = ?, custom_url = ?
                WHERE id = ?
            """, (
                resolved_slug,
                ton_val if ton_val is not None else existing["ton"],
                stars_val if stars_val is not None else existing["stars"],
                floor_val if floor_val is not None else existing["floor_price"],
                quick_val if quick_val is not None else existing["quick_price"],
                effective_num,
                model_val if model_val is not None else existing["model"],
                backdrop_val if backdrop_val is not None else existing["backdrop"],
                symbol_val if symbol_val is not None else existing["symbol"],
                auto_url,
                existing["id"],
            ))
        else:
            cur.execute("""
                INSERT INTO gifts (
                    title, slug, ton, stars, floor_price, quick_price,
                    gift_number, model, backdrop, symbol, custom_url
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                title,
                resolved_slug,
                ton_val if ton_val is not None else 0.0,
                stars_val if stars_val is not None else 0,
                floor_val,
                quick_val,
                effective_num,
                model_val,
                backdrop_val,
                symbol_val,
                auto_url,
            ))
        conn.commit()
    finally:
        conn.close()
    refresh_gifts_cache(force=True)


def extract_quick_sell_query(text: str) -> Optional[dict]:
    # Strict syntax: "كويك <gift link>" (single space separating "كويك" and the link)
    m = re.match(r"^كويك ((?:https?://)?t\.me/nft/([A-Za-z0-9_]+)-(\d+))$", text.strip(), re.IGNORECASE)
    if not m:
        return None
    raw_url, slug_raw, num_str = m.group(1), m.group(2), m.group(3)
    full_url = raw_url if raw_url.lower().startswith("http") else f"https://{raw_url}"
    return {
        "slug": normalize_slug(slug_raw) or slug_raw,
        "number": int(num_str),
        "url": full_url,
    }


def _extract_ton_price_from_obj(obj) -> Optional[float]:
    if obj is None:
        return None
    for attr in (
        "quick_sell_price_ton",
        "quickSellPriceTon",
        "quick_price_ton",
        "quickPriceTon",
        "offer_price_ton",
        "offerPriceTon",
        "price_ton",
        "priceTon",
        "floor_price_ton",
        "floorPriceTon",
        "price",
        "amount",
    ):
        val = obj.get(attr) if isinstance(obj, dict) else getattr(obj, attr, None)
        if isinstance(val, (int, float)) and val > 0:
            return round(float(val), 4)
    for nano_attr in (
        "quick_sell_price_nano_tons",
        "quickSellPriceNanoTons",
        "quick_price_nano_tons",
        "quickPriceNanoTons",
        "offer_price_nano_tons",
        "offerPriceNanoTons",
        "price_nano_tons",
        "priceNanoTons",
        "floor_price_nano_tons",
        "floorPriceNanoTons",
        "price_nano",
        "nano_tons",
    ):
        val = obj.get(nano_attr) if isinstance(obj, dict) else getattr(obj, nano_attr, None)
        if isinstance(val, (int, float)) and val > 0:
            return round(float(val) / 1e9, 4)
    return None


def _extract_quick_price_from_obj(obj) -> Optional[float]:
    if obj is None:
        return None
    for attr in ("quick_sell_price_ton", "quickSellPriceTon", "quick_price_ton", "quickPriceTon", "quick_sell_price", "quickSellPrice"):
        val = obj.get(attr) if isinstance(obj, dict) else getattr(obj, attr, None)
        if isinstance(val, (int, float)) and val > 0:
            return round(float(val), 4)
    for nano_attr in ("quick_sell_price_nano_tons", "quickSellPriceNanoTons", "quick_price_nano_tons", "quickPriceNanoTons"):
        val = obj.get(nano_attr) if isinstance(obj, dict) else getattr(obj, nano_attr, None)
        if isinstance(val, (int, float)) and val > 0:
            return round(float(val) / 1e9, 4)
    return None


def _extract_attr_val(obj, names) -> Optional[str]:
    if obj is None:
        return None
    for k in names:
        val = obj.get(k) if isinstance(obj, dict) else getattr(obj, k, None)
        if isinstance(val, str) and val.strip():
            return val.strip()
        if isinstance(val, dict):
            for sub in ("name", "title", "value"):
                if isinstance(val.get(sub), str) and val[sub].strip():
                    return val[sub].strip()
        elif val is not None and not isinstance(val, (int, float, bool, list)):
            for sub in ("name", "title", "value"):
                s_val = getattr(val, sub, None)
                if isinstance(s_val, str) and s_val.strip():
                    return s_val.strip()
    return None


async def fetch_exact_quick_sell_from_mrkt(q_info: dict) -> dict:
    target_slug = (q_info.get("slug") or "").strip()
    target_num = q_info.get("number")
    pretty_name = prettify_slug_title(target_slug)
    result = {
        "title": pretty_name,
        "slug": target_slug,
        "number": target_num,
        "url": q_info.get("url") or DEFAULT_MARKET_URL,
        "model": None,
        "backdrop": None,
        "symbol": None,
        "quick_ton": None,
        "floor_ton": None,
        "source": "none",
        "listings": [],
    }

    if not (API_ID_ENV.isdigit() and API_HASH_ENV and os.path.exists(mrkt_session_path())):
        return result

    try:
        async with MarketClient(api_id=int(API_ID_ENV), api_hash=API_HASH_ENV, session_name="mrkt", workdir=MRKT_SESSION_DIR) as mc:
            if hasattr(mc, "get_collections"):
                try:
                    cols = await asyncio.wait_for(mc.get_collections(), timeout=8.0)
                    for c in cols:
                        c_slug = normalize_slug(getattr(c, "name", "") or getattr(c, "title", ""))
                        if c_slug.lower() == target_slug.lower():
                            c_title = (getattr(c, "title", "") or getattr(c, "name", "") or "").strip()
                            if c_title:
                                result["title"] = prettify_slug_title(c_title)
                            f_ton = getattr(c, "floor_price_ton", None)
                            if f_ton is None:
                                f_nano = getattr(c, "floor_price_nano_tons", None)
                                if isinstance(f_nano, (int, float)) and f_nano > 0:
                                    f_ton = f_nano / 1e9
                            if isinstance(f_ton, (int, float)) and f_ton > 0:
                                result["floor_ton"] = round(float(f_ton), 4)
                                result["source"] = "mrkt_live"
                            q_ton = _extract_quick_price_from_obj(c)
                            if q_ton is not None:
                                result["quick_ton"] = q_ton
                                result["source"] = "mrkt_live"
                            break
                except Exception:
                    pass

            if hasattr(mc, "search_gifts"):
                try:
                    fn = mc.search_gifts
                    params = inspect.signature(fn).parameters
                    kwargs = {}
                    for key in ("name", "title", "collection", "collection_name", "slug", "query", "search"):
                        if key in params:
                            kwargs[key] = target_slug
                            break
                    for l_key in ("limit", "count", "page_size"):
                        if l_key in params:
                            kwargs[l_key] = 30
                            break
                    g_res = await asyncio.wait_for(fn(**kwargs) if kwargs else fn(target_slug), timeout=8.0)
                    items = g_res if isinstance(g_res, list) else (
                        next((g_res[k] for k in ("items", "gifts", "results", "data") if isinstance(g_res, dict) and isinstance(g_res.get(k), list)), None)
                        or next((getattr(g_res, a) for a in ("items", "gifts", "results", "data") if isinstance(getattr(g_res, a, None), list)), [])
                    )
                    offer_prices = []
                    listing_prices = []
                    target_model_name = None
                    for it in items:
                        it_name = str((it.get("name") if isinstance(it, dict) else getattr(it, "name", "")) or "")
                        num_m = re.search(r"-(\d+)$", it_name.strip())
                        item_num = int(num_m.group(1)) if num_m else None
                        it_model = _extract_attr_val(it, ("model", "model_name"))
                        it_backdrop = _extract_attr_val(it, ("backdrop", "backdrop_name"))
                        it_symbol = _extract_attr_val(it, ("symbol", "symbol_name"))
                        if target_num and item_num == target_num:
                            result["model"] = it_model or result["model"]
                            result["backdrop"] = it_backdrop or result["backdrop"]
                            result["symbol"] = it_symbol or result["symbol"]
                            target_model_name = result["model"]
                        direct_q = _extract_quick_price_from_obj(it)
                        if direct_q is not None:
                            offer_prices.append(direct_q)
                        p_ton = _extract_ton_price_from_obj(it)
                        if p_ton is not None:
                            listing_prices.append((item_num, p_ton, it_model))
                        for o_attr in ("offers", "quick_sell_offers", "bids"):
                            o_list = it.get(o_attr) if isinstance(it, dict) else getattr(it, o_attr, None)
                            if isinstance(o_list, list):
                                for off in o_list:
                                    op = _extract_ton_price_from_obj(off)
                                    if op is not None:
                                        offer_prices.append(op)

                    if listing_prices:
                        if target_model_name:
                            same_model = [x for x in listing_prices if x[2] and str(x[2]).lower() == str(target_model_name).lower()]
                            if same_model:
                                same_model.sort(key=lambda x: x[1])
                                result["floor_ton"] = same_model[0][1]
                        listing_prices.sort(key=lambda x: x[1])
                        if result["floor_ton"] is None:
                            result["floor_ton"] = listing_prices[0][1]
                        result["listings"] = [(n, pr) for n, pr, _ in listing_prices[:3]]
                        result["source"] = "mrkt_live"
                    if offer_prices:
                        result["quick_ton"] = max(offer_prices)
                        result["source"] = "mrkt_live"
                    elif result["floor_ton"] is not None and result["quick_ton"] is None:
                        result["quick_ton"] = round(result["floor_ton"] * 0.96, 4)
                        result["source"] = "mrkt_live"
                except Exception:
                    pass
    except Exception:
        pass

    return result


def _load_clean_fonts():
    font_candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
    ]
    regular_candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
    ]
    bold_path = next((p for p in font_candidates if os.path.exists(p)), None)
    reg_path = next((p for p in regular_candidates if os.path.exists(p)), None)
    if bold_path and reg_path:
        return (
            ImageFont.truetype(bold_path, 34),
            ImageFont.truetype(bold_path, 24),
            ImageFont.truetype(reg_path, 20),
            ImageFont.truetype(bold_path, 19),
        )
    d = ImageFont.load_default()
    return d, d, d, d


def _draw_gift_artwork(draw, x1, y1, x2, y2, bg_rgb, accent_rgb, f_badge):
    draw.rounded_rectangle([x1, y1, x2, y2], radius=16, fill=bg_rgb)
    cx = (x1 + x2) // 2
    cy = (y1 + y2) // 2
    # Subtle geometric emblem inside card (clean, crisp vector shapes)
    draw.ellipse([cx - 46, cy - 46, cx + 46, cy + 46], fill=accent_rgb)
    draw.polygon([(cx, cy - 30), (cx + 26, cy), (cx, cy + 30), (cx - 26, cy)], fill=(26, 28, 32))
    draw.ellipse([cx - 9, cy - 9, cx + 9, cy + 9], fill=(245, 204, 24))


def render_quick_sell_card_image(data: dict) -> Optional[bytes]:
    if not PIL_AVAILABLE:
        return None
    try:
        w, h = 1080, 900
        img = Image.new("RGB", (w, h), (20, 22, 26))
        draw = ImageDraw.Draw(img)
        f_title, f_price, f_sub, f_badge = _load_clean_fonts()

        # Left card: gift preview + attributes
        draw.rounded_rectangle([24, 24, 356, 844], radius=24, fill=(31, 34, 39), outline=(48, 52, 60), width=2)
        _draw_gift_artwork(draw, 46, 46, 334, 340, (126, 108, 92), (172, 150, 130), f_badge)
        draw.rounded_rectangle([264, 60, 320, 90], radius=8, fill=(20, 22, 26))
        draw.text((273, 66), "MRKT", fill=(245, 204, 24), font=f_badge)

        title_txt = str(data.get("title") or data.get("slug") or "Gift")
        num_txt = f"#{data['number']}" if data.get("number") else ""
        draw.text((46, 358), title_txt[:20], fill=(245, 247, 250), font=f_title)
        if num_txt:
            draw.text((46, 398), num_txt, fill=(158, 165, 176), font=f_sub)

        attr_items = [
            ("Model", data.get("model") or "—"),
            ("Backdrop", data.get("backdrop") or "—"),
            ("Symbol", data.get("symbol") or "—"),
        ]
        y_attr = 442
        for k_label, v_label in attr_items:
            draw.rounded_rectangle([46, y_attr, 334, y_attr + 88], radius=14, fill=(41, 45, 52), outline=(56, 61, 70), width=1)
            draw.text((62, y_attr + 12), k_label, fill=(148, 155, 166), font=f_badge)
            draw.text((62, y_attr + 44), str(v_label)[:22], fill=(245, 247, 250), font=f_price)
            y_attr += 102

        # Center-top yellow Quick sell card
        quick_val = data.get("quick_ton")
        quick_label = f"{quick_val:g} TON" if quick_val is not None else "غير متوفر"
        draw.rounded_rectangle([378, 24, 712, 434], radius=24, fill=(245, 204, 24))
        draw.rounded_rectangle([514, 74, 576, 136], radius=16, fill=(24, 26, 30))
        draw.polygon([(545, 88), (561, 105), (545, 122), (529, 105)], fill=(245, 204, 24))
        draw.text((442, 152), quick_label, fill=(20, 22, 26), font=f_title)
        draw.text((495, 194), "Quick Sell", fill=(55, 50, 32), font=f_sub)
        draw.rounded_rectangle([408, 242, 682, 298], radius=12, fill=(31, 34, 39))
        draw.text((494, 259), "All Orders", fill=(245, 247, 250), font=f_price)
        draw.rounded_rectangle([408, 314, 682, 376], radius=12, fill=(255, 228, 84), outline=(214, 176, 12), width=1)
        draw.text((492, 333), "Quick sell", fill=(20, 22, 26), font=f_price)

        # 3 floor/listing cards
        floor_val = data.get("floor_ton")
        listings = data.get("listings") or []
        card_boxes = [
            (734, 24, 1056, 434, (54, 154, 108), (92, 194, 146)),
            (378, 456, 712, 844, (112, 96, 166), (154, 138, 208)),
            (734, 456, 1056, 844, (112, 138, 90), (154, 182, 130)),
        ]
        for idx, (x1, y1, x2, y2, bg_c, acc_c) in enumerate(card_boxes):
            draw.rounded_rectangle([x1, y1, x2, y2], radius=22, fill=(31, 34, 39), outline=(48, 52, 60), width=2)
            _draw_gift_artwork(draw, x1 + 16, y1 + 16, x2 - 16, y2 - 108, bg_c, acc_c, f_badge)
            if idx < len(listings):
                l_num, l_price = listings[idx]
                sub_txt = f"#{l_num}" if l_num else (num_txt or "Floor")
                pr_txt = f"{l_price:g} TON"
            else:
                sub_txt = num_txt or "Floor"
                pr_txt = f"{floor_val:g} TON" if floor_val is not None else "—"
            draw.text((x1 + 20, y2 - 92), title_txt[:16], fill=(245, 247, 250), font=f_price)
            draw.text((x1 + 20, y2 - 62), sub_txt, fill=(158, 165, 176), font=f_sub)
            draw.text((x1 + 20, y2 - 34), pr_txt, fill=(72, 212, 144), font=f_price)

        draw.text((28, 862), "MRKT · Quick sell", fill=(148, 155, 166), font=f_sub)
        draw.text((925, 862), "Giftpulse", fill=(148, 155, 166), font=f_sub)

        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    except Exception:
        return None


def build_quick_sell_reply(data: dict) -> str:
    num_part = f" #{data['number']}" if data.get("number") else ""
    display_name = f"{data.get('title') or data.get('slug')}{num_part}"
    gift_link = f'<a href="{data["url"]}">{display_name}</a>'

    attrs = [a for a in (data.get("model"), data.get("backdrop"), data.get("symbol")) if a]
    attrs_line = " · ".join(attrs) if attrs else "بيانات الهدية من ماركت"

    quick_val = data.get("quick_ton")
    floor_val = data.get("floor_ton")

    if quick_val is not None:
        usd_val = round(float(quick_val) * TON_USD_RATE, 2)
        quick_line = f"⚡البيع الفوري: {quick_val:g} TON ≈ ${usd_val:g}"
    else:
        quick_line = "⚡البيع الفوري: غير متوفر حالياً على ماركت"

    if floor_val is not None:
        floor_line = f"📉الأرضية: {floor_val:g} TON"
    else:
        floor_line = "📉الأرضية: غير متوفرة حالياً"

    if quick_val is not None and floor_val is not None and floor_val > 0:
        diff_pct = round(((floor_val - quick_val) / floor_val) * 100, 1)
        relation_line = f"↕ الكويك أقل من الأرضية بـ {diff_pct:g}%" if diff_pct >= 0 else f"↕ الكويك أعلى من الأرضية بـ {abs(diff_pct):g}%"
    else:
        relation_line = "↕ نسبة الفرق: غير متاحة"

    lines = [
        f"🎁 {gift_link}",
        attrs_line,
        "",
        quick_line,
        "🎯الطلب على: الموديل",
        floor_line,
        relation_line,
        "⏱ 0.5ث · MRKT",
    ]
    return "\n".join(lines)


def mrkt_session_path() -> str:
    return os.path.join(MRKT_SESSION_DIR, "mrkt.session")


def restore_mrkt_session_from_env():
    path = mrkt_session_path()
    if not MRKT_SESSION_B64:
        return
    try:
        os.makedirs(MRKT_SESSION_DIR, exist_ok=True)
        cleaned = "".join(MRKT_SESSION_B64.split())
        if len(cleaned) % 4:
            cleaned += "=" * (4 - (len(cleaned) % 4))
        raw = base64.b64decode(cleaned)
        if raw[:2] == b"\x1f\x8b":
            raw = gzip.decompress(raw)
        with open(path, "wb") as f:
            f.write(raw)
    except Exception as e:
        print(f"MRKT session restore error: {e}")


def save_collection_price(cur, name, title, floor_ton, gift_number=None):
    clean_title = (title or name or "").strip()
    slug = normalize_slug(name or "") or normalize_slug(clean_title)
    if not slug or floor_ton is None or floor_ton <= 0:
        return
    display_title = prettify_slug_title(clean_title or slug)

    cur.execute("SELECT id, gift_number, custom_url FROM gifts WHERE LOWER(slug) = LOWER(?)", (slug,))
    row = cur.fetchone()
    if row:
        effective_num = gift_number if gift_number is not None else row[1]
        auto_url = build_auto_gift_url(title=display_title, gift_number=effective_num, explicit_url=row[2], slug=slug)
        cur.execute(
            "UPDATE gifts SET ton = ?, floor_price = ?, gift_number = ?, slug = COALESCE(slug, ?), custom_url = ? WHERE id = ?",
            (floor_ton, floor_ton, effective_num, slug, auto_url, row[0]),
        )
    else:
        auto_url = build_auto_gift_url(title=display_title, gift_number=gift_number, slug=slug)
        cur.execute(
            "INSERT OR IGNORE INTO gifts (title, slug, ton, floor_price, gift_number, custom_url) VALUES (?, ?, ?, ?, ?, ?)",
            (display_title, slug, floor_ton, floor_ton, gift_number, auto_url),
        )


def _parse_gift_slug_and_number(gift_obj) -> Optional[int]:
    if gift_obj is None:
        return None
    raw_name = gift_obj.get("name") if isinstance(gift_obj, dict) else getattr(gift_obj, "name", None)
    if isinstance(raw_name, str) and raw_name.strip():
        m = re.search(r"-(\d{1,9})$", raw_name.strip())
        if m and int(m.group(1)) > 0:
            return int(m.group(1))
    return None


async def _fetch_gift_number_via_search(mc, col) -> Optional[int]:
    if not FETCH_GIFT_NUMBERS:
        return None
    fn = getattr(mc, "search_gifts", None)
    if not callable(fn):
        return None
    col_name = (getattr(col, "name", "") or "").strip()
    col_title = (getattr(col, "title", "") or "").strip()
    col_slug = normalize_slug(col_name or col_title)
    try:
        params = inspect.signature(fn).parameters
    except Exception:
        params = {}

    seen = set()
    for q in (col_name, col_title, col_slug):
        if not q or q.lower() in seen:
            continue
        seen.add(q.lower())
        kwargs = {}
        for key in ("name", "title", "collection", "collection_name", "slug", "query", "search"):
            if key in params:
                kwargs[key] = q
                break
        for l_key in ("limit", "count", "page_size"):
            if l_key in params:
                kwargs[l_key] = 3
                break
        try:
            res = await asyncio.wait_for(fn(**kwargs) if kwargs else fn(q), timeout=6.0)
            items = res if isinstance(res, list) else (
                next((res[k] for k in ("items", "gifts", "results", "data") if isinstance(res, dict) and isinstance(res.get(k), list)), None)
                or next((getattr(res, a) for a in ("items", "gifts", "results", "data") if isinstance(getattr(res, a, None), list)), [])
            )
            for item in items:
                g_num = _parse_gift_slug_and_number(item)
                if g_num is not None:
                    return g_num
        except Exception:
            continue
    return None


async def sync_prices_from_mrkt() -> int:
    if not (API_ID_ENV.isdigit() and API_HASH_ENV):
        raise RuntimeError("TG_API_ID / TG_API_HASH غير مكتملة.")

    async with _SYNC_LOCK:
        try:
            fetched_records = []
            numbers_found = 0
            async with MarketClient(api_id=int(API_ID_ENV), api_hash=API_HASH_ENV, session_name="mrkt", workdir=MRKT_SESSION_DIR) as mc:
                cols = await asyncio.wait_for(mc.get_collections(), timeout=20.0)
                for c in cols:
                    if getattr(c, "is_hidden", False):
                        continue
                    floor_ton = getattr(c, "floor_price_ton", None)
                    if floor_ton is None:
                        floor_nano = getattr(c, "floor_price_nano_tons", None)
                        if isinstance(floor_nano, (int, float)) and floor_nano > 0:
                            floor_ton = floor_nano / 1e9
                    if floor_ton is not None and floor_ton > 0:
                        gift_num = await _fetch_gift_number_via_search(mc, c)
                        if gift_num is not None:
                            numbers_found += 1
                        fetched_records.append((getattr(c, "name", ""), getattr(c, "title", ""), floor_ton, gift_num))
                        if FETCH_GIFT_NUMBERS:
                            await asyncio.sleep(0.08)

            conn = get_db_conn(row_factory=False)
            try:
                cur = conn.cursor()
                for c_name, c_title, f_ton, g_num in fetched_records:
                    save_collection_price(cur, c_name, c_title, f_ton, gift_number=g_num)
                conn.commit()
            finally:
                conn.close()

            refresh_gifts_cache(force=True)
            confirmed_nums = sum(1 for r in _GIFTS_CACHE["rows"] if r.get("gift_number") is not None and int(r["gift_number"]) > 0)
            _SYNC_STATUS["last_sync_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            _SYNC_STATUS["last_synced_count"] = len(fetched_records)
            _SYNC_STATUS["last_numbers_count"] = max(numbers_found, confirmed_nums)
            _SYNC_STATUS["last_error"] = None
            return len(fetched_records)
        except Exception as e:
            _SYNC_STATUS["last_error"] = str(e)
            raise


async def mrkt_sync_loop():
    while True:
        try:
            n = await sync_prices_from_mrkt()
            print(f"Synced {n} MRKT collections.")
        except Exception as e:
            print(f"MRKT sync error: {e}")
        await asyncio.sleep(SYNC_EVERY)


async def sync_mrkt_via_secondary_account() -> None:
    if not (API_ID_ENV.isdigit() and API_HASH_ENV and TG_SESSION_STRING):
        return
    try:
        async with TelegramClient(StringSession(TG_SESSION_STRING), int(API_ID_ENV), API_HASH_ENV) as tg_client:
            peer = await tg_client.get_input_entity(MRKT_BOT_USERNAME)
            msgs = await tg_client.get_messages(peer, limit=15)
            collected_feed = []
            for m in msgs:
                text = (getattr(m, "message", "") or "").strip()
                for u in re.findall(r"https?://\S+", text):
                    collected_feed.append(("message_url", text[:160], u.rstrip(").,]")))
                if getattr(m, "buttons", None):
                    for row in m.buttons:
                        for btn in row:
                            btn_url = (getattr(btn, "url", "") or "").strip()
                            if btn_url:
                                collected_feed.append(("button_url", (getattr(btn, "text", "") or "").strip(), btn_url))
                for nft_match in re.finditer(r"https?://t\.me/nft/([A-Za-z0-9_]+)-(\d+)", text):
                    slug_part, num_part = nft_match.group(1), nft_match.group(2)
                    upsert_gift_in_db(title=prettify_slug_title(slug_part), slug=slug_part, num_val=int(num_part), url_val=nft_match.group(0))

            if collected_feed:
                conn = get_db_conn(row_factory=False)
                try:
                    cur = conn.cursor()
                    cur.execute("DELETE FROM mrkt_feed")
                    for stype, content, url in collected_feed:
                        cur.execute("INSERT INTO mrkt_feed (source_type, content, url) VALUES (?, ?, ?)", (stype, content, url))
                    conn.commit()
                finally:
                    conn.close()
    except Exception as e:
        print(f"Secondary account sync error: {e}")


async def secondary_sync_loop():
    while True:
        await sync_mrkt_via_secondary_account()
        await asyncio.sleep(max(SYNC_EVERY, 300))


# --- API Endpoints ---
async def api_health(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok", "service": "Giftpulse MRKT API"})


async def api_get_gifts(request: web.Request) -> web.Response:
    max_ton_param = request.query.get("max_ton")
    if max_ton_param is not None:
        try:
            rows = get_gifts_within_budget(float(max_ton_param), limit=50)
        except ValueError:
            return web.json_response({"error": "Invalid max_ton"}, status=400)
    else:
        rows = get_all_gifts_rows()

    for r in rows:
        r["link"] = build_nft_link(
            title=r.get("title", ""),
            gift_number=r.get("gift_number"),
            explicit_url=r.get("custom_url"),
            slug=r.get("slug"),
        )
    return web.json_response({"count": len(rows), "gifts": rows})


async def api_upsert_gifts(request: web.Request) -> web.Response:
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body"}, status=400)

    items = payload.get("gifts") if isinstance(payload, dict) and "gifts" in payload else [payload]
    if not isinstance(items, list) or not items:
        return web.json_response({"error": "Provide a gift object or a 'gifts' list"}, status=400)

    saved = 0
    for item in items:
        if not isinstance(item, dict) or not item.get("title"):
            continue
        upsert_gift_in_db(
            title=str(item["title"]).strip(),
            slug=item.get("slug"),
            ton_val=item.get("ton"),
            stars_val=item.get("stars"),
            floor_val=item.get("floor_price"),
            quick_val=item.get("quick_price"),
            num_val=item.get("gift_number"),
            model_val=item.get("model"),
            backdrop_val=item.get("backdrop"),
            symbol_val=item.get("symbol"),
            url_val=item.get("custom_url") or item.get("url"),
        )
        saved += 1
    return web.json_response({"status": "saved", "count": saved})


async def api_trigger_sync(request: web.Request) -> web.Response:
    try:
        n = await sync_prices_from_mrkt()
        await sync_mrkt_via_secondary_account()
        return web.json_response({"status": "synced", "collections": n, "sync_status": _SYNC_STATUS})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def start_api_server():
    app = web.Application()
    app.router.add_get("/api/health", api_health)
    app.router.add_get("/api/gifts", api_get_gifts)
    app.router.add_post("/api/gifts", api_upsert_gifts)
    app.router.add_post("/api/sync", api_trigger_sync)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", API_PORT)
    await site.start()


def get_active_model() -> str:
    global _CACHED_MODEL
    if _CACHED_MODEL:
        return _CACHED_MODEL
    if GROQ_MODEL and GROQ_MODEL.strip():
        _CACHED_MODEL = GROQ_MODEL.strip()
        return _CACHED_MODEL
    model_ids = [m.id for m in client.models.list().data]
    for preferred in ("qwen/qwen3.8-27b", "allam-2-7b", "openai/gpt-oss-120b", "openai/gpt-oss-20b"):
        if preferred in model_ids:
            _CACHED_MODEL = preferred
            return _CACHED_MODEL
    chat_candidates = [mid for mid in model_ids if not any(bad in mid.lower() for bad in ("whisper", "orpheus", "guard"))]
    if chat_candidates:
        _CACHED_MODEL = chat_candidates[0]
        return _CACHED_MODEL
    raise RuntimeError(f"No suitable chat model found in: {model_ids}")


def extract_nft_query(text: str):
    url_match = re.search(r"(https?://t\.me/nft/[^\s]+)", text, re.IGNORECASE)
    if url_match:
        return url_match.group(1)
    num_match = re.search(r"#\s*(\d{1,9})\b", text)
    if not num_match:
        return None
    gift_num = num_match.group(1)
    for ar_name, slug in AR_TO_SLUG.items():
        if ar_name in text:
            return f"https://t.me/nft/{slug}-{gift_num}"
    eng_match = re.search(r"([A-Za-z][A-Za-z\s\-]{1,40}?)\s*#?\s*\d{1,9}\b", text)
    if eng_match:
        slug = normalize_slug(eng_match.group(1))
        if slug:
            return f"https://t.me/nft/{slug}-{gift_num}"
    return None


def linkify_gifts_in_reply(ai_reply: str) -> str:
    refresh_gifts_cache(force=False)
    candidates = _GIFTS_CACHE["candidates"]
    token_counter = 0
    link_tokens = {}

    def _linkify_plain_segment(segment: str) -> str:
        nonlocal token_counter
        out = segment
        for label, gift_url in candidates:
            pattern = re.compile(r"(?<![A-Za-z0-9_])" + re.escape(label) + r"(?![A-Za-z0-9_])", re.IGNORECASE)
            match = pattern.search(out)
            while match:
                matched_text = match.group(0)
                start, end = match.span()
                token = f"__GIFT_LINK_{token_counter}__"
                token_counter += 1
                out = out[:start] + token + out[end:]
                link_tokens[token] = f'<a href="{gift_url}">{matched_text}</a>'
                match = pattern.search(out)
        return out

    parts = re.split(r"(<a\b[^>]*>.*?</a>)", ai_reply, flags=re.IGNORECASE | re.DOTALL)
    for idx, part in enumerate(parts):
        if not part or part.lower().startswith("<a "):
            continue
        parts[idx] = _linkify_plain_segment(part)

    result = "".join(parts)
    for token, anchor in link_tokens.items():
        result = result.replace(token, anchor)
    return result


@dp.message(lambda message: message.text and message.text.startswith("/start"))
async def cmd_start(message: types.Message):
    user_conversations.pop(message.from_user.id, None)
    text = (
        "أهلاً بك في Giftpulse — هدايا تيليجرام وسوق "
        f'<a href="{DEFAULT_MARKET_URL}">ماركت</a>.\n'
        f'المطور: <a href="https://t.me/{DEVELOPER_USERNAME}">{DEVELOPER_NAME}</a>'
    )
    await message.answer(text, parse_mode="HTML", disable_web_page_preview=True)


@dp.message()
async def handle_user_message(message: types.Message):
    if not message.text:
        await message.answer("أرسل نصاً عن الهدايا أو TON أو ماركت.")
        return

    user_id = message.from_user.id
    user_text = message.text.strip()

    if ADMIN_ID is not None and user_id == ADMIN_ID:
        if user_text == "حالة_القاعدة":
            await message.answer(get_db_status_report(), disable_web_page_preview=True)
            return
        if user_text == "مزامنة_الاسعار":
            if not os.path.exists(mrkt_session_path()):
                await message.answer("ملف الجلسة mrkt.session غير موجود.")
                return
            try:
                n = await sync_prices_from_mrkt()
                await message.answer(f"تمت مزامنة {n} كولكشن (أرقام: {_SYNC_STATUS['last_numbers_count']}).")
            except Exception as e:
                await message.answer(f"فشلت المزامنة: {e}")
            return

    quick_info = extract_quick_sell_query(user_text)
    if quick_info:
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="فتح سوق MRKT", url=DEFAULT_MARKET_URL)]])
        exact_data = await fetch_exact_quick_sell_from_mrkt(quick_info)
        caption_txt = build_quick_sell_reply(exact_data)
        card_bytes = render_quick_sell_card_image(exact_data)
        if card_bytes:
            await message.answer_photo(photo=BufferedInputFile(card_bytes, filename="quick_sell_card.png"), caption=caption_txt, parse_mode="HTML", reply_markup=kb)
        else:
            await message.answer(caption_txt, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)
        return

    nft_link = extract_nft_query(user_text)
    if nft_link:
        await message.answer(f'رابط الهدية: <a href="{nft_link}">فتح الهدية</a>', parse_mode="HTML", disable_web_page_preview=True)
        return

    if user_id not in user_conversations:
        user_conversations[user_id] = [{"role": "system", "content": EXPERT_SYSTEM_PROMPT}]

    db_snapshot = get_relevant_gifts_summary(user_text)
    user_conversations[user_id].append({"role": "user", "content": f"{user_text}\n\n[بيانات قاعدة البيانات: {db_snapshot}]"})
    if len(user_conversations[user_id]) > 5:
        user_conversations[user_id] = [user_conversations[user_id][0]] + user_conversations[user_id][-4:]

    try:
        model_to_use = get_active_model()
        response = await asyncio.to_thread(
            lambda: client.chat.completions.create(
                model=model_to_use,
                messages=user_conversations[user_id],
                temperature=0.25,
                max_tokens=150,
            )
        )
        ai_reply = clean_text(response.choices[0].message.content)
        user_conversations[user_id].append({"role": "assistant", "content": ai_reply})

        linked_reply = linkify_gifts_in_reply(ai_reply)
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="فتح سوق MRKT", url=DEFAULT_MARKET_URL)]])
        try:
            await message.answer(linked_reply, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)
        except Exception:
            await message.answer(re.sub(r"<[^>]+>", "", linked_reply), reply_markup=kb, disable_web_page_preview=True)
    except Exception as e:
        print(f"Groq/Bot error: {e}")
        await message.answer("حدث خطأ تقني مؤقت، حاول مجدداً.")


async def main():
    init_db()
    try:
        _ = get_active_model()
    except Exception as e:
        print(f"Model pre-cache warning: {e}")
    await start_api_server()

    restore_mrkt_session_from_env()
    if API_ID_ENV.isdigit() and API_HASH_ENV and os.path.exists(mrkt_session_path()):
        asyncio.create_task(mrkt_sync_loop())
    if API_ID_ENV.isdigit() and API_HASH_ENV and TG_SESSION_STRING:
        asyncio.create_task(secondary_sync_loop())

    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
