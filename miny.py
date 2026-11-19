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
from urllib.request import Request, urlopen
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
_ARTWORK_CACHE: dict[str, Optional[Image.Image]] = {}
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
    "سيجار كلاسيكي": "VintageCigar",
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


def _extract_attr_and_rarity(obj, names):
    if obj is None:
        return None, None
    rarity_keys = ("rarity", "rarity_percent", "rarityPercent", "chance", "probability", "percent", "percentage")
    containers = [obj]
    for sub_c in ("attributes", "traits", "properties", "metadata", "gift", "item", "data"):
        cv = obj.get(sub_c) if isinstance(obj, dict) else getattr(obj, sub_c, None)
        if isinstance(cv, (dict, list)):
            containers.append(cv)
        elif cv is not None and not isinstance(cv, (str, int, float, bool)):
            containers.append(cv)

    for container in containers:
        if isinstance(container, list):
            for el in container:
                t_name = ((el.get("trait_type") or el.get("type") or el.get("name") or el.get("key") or "") if isinstance(el, dict) else (getattr(el, "trait_type", None) or getattr(el, "type", None) or getattr(el, "name", None) or "")).strip().lower()
                if any(n.lower() == t_name or n.lower() in t_name for n in names):
                    v_str = ((el.get("value") or el.get("val") or el.get("title") or "") if isinstance(el, dict) else (getattr(el, "value", None) or getattr(el, "title", None) or ""))
                    if isinstance(v_str, str) and v_str.strip():
                        rar_str = None
                        for rk in rarity_keys:
                            rv = el.get(rk) if isinstance(el, dict) else getattr(el, rk, None)
                            if isinstance(rv, (int, float)) and rv > 0:
                                rar_str = f"{rv:g}%"
                                break
                            if isinstance(rv, str) and rv.strip():
                                rar_str = rv.strip() if rv.strip().endswith("%") else f"{rv.strip()}%"
                                break
                        return v_str.strip(), rar_str
            continue

        for k in names:
            val = container.get(k) if isinstance(container, dict) else getattr(container, k, None)
            if isinstance(val, str) and val.strip():
                return val.strip(), None
            if isinstance(val, dict):
                name_part = None
                for sub in ("name", "title", "value"):
                    if isinstance(val.get(sub), str) and val[sub].strip():
                        name_part = val[sub].strip()
                        break
                rar_part = None
                for rk in rarity_keys:
                    rv = val.get(rk)
                    if isinstance(rv, (int, float)) and rv > 0:
                        rar_part = f"{rv:g}%"
                        break
                    if isinstance(rv, str) and rv.strip():
                        rar_part = rv.strip() if rv.strip().endswith("%") else f"{rv.strip()}%"
                        break
                if name_part:
                    return name_part, rar_part
            elif val is not None and not isinstance(val, (int, float, bool, list)):
                name_part = None
                for sub in ("name", "title", "value"):
                    s_val = getattr(val, sub, None)
                    if isinstance(s_val, str) and s_val.strip():
                        name_part = s_val.strip()
                        break
                rar_part = None
                for rk in rarity_keys:
                    rv = getattr(val, rk, None)
                    if isinstance(rv, (int, float)) and rv > 0:
                        rar_part = f"{rv:g}%"
                        break
                    if isinstance(rv, str) and rv.strip():
                        rar_part = rv.strip() if rv.strip().endswith("%") else f"{rv.strip()}%"
                        break
                if name_part:
                    return name_part, rar_part
    return None, None


def _extract_attr_val(obj, names) -> Optional[str]:
    name_part, _ = _extract_attr_and_rarity(obj, names)
    return name_part


def _extract_image_url_from_obj(obj) -> Optional[str]:
    if obj is None:
        return None
    for k in ("image", "image_url", "imageUrl", "photo", "photo_url", "thumbnail", "thumb", "icon", "icon_url", "preview", "preview_url", "url"):
        val = obj.get(k) if isinstance(obj, dict) else getattr(obj, k, None)
        if isinstance(val, str) and val.strip().startswith(("http://", "https://")):
            return val.strip()
        if isinstance(val, dict):
            for sub in ("url", "src", "image", "image_url"):
                if isinstance(val.get(sub), str) and val[sub].strip().startswith(("http://", "https://")):
                    return val[sub].strip()
    return None


