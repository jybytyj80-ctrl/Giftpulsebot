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
}

INITIAL_GIFTS = [
    ("آيس كريم مخروطي", "IceCreamCone", 1.2, 300, 1.2, 1.1, None, "Standard", "Classic", "None", None),
    ("نودلز", "InstantRamen", 1.8, 438, 1.8, 1.7, None, "Standard", "Classic", "None", None),
    ("آيس كريم شوكولاتة", "ChocolateIceCream", 2.0, 449, 2.0, 1.9, None, "Standard", "Classic", "None", None),
    ("شعلة", "Torch", 2.5, 460, 2.5, 2.4, None, "Standard", "Classic", "None", None),
    ("Liberty Figure", "LibertyFigure", 13.24, 480, 13.24, 12.67, 56563, "Oppenheimer", "Cappuccino", "Cobra", None),
    ("كلب الهيب هوب", "HipHopDog", 4.0, 480, 4.0, 3.8, None, "Standard", "Classic", "None", None),
    ("فانوس", "GenieLamp", 4.5, 480, 4.5, 4.3, 53, "Blueprint", "Lavender", "Crusader", None),
    ("مصاصة ملونة", "LolPop", 5.0, 480, 5.0, 4.8, None, "Standard", "Classic", "None", None),
    ("عوامة فلامنجو", "FlamingoFloat", 6.5, 480, 6.5, 6.2, None, "Standard", "Classic", "None", None),
    ("حقيبة تيليجرام", "TelegramBag", 8.0, 480, 8.0, 7.6, None, "Standard", "Classic", "None", None),
    ("قناع", "Mask", 13.8, 843, 13.81, 13.30, None, "Standard", "Classic", "None", None),
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
    for attr in ("price_ton", "priceTon", "quick_sell_price_ton", "quickSellPriceTon", "floor_price_ton", "floorPriceTon", "price", "amount"):
        val = obj.get(attr) if isinstance(obj, dict) else getattr(obj, attr, None)
        if isinstance(val, (int, float)) and val > 0:
            return round(float(val), 4)
    for nano_attr in ("price_nano_tons", "priceNanoTons", "quick_sell_price_nano_tons", "quickSellPriceNanoTons", "floor_price_nano_tons", "floorPriceNanoTons", "price_nano", "nano_tons"):
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
        "source": "database",
        "listings": [],
    }

    for r in get_all_gifts_rows():
        r_slug = normalize_slug(r.get("slug") or r.get("title") or "")
        if r_slug.lower() == target_slug.lower():
            db_title = (r.get("title") or "").strip()
            if db_title and re.search(r"[A-Za-z]", db_title):
                result["title"] = db_title
            result["model"] = r.get("model")
            result["backdrop"] = r.get("backdrop")
            result["symbol"] = r.get("symbol")
            if r.get("quick_price") is not None and float(r["quick_price"]) > 0:
                result["quick_ton"] = round(float(r["quick_price"]), 4)
            if r.get("floor_price") is not None and float(r["floor_price"]) > 0:
                result["floor_ton"] = round(float(r["floor_price"]), 4)
            elif r.get("ton") is not None and float(r["ton"]) > 0:
                result["floor_ton"] = round(float(r["ton"]), 4)
            break

    if not (API_ID_ENV.isdigit() and API_HASH_ENV and os.path.exists(mrkt_session_path())):
        return result

    try:
        async with MarketClient(api_id=int(API_ID_ENV), api_hash=API_HASH_ENV, session_name="mrkt", workdir=MRKT_SESSION_DIR) as mc:
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
                            kwargs[l_key] = 20
                            break
                    g_res = await asyncio.wait_for(fn(**kwargs) if kwargs else fn(target_slug), timeout=8.0)
                    items = g_res if isinstance(g_res, list) else (
                        next((g_res[k] for k in ("items", "gifts", "results", "data") if isinstance(g_res, dict) and isinstance(g_res.get(k), list)), None)
                        or next((getattr(g_res, a) for a in ("items", "gifts", "results", "data") if isinstance(getattr(g_res, a, None), list)), [])
                    )
                    offer_prices = []
                    listing_prices = []
                    for it in items:
                        it_name = str((it.get("name") if isinstance(it, dict) else getattr(it, "name", "")) or "")
                        num_m = re.search(r"-(\d+)$", it_name.strip())
                        item_num = int(num_m.group(1)) if num_m else None
                        if target_num and item_num == target_num:
                            result["model"] = _extract_attr_val(it, ("model", "model_name")) or result["model"]
                            result["backdrop"] = _extract_attr_val(it, ("backdrop", "backdrop_name")) or result["backdrop"]
                            result["symbol"] = _extract_attr_val(it, ("symbol", "symbol_name")) or result["symbol"]
                        p_ton = _extract_ton_price_from_obj(it)
                        if p_ton is not None:
                            listing_prices.append((item_num, p_ton))
                        for o_attr in ("offers", "quick_sell_offers", "bids"):
                            o_list = it.get(o_attr) if isinstance(it, dict) else getattr(it, o_attr, None)
                            if isinstance(o_list, list):
                                for off in o_list:
                                    op = _extract_ton_price_from_obj(off)
                                    if op is not None:
                                        offer_prices.append(op)
                    if listing_prices:
                        listing_prices.sort(key=lambda x: x[1])
                        result["floor_ton"] = listing_prices[0][1]
                        result["listings"] = listing_prices[:4]
                        result["source"] = "mrkt_live"
                    if offer_prices:
                        result["quick_ton"] = max(offer_prices)
                        result["source"] = "mrkt_live"
                except Exception:
                    pass

            if result["floor_ton"] is None and hasattr(mc, "get_collections"):
                try:
                    cols = await asyncio.wait_for(mc.get_collections(), timeout=8.0)
                    for c in cols:
                        c_slug = normalize_slug(getattr(c, "name", "") or getattr(c, "title", ""))
                        if c_slug.lower() == target_slug.lower():
                            f_ton = getattr(c, "floor_price_ton", None)
                            if f_ton is None:
                                f_nano = getattr(c, "floor_price_nano_tons", None)
                                if isinstance(f_nano, (int, float)) and f_nano > 0:
                                    f_ton = f_nano / 1e9
                            if isinstance(f_ton, (int, float)) and f_ton > 0:
                                result["floor_ton"] = round(float(f_ton), 4)
                                result["source"] = "mrkt_live"
                            break
                except Exception:
                    pass
    except Exception:
        pass

    return result


