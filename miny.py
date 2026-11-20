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
from mrkt_orders import fetch_top_order_ton

try:
    from PIL import Image, ImageDraw, ImageFont
    PIL_AVAILABLE = True
except Exception:
    PIL_AVAILABLE = False

try:
    import arabic_reshaper
    from bidi.algorithm import get_display
    ARABIC_SHAPING_AVAILABLE = True
except Exception:
    ARABIC_SHAPING_AVAILABLE = False

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
SYNC_EVERY = int(os.getenv("SYNC_EVERY_SECONDS", "120"))
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


def _format_per_mille(val) -> Optional[str]:
    if isinstance(val, (int, float)) and val > 0:
        pct = round(float(val) / 10.0, 2)
        return f"{pct:g}%"
    if isinstance(val, str) and val.strip():
        cleaned = val.strip().rstrip("%")
        try:
            num = float(cleaned)
            if num > 0:
                pct = round(num / 10.0, 2) if "%" not in val else round(num, 2)
                return f"{pct:g}%"
        except Exception:
            pass
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

        for base_k in names:
            val_name = None
            for cand_k in (f"{base_k}_name", f"{base_k}_title", base_k):
                v_raw = container.get(cand_k) if isinstance(container, dict) else getattr(container, cand_k, None)
                if isinstance(v_raw, str) and v_raw.strip():
                    val_name = v_raw.strip()
                    break
            if val_name:
                rar_out = None
                for pm_k in (f"{base_k}_rarity_per_mille", f"{base_k}RarityPerMille", "rarity_per_mille", "rarityPerMille"):
                    pm_val = container.get(pm_k) if isinstance(container, dict) else getattr(container, pm_k, None)
                    rar_out = _format_per_mille(pm_val)
                    if rar_out:
                        break
                if not rar_out:
                    for rk in rarity_keys:
                        rv = container.get(rk) if isinstance(container, dict) else getattr(container, rk, None)
                        if isinstance(rv, (int, float)) and rv > 0:
                            rar_out = f"{rv:g}%"
                            break
                        if isinstance(rv, str) and rv.strip():
                            rar_out = rv.strip() if rv.strip().endswith("%") else f"{rv.strip()}%"
                            break
                return val_name, rar_out

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
                for pm_k in ("rarity_per_mille", "rarityPerMille"):
                    rar_part = _format_per_mille(val.get(pm_k))
                    if rar_part:
                        break
                if not rar_part:
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
                for pm_k in ("rarity_per_mille", "rarityPerMille"):
                    rar_part = _format_per_mille(getattr(val, pm_k, None))
                    if rar_part:
                        break
                if not rar_part:
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
    if url in _ARTWORK_CACHE and _ARTWORK_CACHE[url] is not None:
        return _ARTWORK_CACHE[url].copy()

    last_err = None
    for attempt in range(2):
        try:
            req = Request(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                    "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
                },
            )
            with urlopen(req, timeout=6.0) as resp:
                raw = resp.read()
            if not raw:
                continue
            im = Image.open(io.BytesIO(raw))
            try:
                im.seek(0)
            except Exception:
                pass
            rgba = im.convert("RGBA")
            _ARTWORK_CACHE[url] = rgba
            print(f"[CROP_DEBUG] downloaded ok size={rgba.size} url={url[:90]}", flush=True)
            return rgba.copy()
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            time.sleep(0.25 * (attempt + 1))

    print(f"[CROP_DEBUG] download failed ({last_err}) url={url[:90]}", flush=True)
    return None


async def _fetch_gift_artwork_image(url: Optional[str]) -> Optional[Image.Image]:
    if not url or not PIL_AVAILABLE:
        return None
    return await asyncio.to_thread(_download_image_sync, url)