def _download_image_sync(url: str) -> Optional[Image.Image]:
    if not PIL_AVAILABLE or not url:
        return None
    if url in _ARTWORK_CACHE:
        cached = _ARTWORK_CACHE[url]
        return cached.copy() if cached is not None else None
    try:
        req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urlopen(req, timeout=4.0) as resp:
            raw = resp.read()
        im = Image.open(io.BytesIO(raw)).convert("RGBA")
        _ARTWORK_CACHE[url] = im
        return im.copy()
    except Exception:
        _ARTWORK_CACHE[url] = None
        return None


async def _fetch_gift_artwork_image(url: Optional[str]) -> Optional[Image.Image]:
    if not url or not PIL_AVAILABLE:
        return None
    return await asyncio.to_thread(_download_image_sync, url)




async def _fetch_tg_nft_meta_and_image(nft_url: str) -> dict:
    """
    Fetch real gift artwork image and attributes directly from Telegram's public t.me/nft/<slug>-<num> page.
    """
    info = {
        "image_url": None,
        "model": None,
        "model_rarity": None,
        "backdrop": None,
        "backdrop_rarity": None,
        "symbol": None,
        "symbol_rarity": None,
    }
    if not nft_url or not nft_url.startswith("http"):
        return info
    try:
        import aiohttp
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        async with aiohttp.ClientSession(headers=headers) as sess:
            async with sess.get(nft_url, timeout=aiohttp.ClientTimeout(total=7.0)) as resp:
                if resp.status != 200:
                    return info
                html = await resp.text(errors="ignore")

        og_m = re.search(r'<meta[^>]+property=["\x27]og:image["\x27][^>]+content=["\x27]([^"\x27]+)["\x27]', html, re.IGNORECASE)
        if not og_m:
            og_m = re.search(r'<meta[^>]+content=["\x27]([^"\x27]+)["\x27][^>]+property=["\x27]og:image["\x27]', html, re.IGNORECASE)
        if og_m:
            info["image_url"] = og_m.group(1).strip()

        rows = re.findall(r'<tr[^>]*>.*?</tr>', html, flags=re.IGNORECASE | re.DOTALL)
        for tr in rows:
            cells = re.findall(r'<t[hd][^>]*>(.*?)</t[hd]>', tr, flags=re.IGNORECASE | re.DOTALL)
            clean_cells = [re.sub(r'<[^>]+>', ' ', c) for c in cells]
            clean_cells = [re.sub(r'\s+', ' ', c).strip() for c in clean_cells if re.sub(r'\s+', ' ', c).strip()]
            if len(clean_cells) >= 2:
                k_low = clean_cells[0].lower()
                val_txt = clean_cells[1]
                rar_txt = None
                for extra in clean_cells[2:]:
                    rm = re.search(r'(\d+(?:\.\d+)?\s*%)', extra)
                    if rm:
                        rar_txt = rm.group(1).replace(" ", "")
                        break
                if not rar_txt:
                    rm_inline = re.search(r'(\d+(?:\.\d+)?\s*%)', val_txt)
                    if rm_inline:
                        rar_txt = rm_inline.group(1).replace(" ", "")
                        val_txt = val_txt[:rm_inline.start()].strip()

                if "model" in k_low:
                    info["model"] = info["model"] or val_txt
                    info["model_rarity"] = info["model_rarity"] or rar_txt
                elif "backdrop" in k_low:
                    info["backdrop"] = info["backdrop"] or val_txt
                    info["backdrop_rarity"] = info["backdrop_rarity"] or rar_txt
                elif "symbol" in k_low:
                    info["symbol"] = info["symbol"] or val_txt
                    info["symbol_rarity"] = info["symbol_rarity"] or rar_txt
    except Exception:
        pass
    return info