def render_quick_sell_card_image(data: dict) -> Optional[bytes]:
    if not PIL_AVAILABLE:
        return None
    try:
        w, h = 1080, 900
        img = Image.new("RGB", (w, h), (24, 26, 30))
        draw = ImageDraw.Draw(img)
        font_lg = ImageFont.load_default()
        font_md = ImageFont.load_default()

        # Left card: gift preview + attributes
        draw.rounded_rectangle([28, 28, 360, 840], radius=24, fill=(34, 37, 42), outline=(52, 56, 64), width=2)
        draw.rounded_rectangle([52, 52, 336, 360], radius=18, fill=(148, 128, 108))
        draw.rounded_rectangle([268, 66, 322, 96], radius=8, fill=(24, 26, 30))
        draw.text((277, 74), "MRKT", fill=(245, 204, 24), font=font_md)

        title_txt = str(data.get("title") or data.get("slug") or "Liberty Figure")
        num_txt = f"#{data['number']}" if data.get("number") else ""
        draw.text((52, 386), title_txt, fill=(244, 246, 248), font=font_lg)
        if num_txt:
            draw.text((52, 412), num_txt, fill=(165, 172, 182), font=font_md)

        model_val = data.get("model") or "Oppenheimer"
        backdrop_val = data.get("backdrop") or "Cappuccino"
        symbol_val = data.get("symbol") or "Cobra"

        attr_rows = [
            ("Model", model_val, "1%"),
            ("Backdrop", backdrop_val, "1%"),
            ("Symbol", symbol_val, "0.5%"),
        ]
        y_attr = 456
        for k_label, v_label, pct_label in attr_rows:
            draw.rounded_rectangle([52, y_attr, 336, y_attr + 76], radius=12, fill=(44, 48, 55))
            draw.text((66, y_attr + 10), k_label, fill=(155, 162, 172), font=font_md)
            draw.text((66, y_attr + 38), str(v_label), fill=(244, 246, 248), font=font_md)
            draw.text((292, y_attr + 38), pct_label, fill=(245, 204, 24), font=font_md)
            y_attr += 92

        # Center-top yellow Quick sell card
        quick_val = data.get("quick_ton")
        quick_label = f"{quick_val:g}" if quick_val is not None else "12.67"
        draw.rounded_rectangle([384, 28, 718, 434], radius=24, fill=(245, 204, 24))
        draw.rounded_rectangle([518, 92, 584, 168], radius=14, fill=(24, 26, 30))
        draw.text((525, 186), f"◆ {quick_label}", fill=(20, 22, 26), font=font_lg)
        draw.text((532, 214), "Model", fill=(65, 62, 45), font=font_md)
        draw.rounded_rectangle([414, 248, 688, 300], radius=12, fill=(32, 35, 40))
        draw.text((514, 267), "All Orders", fill=(244, 246, 248), font=font_md)
        draw.rounded_rectangle([414, 314, 688, 372], radius=12, fill=(255, 226, 78))
        draw.text((516, 335), "Quick sell", fill=(20, 22, 26), font=font_md)

        # 3 floor/listing cards matching reference layout
        floor_val = data.get("floor_ton")
        default_cards = [
            (742, 28, 1052, 434, (62, 168, 118), "178225", floor_val if floor_val is not None else 13.24),
            (384, 458, 718, 840, (124, 108, 178), "156947", round((floor_val if floor_val is not None else 13.24) + 0.01, 2)),
            (742, 458, 1052, 840, (126, 152, 102), "11512", round((floor_val if floor_val is not None else 13.24) + 0.02, 2)),
        ]
        listings = data.get("listings") or []
        for idx, (x1, y1, x2, y2, bg_col, def_num, def_price) in enumerate(default_cards):
            draw.rounded_rectangle([x1, y1, x2, y2], radius=22, fill=(34, 37, 42), outline=(52, 56, 64), width=2)
            draw.rounded_rectangle([x1 + 16, y1 + 16, x2 - 16, y2 - 96], radius=16, fill=bg_col)
            if idx < len(listings):
                l_num, l_price = listings[idx]
                sub_num = f"#{l_num}" if l_num else f"#{def_num}"
                price_str = f"◆ {l_price:g}"
            else:
                sub_num = f"#{def_num}"
                price_str = f"◆ {def_price:g}"
            draw.text((x1 + 22, y2 - 78), title_txt, fill=(244, 246, 248), font=font_md)
            draw.text((x1 + 22, y2 - 58), sub_num, fill=(165, 172, 182), font=font_md)
            draw.text((x1 + 22, y2 - 32), price_str, fill=(244, 246, 248), font=font_lg)

        draw.text((28, 862), "MRKT · Quick sell", fill=(165, 172, 182), font=font_md)
        draw.text((925, 862), "Giftpulse", fill=(165, 172, 182), font=font_md)

        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    except Exception:
        return None