def _crop_and_fit_artwork(art_img: Optional[Image.Image], box_w: int, box_h: int) -> Optional[Image.Image]:
    """
    Crops and scales artwork cleanly to fill (box_w, box_h) centered on the subject.
    """
    if not PIL_AVAILABLE or art_img is None or box_w <= 0 or box_h <= 0:
        return None
    src_img = art_img.convert("RGBA")
    src_w, src_h = src_img.size
    if src_w <= 0 or src_h <= 0:
        return None

    # Trim outer uniform margin if present so the gift itself fills the crop tightly
    try:
        alpha = src_img.getchannel("A")
        bbox = alpha.getbbox()
        if bbox and (bbox[2] - bbox[0]) >= 24 and (bbox[3] - bbox[1]) >= 24:
            pad_x = int((bbox[2] - bbox[0]) * 0.04)
            pad_y = int((bbox[3] - bbox[1]) * 0.04)
            cx1 = max(0, bbox[0] - pad_x)
            cy1 = max(0, bbox[1] - pad_y)
            cx2 = min(src_w, bbox[2] + pad_x)
            cy2 = min(src_h, bbox[3] + pad_y)
            if cx2 > cx1 and cy2 > cy1:
                src_img = src_img.crop((cx1, cy1, cx2, cy2))
                src_w, src_h = src_img.size
    except Exception:
        pass

    scale = max(box_w / src_w, box_h / src_h)
    scaled_w = max(box_w, int(round(src_w * scale)))
    scaled_h = max(box_h, int(round(src_h * scale)))
    resized = src_img.resize((scaled_w, scaled_h), Image.Resampling.LANCZOS)

    left = max(0, (scaled_w - box_w) // 2)
    top = max(0, (scaled_h - box_h) // 2)
    return resized.crop((left, top, left + box_w, top + box_h))


async def _call_get_gift_smart(mc, col_name_exact: str, target_slug: str, target_num):
    fn = getattr(mc, "get_gift", None)
    if not callable(fn) or target_num is None:
        return None
    slug_num = f"{normalize_slug(col_name_exact) or target_slug}-{target_num}"
    attempts = [
        ("kwargs_gift_id", {"gift_id": slug_num}),
        ("pos1_slug_num", (slug_num,)),
    ]
    for label, arg in attempts:
        try:
            if isinstance(arg, dict):
                res = await asyncio.wait_for(fn(**arg), timeout=7.0)
            else:
                res = await asyncio.wait_for(fn(*arg), timeout=7.0)
            if res is not None:
                print(f"[QUICK_DEBUG] get_gift ({label}) -> type={type(res).__name__} val={repr(res)[:350]}", flush=True)
                return res
        except Exception as e:
            print(f"[QUICK_DEBUG] get_gift ({label}) err={type(e).__name__}: {str(e)[:200]}", flush=True)
            continue
    return None


def _items_from_any_response(res) -> list:
    if res is None:
        return []
    if isinstance(res, list):
        return res
    if isinstance(res, dict):
        for k in ("items", "gifts", "listings", "offers", "orders", "results", "data"):
            if isinstance(res.get(k), list):
                return res[k]
        return [res] if any(x in res for x in ("name", "price", "priceTon", "price_ton", "priceNanoTons", "price_nano_tons", "id")) else []
    for attr in ("items", "gifts", "listings", "offers", "orders", "results", "data"):
        val = getattr(res, attr, None)
        if isinstance(val, list):
            return val
    return [res]


def _extract_gift_number_from_any(obj) -> Optional[int]:
    if obj is None:
        return None
    for attr in ("number", "gift_number", "giftNumber", "index", "num", "serial", "serial_number", "serialNumber"):
        v = obj.get(attr) if isinstance(obj, dict) else getattr(obj, attr, None)
        if isinstance(v, int) and v > 0:
            return v
        if isinstance(v, str) and v.strip().lstrip("#").isdigit():
            iv = int(v.strip().lstrip("#"))
            if iv > 0:
                return iv
    for name_attr in ("name", "title", "slug", "url", "link"):
        s = obj.get(name_attr) if isinstance(obj, dict) else getattr(obj, name_attr, None)
        if isinstance(s, str) and s.strip():
            m = re.search(r"[-#](\d{1,9})(?:\D|$)", s.strip())
            if m and int(m.group(1)) > 0:
                return int(m.group(1))
    return None


def _extract_listing_price_ton(obj) -> Optional[float]:
    if obj is None:
        return None
    ton_keys = (
        "price_ton", "priceTon", "sale_price_ton", "salePriceTon",
        "floor_price_ton", "floorPriceTon", "model_floor_price_ton", "modelFloorPriceTon",
        "amount_ton", "amountTon", "buy_now_price_ton", "buyNowPriceTon",
        "quick_sell_price_ton", "quickSellPriceTon", "offer_price_ton", "offerPriceTon",
        "price", "amount"
    )
    for attr in ton_keys:
        v = obj.get(attr) if isinstance(obj, dict) else getattr(obj, attr, None)
        if isinstance(v, (int, float)) and v > 0:
            return round(float(v), 4)
        if isinstance(v, str):
            m = re.search(r"(\d+(?:\.\d+)?)", v.replace(",", "."))
            if m and float(m.group(1)) > 0:
                return round(float(m.group(1)), 4)

    nano_keys = (
        "price_nano_tons", "priceNanoTons", "sale_price_nano_tons", "salePriceNanoTons",
        "floor_price_nano_tons", "floorPriceNanoTons",
        "model_floor_price_nano_tons", "modelFloorPriceNanoTons",
        "amount_nano_tons", "amountNanoTons", "buy_now_price_nano_tons", "buyNowPriceNanoTons",
        "quick_sell_price_nano_tons", "quickSellPriceNanoTons",
        "offer_price_nano_tons", "offerPriceNanoTons",
        "price_nano", "nano_tons"
    )
    for nano_attr in nano_keys:
        v = obj.get(nano_attr) if isinstance(obj, dict) else getattr(obj, nano_attr, None)
        if isinstance(v, (int, float)) and v > 0:
            return round(float(v) / 1e9, 4)
        if isinstance(v, str) and v.strip().isdigit() and int(v.strip()) > 0:
            return round(int(v.strip()) / 1e9, 4)

    # Fallback: scan any remaining numeric *_nano_tons / *_ton / *_price attribute on object
    raw_dict = obj if isinstance(obj, dict) else getattr(obj, "__dict__", {}) or {}
    if isinstance(raw_dict, dict):
        for k, v in raw_dict.items():
            kl = str(k).lower()
            if ("nano" in kl or "ton" in kl or "price" in kl or "offer" in kl or "quick" in kl) and isinstance(v, (int, float)) and v > 0:
                if "nano" in kl:
                    return round(float(v) / 1e9, 4)
                return round(float(v), 4)
    return None


async def _call_search_gifts_smart(mc, col_name_exact: str, target_slug: str, title_str: str, target_num=None, model_name: Optional[str] = None):
    fn = getattr(mc, "search_gifts", None)
    if not callable(fn):
        return []
    try:
        sig = inspect.signature(fn)
        params = set(sig.parameters.keys()) - {"self"}
    except Exception:
        params = set()

    col_candidates = []
    for c in (col_name_exact, title_str, prettify_slug_title(target_slug), target_slug):
        if c and str(c).strip() and str(c).strip().lower() not in {x.lower() for x in col_candidates}:
            col_candidates.append(str(c).strip())

    collected = []
    seen_ids = set()

    def _add_items(raw_items):
        for obj in _items_from_any_response(raw_items):
            num = _extract_gift_number_from_any(obj)
            pr = _extract_listing_price_ton(obj)
            key = (num, pr, id(obj))
            if key not in seen_ids:
                seen_ids.add(key)
                collected.append(obj)

    for col_val in col_candidates:
        base_kw = {}
        if "collection_names" in params:
            base_kw["collection_names"] = [col_val]
        if "count" in params:
            base_kw["count"] = 30
        elif "limit" in params:
            base_kw["limit"] = 30
        if "low_to_high" in params:
            base_kw["low_to_high"] = True

        if model_name and "model_names" in params:
            kw_model = dict(base_kw)
            kw_model["model_names"] = [str(model_name).strip()]
            try:
                res = await asyncio.wait_for(fn(**kw_model), timeout=7.0)
                _add_items(res)
                print(f"[QUICK_DEBUG] search_gifts (collection+model) col={col_val} -> total={len(collected)}", flush=True)
            except Exception as e:
                print(f"[QUICK_DEBUG] search_gifts (collection+model) err={type(e).__name__}: {str(e)[:200]}", flush=True)

        if base_kw and len(collected) < 3:
            try:
                res = await asyncio.wait_for(fn(**base_kw), timeout=7.0)
                _add_items(res)
                print(f"[QUICK_DEBUG] search_gifts (collection_only) col={col_val} -> total={len(collected)}", flush=True)
            except Exception as e:
                print(f"[QUICK_DEBUG] search_gifts (collection_only) err={type(e).__name__}: {str(e)[:200]}", flush=True)

        if collected:
            break

    return collected


def _extract_quick_offer_price_ton(obj) -> Optional[float]:
    if obj is None:
        return None
    for attr in (
        "quick_sell_price_ton", "quickSellPriceTon", "quick_price_ton", "quickPriceTon",
        "offer_price_ton", "offerPriceTon", "instant_price_ton", "instantPriceTon",
        "quick_sell_price", "quickSellPrice", "offer_price", "offerPrice"
    ):
        v = obj.get(attr) if isinstance(obj, dict) else getattr(obj, attr, None)
        if isinstance(v, (int, float)) and v > 0:
            return round(float(v), 4)
    for nano_attr in (
        "quick_sell_price_nano_tons", "quickSellPriceNanoTons", "quick_price_nano_tons", "quickPriceNanoTons",
        "offer_price_nano_tons", "offerPriceNanoTons", "instant_price_nano_tons", "instantPriceNanoTons"
    ):
        v = obj.get(nano_attr) if isinstance(obj, dict) else getattr(obj, nano_attr, None)
        if isinstance(v, (int, float)) and v > 0:
            return round(float(v) / 1e9, 4)
    return _extract_listing_price_ton(obj)


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


def _call_kwargs_for_sig(fn, preferred_values: dict) -> dict:
    try:
        params = inspect.signature(fn).parameters
    except Exception:
        return {}
    kwargs = {}
    for p_name in params:
        if p_name == "self":
            continue
        low = p_name.lower()
        for k_hint, val in preferred_values.items():
            if val is None:
                continue
            if low == k_hint or k_hint in low:
                kwargs[p_name] = val
                break
    return kwargs


async def _safe_mc_call(mc, method_names: tuple, preferred_values: dict, positional_fallback=None, timeout: float = 8.0):
    for m_name in method_names:
        fn = getattr(mc, m_name, None)
        if not callable(fn):
            continue
        kwargs = _call_kwargs_for_sig(fn, preferred_values)
        try:
            if kwargs:
                return await asyncio.wait_for(fn(**kwargs), timeout=timeout)
            if positional_fallback is not None:
                return await asyncio.wait_for(fn(positional_fallback), timeout=timeout)
            return await asyncio.wait_for(fn(), timeout=timeout)
        except TypeError:
            if positional_fallback is not None:
                try:
                    return await asyncio.wait_for(fn(positional_fallback), timeout=timeout)
                except Exception:
                    continue
        except Exception:
            continue
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

    session_exists = os.path.exists(mrkt_session_path())
    if not (API_ID_ENV.isdigit() and API_HASH_ENV and session_exists):
        return result

    try:
        async with MarketClient(api_id=int(API_ID_ENV), api_hash=API_HASH_ENV, session_name="mrkt", workdir=MRKT_SESSION_DIR) as mc:
            col_name_exact = target_slug
            cols_res = await _safe_mc_call(mc, ("get_collections", "collections", "list_collections"), {}, timeout=8.0)
            cols = _items_from_any_response(cols_res)
            for c in cols:
                c_raw_name = (getattr(c, "name", None) if not isinstance(c, dict) else c.get("name")) or ""
                c_raw_title = (getattr(c, "title", None) if not isinstance(c, dict) else c.get("title")) or ""
                c_slug = normalize_slug(c_raw_name or c_raw_title)
                if c_slug.lower() == target_slug.lower():
                    if c_raw_name:
                        col_name_exact = str(c_raw_name).strip()
                    if c_raw_title:
                        result["title"] = prettify_slug_title(str(c_raw_title).strip())
                    if not result["image_url"]:
                        result["image_url"] = _extract_image_url_from_obj(c)
                    f_ton = _extract_listing_price_ton(c)
                    if f_ton is not None:
                        result["floor_ton"] = f_ton
                        result["source"] = "mrkt_live"
                    q_from_col = _extract_quick_offer_price_ton(c)
                    if q_from_col is not None and q_from_col != result["floor_ton"]:
                        result["quick_ton"] = q_from_col
                    break

            offer_prices = []
            exact_gift_obj = await _call_get_gift_smart(mc, col_name_exact, target_slug, target_num)
            if exact_gift_obj is not None:
                g_img = _extract_image_url_from_obj(exact_gift_obj)
                if g_img and not result["image_url"]:
                    result["image_url"] = g_img
                gm, gmr = _extract_attr_and_rarity(exact_gift_obj, ("model", "model_name"))
                gb, gbr = _extract_attr_and_rarity(exact_gift_obj, ("backdrop", "backdrop_name"))
                gs, gsr = _extract_attr_and_rarity(exact_gift_obj, ("symbol", "symbol_name"))
                result["model"] = gm or result["model"]
                result["model_rarity"] = gmr or result["model_rarity"]
                result["backdrop"] = gb or result["backdrop"]
                result["backdrop_rarity"] = gbr or result["backdrop_rarity"]
                result["symbol"] = gs or result["symbol"]
                result["symbol_rarity"] = gsr or result["symbol_rarity"]

                q_exact = _extract_quick_price_from_obj(exact_gift_obj)
                if q_exact is not None:
                    offer_prices.append(q_exact)
                for q_attr in ("quick_sell_price", "quickSellPrice", "quick_price", "quickPrice", "offer_price", "offerPrice", "best_offer", "bestOffer"):
                    qv = exact_gift_obj.get(q_attr) if isinstance(exact_gift_obj, dict) else getattr(exact_gift_obj, q_attr, None)
                    if isinstance(qv, (int, float)) and qv > 0:
                        offer_prices.append(round(float(qv), 4))
                for o_attr in ("offers", "quick_sell_offers", "quickSells", "bids"):
                    o_list = exact_gift_obj.get(o_attr) if isinstance(exact_gift_obj, dict) else getattr(exact_gift_obj, o_attr, None)
                    if isinstance(o_list, list):
                        for off in o_list:
                            op = _extract_quick_offer_price_ton(off)
                            if op is not None:
                                offer_prices.append(op)

            gift_items = await _call_search_gifts_smart(mc, col_name_exact, target_slug, result["title"], target_num, model_name=result.get("model"))
            if gift_items:
                g0 = gift_items[0]
                g0_dict = g0 if isinstance(g0, dict) else {k: v for k, v in vars(g0).items() if v is not None} if hasattr(g0, "__dict__") else repr(g0)
                print(f"[QUICK_DEBUG] first_gift_non_none={str(g0_dict)[:700]}", flush=True)

            listings_pool = []
            for it in gift_items:
                item_num = _extract_gift_number_from_any(it)
                it_model, it_model_rar = _extract_attr_and_rarity(it, ("model", "model_name"))
                it_backdrop, it_backdrop_rar = _extract_attr_and_rarity(it, ("backdrop", "backdrop_name"))
                it_symbol, it_symbol_rar = _extract_attr_and_rarity(it, ("symbol", "symbol_name"))
                it_img = _extract_image_url_from_obj(it)

                if target_num and item_num == target_num:
                    if it_img and not result["image_url"]:
                        result["image_url"] = it_img
                    result["model"] = it_model or result["model"]
                    result["model_rarity"] = it_model_rar or result["model_rarity"]
                    result["backdrop"] = it_backdrop or result["backdrop"]
                    result["backdrop_rarity"] = it_backdrop_rar or result["backdrop_rarity"]
                    result["symbol"] = it_symbol or result["symbol"]
                    result["symbol_rarity"] = it_symbol_rar or result["symbol_rarity"]

                for q_attr in ("quick_sell_price", "quickSellPrice", "quick_price", "quickPrice", "offer_price", "offerPrice", "best_offer", "bestOffer"):
                    qv = it.get(q_attr) if isinstance(it, dict) else getattr(it, q_attr, None)
                    if isinstance(qv, (int, float)) and qv > 0:
                        offer_prices.append(round(float(qv), 4))
                for o_attr in ("offers", "quick_sell_offers", "quickSells", "bids"):
                    o_list = it.get(o_attr) if isinstance(it, dict) else getattr(it, o_attr, None)
                    if isinstance(o_list, list):
                        for off in o_list:
                            op = _extract_quick_offer_price_ton(off)
                            if op is not None:
                                offer_prices.append(op)

                p_ton = _extract_listing_price_ton(it)
                if p_ton is not None:
                    if target_num is not None and item_num == target_num:
                        continue
                    item_slug_clean = normalize_slug(col_name_exact) or target_slug
                    item_url = f"https://t.me/nft/{item_slug_clean}-{item_num}" if item_num else None