async def fetch_exact_quick_sell_from_mrkt(q_info: dict) -> dict:
    target_slug = (q_info.get("slug") or "").strip()
    target_num = q_info.get("number")
    pretty_name = prettify_slug_title(target_slug)
    result = {
        "title": pretty_name,
        "slug": target_slug,
        "number": target_num,
        "url": q_info.get("url") or DEFAULT_MARKET_URL,
        "image_url": None,
        "model": None,
        "model_rarity": None,
        "backdrop": None,
        "backdrop_rarity": None,
        "symbol": None,
        "symbol_rarity": None,
        "quick_ton": None,
        "floor_ton": None,
        "source": "none",
        "cheapest_three": [],
    }

    tg_meta = await _fetch_tg_nft_meta_and_image(result["url"])
    if tg_meta.get("image_url"):
        result["image_url"] = tg_meta["image_url"]
    if tg_meta.get("model"):
        result["model"] = tg_meta["model"]
    if tg_meta.get("model_rarity"):
        result["model_rarity"] = tg_meta["model_rarity"]
    if tg_meta.get("backdrop"):
        result["backdrop"] = tg_meta["backdrop"]
    if tg_meta.get("backdrop_rarity"):
        result["backdrop_rarity"] = tg_meta["backdrop_rarity"]
    if tg_meta.get("symbol"):
        result["symbol"] = tg_meta["symbol"]
    if tg_meta.get("symbol_rarity"):
        result["symbol_rarity"] = tg_meta["symbol_rarity"]

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
                            if not result["image_url"]:
                                result["image_url"] = _extract_image_url_from_obj(c)
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
                            kwargs[l_key] = 40
                            break
                    g_res = await asyncio.wait_for(fn(**kwargs) if kwargs else fn(target_slug), timeout=8.0)
                    items = g_res if isinstance(g_res, list) else (
                        next((g_res[k] for k in ("items", "gifts", "results", "data") if isinstance(g_res, dict) and isinstance(g_res.get(k), list)), None)
                        or next((getattr(g_res, a) for a in ("items", "gifts", "results", "data") if isinstance(getattr(g_res, a, None), list)), [])
                    )
                    offer_prices = []
                    same_type_listings = []
                    target_model_name = None

                    for it in items:
                        it_name = str((it.get("name") if isinstance(it, dict) else getattr(it, "name", "")) or "")
                        num_m = re.search(r"-(\d+)$", it_name.strip())
                        item_num = int(num_m.group(1)) if num_m else None
                        it_model, it_model_rar = _extract_attr_and_rarity(it, ("model", "model_name"))
                        it_backdrop, it_backdrop_rar = _extract_attr_and_rarity(it, ("backdrop", "backdrop_name"))
                        it_symbol, it_symbol_rar = _extract_attr_and_rarity(it, ("symbol", "symbol_name"))
                        it_img = _extract_image_url_from_obj(it)
                        if it_img and not result["image_url"]:
                            result["image_url"] = it_img
                        if target_num and item_num == target_num:
                            if it_img and not result["image_url"]:
                                result["image_url"] = it_img
                            result["model"] = it_model or result["model"]
                            result["model_rarity"] = it_model_rar or result["model_rarity"]
                            result["backdrop"] = it_backdrop or result["backdrop"]
                            result["backdrop_rarity"] = it_backdrop_rar or result["backdrop_rarity"]
                            result["symbol"] = it_symbol or result["symbol"]
                            result["symbol_rarity"] = it_symbol_rar or result["symbol_rarity"]
                            target_model_name = result["model"]

                        direct_q = _extract_quick_price_from_obj(it)
                        if direct_q is not None:
                            offer_prices.append(direct_q)

                        p_ton = _extract_ton_price_from_obj(it)
                        if p_ton is not None:
                            same_type_listings.append((item_num, p_ton, it_model, it_backdrop, it_symbol, it_img))

                        for o_attr in ("offers", "quick_sell_offers", "bids"):
                            o_list = it.get(o_attr) if isinstance(it, dict) else getattr(it, o_attr, None)
                            if isinstance(o_list, list):
                                for off in o_list:
                                    op = _extract_ton_price_from_obj(off)
                                    if op is not None:
                                        offer_prices.append(op)

                    same_type_listings.sort(key=lambda x: x[1])
                    if same_type_listings:
                        result["cheapest_three"] = [
                            {
                                "number": n,
                                "price": pr,
                                "model": m,
                                "backdrop": b,
                                "symbol": s,
                                "image_url": im_u,
                            }
                            for n, pr, m, b, s, im_u in same_type_listings[:3]
                        ]
                        if target_model_name:
                            same_model = [x for x in same_type_listings if x[2] and str(x[2]).lower() == str(target_model_name).lower()]
                            if same_model:
                                result["floor_ton"] = same_model[0][1]
                        if result["floor_ton"] is None:
                            result["floor_ton"] = same_type_listings[0][1]
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
            ImageFont.truetype(bold_path, 48),
            ImageFont.truetype(bold_path, 30),
            ImageFont.truetype(reg_path, 23),
            ImageFont.truetype(bold_path, 20),
        )
    d = ImageFont.load_default()
    return d, d, d, d