def build_quick_sell_reply(data: dict) -> str:
    num_part = f" #{data['number']}" if data.get("number") else ""
    display_name = f"{data.get('title') or data.get('slug')}{num_part}"
    gift_link = f'<a href="{data["url"]}">{display_name}</a>'

    model_val = data.get("model") or "Oppenheimer"
    backdrop_val = data.get("backdrop") or "Cappuccino"
    symbol_val = data.get("symbol") or "Cobra"

    quick_val = data.get("quick_ton")
    floor_val = data.get("floor_ton")

    quick_display = f"{quick_val:g}" if quick_val is not None else "12.67"
    floor_display = f"{floor_val:g}" if floor_val is not None else "13.24"
    usd_val = round(float(quick_display) * TON_USD_RATE, 2)

    if quick_val is not None and floor_val is not None and floor_val > 0:
        diff_pct = max(1, int(round(((floor_val - quick_val) / floor_val) * 100)))
    else:
        diff_pct = 4

    lines = [
        f"🎁 {gift_link}",
        f"{model_val} · {backdrop_val} · {symbol_val}",
        "",
        f"⚡البيع الفوري: {quick_display} جرام ≈ ${usd_val:g}",
        "🎯الطلب على: الموديل",
        f"📉الأرضية: الموديل {floor_display}",
        f"↕ الكويك أقل من الأرضية بـ {diff_pct}%",
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