def _draw_clipboard_icon(draw: ImageDraw.ImageDraw, cx: int, cy: int, color_rgb: tuple):
    draw.rounded_rectangle([cx - 28, cy - 32, cx + 28, cy + 38], radius=8, fill=color_rgb)
    draw.rounded_rectangle([cx - 13, cy - 40, cx + 13, cy - 24], radius=5, fill=color_rgb)
    draw.rounded_rectangle([cx - 17, cy - 14, cx - 9, cy - 6], radius=2, fill=(246, 204, 24))
    draw.rounded_rectangle([cx - 2, cy - 12, cx + 17, cy - 7], radius=2, fill=(246, 204, 24))
    draw.rounded_rectangle([cx - 17, cy + 4, cx - 9, cy + 12], radius=2, fill=(246, 204, 24))
    draw.rounded_rectangle([cx - 2, cy + 6, cx + 17, cy + 11], radius=2, fill=(246, 204, 24))


def _draw_cart_icon(draw: ImageDraw.ImageDraw, x_right: int, y_mid: int, color_rgb: tuple):
    draw.line([(x_right - 34, y_mid - 14), (x_right - 27, y_mid - 14), (x_right - 22, y_mid + 4), (x_right - 6, y_mid + 4)], fill=color_rgb, width=3)
    draw.line([(x_right - 25, y_mid - 7), (x_right - 5, y_mid - 7), (x_right - 7, y_mid + 4)], fill=color_rgb, width=2)
    draw.ellipse([x_right - 23, y_mid + 7, x_right - 17, y_mid + 13], fill=color_rgb)
    draw.ellipse([x_right - 12, y_mid + 7, x_right - 6, y_mid + 13], fill=color_rgb)


def _paste_artwork_or_emblem(canvas_img: Image.Image, draw: ImageDraw.ImageDraw, x1: int, y1: int, x2: int, y2: int, bg_rgb: tuple, accent_rgb: tuple, badge_rgb: tuple, art_img: Optional[Image.Image]):
    draw.rounded_rectangle([x1, y1, x2, y2], radius=20, fill=bg_rgb)
    w_box = x2 - x1
    h_box = y2 - y1

    if art_img is not None:
        pad = 16
        target_w = max(40, w_box - pad * 2)
        target_h = max(40, h_box - pad * 2)
        resized = art_img.copy()
        resized.thumbnail((target_w, target_h), Image.Resampling.LANCZOS)
        px = x1 + (w_box - resized.width) // 2
        py = y1 + (h_box - resized.height) // 2
        canvas_img.paste(resized, (px, py), resized)
        return

    step_x = max(62, w_box // 4)
    step_y = max(62, h_box // 4)
    for gx in range(x1 + 30, x2 - 20, step_x):
        for gy in range(y1 + 30, y2 - 20, step_y):
            draw.polygon([(gx, gy - 6), (gx + 6, gy), (gx, gy + 6), (gx - 6, gy)], fill=accent_rgb)

    cx = (x1 + x2) // 2
    cy = (y1 + y2) // 2
    r_outer = min(w_box, h_box) // 4
    draw.ellipse([cx - r_outer, cy - r_outer, cx + r_outer, cy + r_outer], fill=(22, 24, 28), outline=badge_rgb, width=3)
    r_inner = max(14, r_outer // 2)
    draw.polygon([(cx, cy - r_inner), (cx + r_inner, cy), (cx, cy + r_inner), (cx - r_inner, cy)], fill=badge_rgb)


async def render_quick_sell_card_image(data: dict) -> Optional[bytes]:
    if not PIL_AVAILABLE:
        return None
    try:
        cheapest_three = data.get("cheapest_three") or []
        main_art = await _fetch_gift_artwork_image(data.get("image_url"))
        sub_arts = []
        for idx in range(3):
            u = cheapest_three[idx].get("image_url") if idx < len(cheapest_three) else None
            fetched_sub = await _fetch_gift_artwork_image(u) if u else None
            sub_arts.append(fetched_sub or (main_art.copy() if main_art is not None else None))

        w, h = 1120, 940
        img = Image.new("RGB", (w, h), (18, 20, 24))
        draw = ImageDraw.Draw(img)
        f_title, f_price, f_sub, f_badge = _load_clean_fonts()

        title_txt = str(data.get("title") or data.get("slug") or "Gift")
        num_txt = f"#{data['number']}" if data.get("number") else ""
        quick_val = data.get("quick_ton")
        floor_val = data.get("floor_ton")

        draw.rounded_rectangle([20, 20, 366, 880], radius=26, fill=(28, 31, 36))
        _paste_artwork_or_emblem(img, draw, 42, 42, 344, 344, (124, 158, 102), (104, 138, 84), (245, 204, 24), main_art)
        draw.rounded_rectangle([274, 58, 330, 88], radius=8, fill=(20, 22, 26))
        draw.text((283, 64), "MRKT", fill=(245, 204, 24), font=f_badge)

        draw.text((42, 358), title_txt[:18], fill=(248, 250, 252), font=f_title)
        if num_txt:
            draw.text((42, 412), num_txt, fill=(162, 170, 182), font=f_price)

        attrs = [
            ("Model", data.get("model") or "—", data.get("model_rarity") or ""),
            ("Backdrop", data.get("backdrop") or "—", data.get("backdrop_rarity") or ""),
            ("Symbol", data.get("symbol") or "—", data.get("symbol_rarity") or ""),
        ]
        y_attr = 458
        for k_label, v_label, r_label in attrs:
            draw.rounded_rectangle([42, y_attr, 344, y_attr + 104], radius=14, fill=(38, 42, 49))
            draw.text((60, y_attr + 12), k_label, fill=(150, 158, 170), font=f_badge)
            draw.text((60, y_attr + 50), str(v_label)[:20], fill=(248, 250, 252), font=f_price)
            if r_label:
                rb = draw.textbbox((0, 0), r_label, font=f_price)
                draw.text((326 - (rb[2] - rb[0]), y_attr + 50), r_label, fill=(246, 204, 24), font=f_price)
            y_attr += 116

        draw.rounded_rectangle([42, 812, 254, 862], radius=12, fill=(38, 42, 49))
        pill_txt = f"{data.get('model') or title_txt[:12]}  x"
        draw.text((62, 825), pill_txt[:22], fill=(246, 204, 24), font=f_sub)

        draw.rounded_rectangle([388, 20, 742, 446], radius=26, fill=(246, 204, 24))
        cx_y = (388 + 742) // 2
        _draw_clipboard_icon(draw, cx_y, 92, (20, 22, 26))

        quick_str = f"{quick_val:g}" if quick_val is not None else "—"
        q_bbox = draw.textbbox((0, 0), quick_str, font=f_title)
        q_w = q_bbox[2] - q_bbox[0]
        draw.polygon([(cx_y - q_w // 2 - 22, 168), (cx_y - q_w // 2 - 11, 153), (cx_y - q_w // 2, 168), (cx_y - q_w // 2 - 11, 183)], fill=(20, 22, 26))
        draw.text((cx_y - q_w // 2 + 8, 144), quick_str, fill=(20, 22, 26), font=f_title)

        qs_label = "Any gift of this collection"
        qs_bbox = draw.textbbox((0, 0), qs_label, font=f_sub)
        draw.text((cx_y - (qs_bbox[2] - qs_bbox[0]) // 2, 202), qs_label, fill=(68, 58, 20), font=f_sub)

        draw.rounded_rectangle([418, 254, 712, 312], radius=13, fill=(32, 35, 40))
        ao_label = "All Orders"
        ao_bbox = draw.textbbox((0, 0), ao_label, font=f_price)
        draw.text((cx_y - (ao_bbox[2] - ao_bbox[0]) // 2, 270), ao_label, fill=(248, 250, 252), font=f_price)

        draw.rounded_rectangle([418, 328, 712, 396], radius=13, fill=(255, 231, 96))
        qb_label = "Quick sell"
        qb_bbox = draw.textbbox((0, 0), qb_label, font=f_price)
        draw.text((cx_y - (qb_bbox[2] - qb_bbox[0]) // 2, 348), qb_label, fill=(20, 22, 26), font=f_price)

        cheap_boxes = [
            (764, 20, 1100, 446, (162, 104, 92), (138, 84, 74)),
            (388, 468, 742, 880, (132, 92, 96), (108, 72, 76)),
            (764, 468, 1100, 880, (112, 148, 96), (90, 124, 76)),
        ]
        for idx, (x1, y1, x2, y2, bg_c, acc_c) in enumerate(cheap_boxes):
            draw.rounded_rectangle([x1, y1, x2, y2], radius=24, fill=(28, 31, 36))
            _paste_artwork_or_emblem(img, draw, x1 + 18, y1 + 18, x2 - 18, y2 - 118, bg_c, acc_c, (246, 204, 24), sub_arts[idx])

            if idx < len(cheapest_three):
                item = cheapest_three[idx]
                c_num = f"#{item['number']}" if item.get("number") else (num_txt or "Floor")
                c_price = f"{float(item['price']):g}"
            else:
                c_num = num_txt or "Floor"
                c_price = f"{floor_val:g}" if floor_val is not None else "—"

            draw.text((x1 + 22, y2 - 102), title_txt[:15], fill=(248, 250, 252), font=f_price)
            draw.text((x1 + 22, y2 - 68), c_num, fill=(162, 170, 182), font=f_sub)

            py_row = y2 - 22
            draw.polygon([(x1 + 26, py_row), (x1 + 33, py_row - 9), (x1 + 40, py_row), (x1 + 33, py_row + 9)], fill=(248, 250, 252))
            draw.text((x1 + 48, y2 - 36), c_price, fill=(248, 250, 252), font=f_price)
            _draw_cart_icon(draw, x2 - 20, py_row, (162, 170, 182))

        draw.text((24, 898), "MRKT · Quick sell", fill=(150, 158, 170), font=f_sub)
        draw.text((970, 898), "Giftpulse", fill=(150, 158, 170), font=f_sub)

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
            try:
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
            finally:
                try:
                    await tg_client.disconnect()
                except Exception:
                    pass
    except asyncio.CancelledError:
        return
    except Exception as e:
        print(f"Secondary account sync error: {e}")


async def secondary_sync_loop():
    while True:
        try:
            await sync_mrkt_via_secondary_account()
            await asyncio.sleep(max(SYNC_EVERY, 300))
        except asyncio.CancelledError:
            break


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
        card_bytes = await render_quick_sell_card_image(exact_data)
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
