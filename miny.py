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
DEVELOPER_NAME = "Abbas"
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

user_conversations = {}
user_langs = {} # لتخزين لغة كل مستخدم (ar أو en)

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
    ("IceCreamCone", "IceCreamCone", 1.2, 300, 1.2, 1.1, None, None, None, None, None),
    ("InstantRamen", "InstantRamen", 1.8, 438, 1.8, 1.7, None, None, None, None, None),
    ("ChocolateIceCream", "ChocolateIceCream", 2.0, 449, 2.0, 1.9, None, None, None, None, None),
    ("Torch", "Torch", 2.5, 460, 2.5, 2.4, None, None, None, None, None),
    ("Liberty Figure", "LibertyFigure", 13.24, 480, 13.24, None, None, None, None, None, None),
    ("HipHopDog", "HipHopDog", 4.0, 480, 4.0, 3.8, None, None, None, None, None),
    ("GenieLamp", "GenieLamp", 4.5, 480, 4.5, 4.3, None, None, None, None, None),
    ("LolPop", "LolPop", 5.0, 480, 5.0, 4.8, None, None, None, None, None),
    ("FlamingoFloat", "FlamingoFloat", 6.5, 480, 6.5, 6.2, None, None, None, None, None),
    ("TelegramBag", "TelegramBag", 8.0, 480, 8.0, 7.6, None, None, None, None, None),
    ("Mask", "Mask", 13.8, 843, 13.81, 13.30, None, None, None, None, None),
]

EXPERT_SYSTEM_PROMPT = """
You are "Giftpulse", a warm, lively, and sharp advisor on Telegram Gifts, Telegram Stars, collectible attributes (Model, Backdrop, Symbol), and trading in the MRKT marketplace.

STRICT RULES:
1. Keep replies VERY SHORT (1 or 2 concise lines MAXIMUM, never more). Speak with warmth, natural energy, and human touch—never dry or robotic—while keeping every word tight and free of fluff. When presenting gift results, start with: Here are the best results I found on <a href="https://t.me/mrkt/app">Market</a>: followed by items formatted as Gift Name #Number — Price TON (or Gift Name — Price TON if no number is given), including exact price/floor/quick/model/number details from the database entries when relevant.
2. NEVER invent NFT gift numbers (#...) if a gift number is not explicitly given in the database entry. Only mention a number if it is explicitly shown in the database entry.
3. NEVER say "I cannot show links". Gift names in replies are automatically turned into clickable links by the bot, and there is an "Open MRKT" button below.
4. Never use ** or markdown asterisks. Plain text only (except the exact <a href="https://t.me/mrkt/app">Market</a> link when presenting results).
5. Prices in MRKT are in TON (not thousands of Stars). Floor = floor price; Quick = instant sale price (slightly below floor).
6. Answer ONLY about Telegram Gifts, Telegram Stars, collectibles, and MRKT. Outside scope: reply in one short line:
   I only specialize in gifts and related topics.
"""


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
    ton_str = f"{float(ton_val):g} TON" if ton_val is not None else "Unknown"
    name_with_num = (
        f"{r['title']} #{r['gift_number']}"
        if r.get("gift_number") is not None and int(r["gift_number"]) > 0
        else r["title"]
    )
    s = f"{name_with_num} — {ton_str}"
    if r.get("floor_price") is not None:
        s += f" | Floor {r['floor_price']} TON"
    if r.get("quick_price") is not None:
        s += f" | Quick {r['quick_price']} TON"
    if r.get("model"):
        s += f" | Model {r['model']}"
    if r.get("backdrop"):
        s += f" | Backdrop {r['backdrop']}"
    if r.get("symbol"):
        s += f" | Symbol {r['symbol']}"
    return s


def get_relevant_gifts_summary(user_text: str) -> str:
    rows = get_all_gifts_rows()
    if not rows:
        return "No gifts registered."

    q_low = user_text.lower()
    matched = []
    for r in rows:
        title = (r.get("title") or "").strip()
        slug = (r.get("slug") or "").strip()
        if (title and title.lower() in q_low) or (slug and len(slug) >= 2 and slug.lower() in q_low):
            matched.append(r)

    budget_match = re.search(r"(\d+(?:\.\d+)?)\s*(?:ton)", q_low)
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
        "Database and Sync Status:",
        f"- Total gifts saved: {total} (Confirmed numbers: {with_nums})",
        f"- Last sync: {_SYNC_STATUS['last_sync_at'] or 'Not yet'} (Collections: {_SYNC_STATUS['last_synced_count']} | Numbers: {_SYNC_STATUS['last_numbers_count']})",
        f"- Interval: {SYNC_EVERY} seconds",
        f"- Top names: {top_names or 'None'}",
    ]
    if _SYNC_STATUS["last_error"]:
        lines.append(f"- Last error: {_SYNC_STATUS['last_error']}")
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
    # يقبل الكلمة العربية والإنجليزية
    m = re.match(r"^(?:كويك|quick)\s+((?:https?://)?t\.me/nft/([A-Za-z0-9_]+)-(\d+))$", text.strip(), re.IGNORECASE)
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
        "quick_sell_price_ton", "quickSellPriceTon", "quick_price_ton", "quickPriceTon",
        "offer_price_ton", "offerPriceTon", "price_ton", "priceTon", "floor_price_ton",
        "floorPriceTon", "price", "amount",
    ):
        val = obj.get(attr) if isinstance(obj, dict) else getattr(obj, attr, None)
        if isinstance(val, (int, float)) and val > 0:
            return round(float(val), 4)
    for nano_attr in (
        "quick_sell_price_nano_tons", "quickSellPriceNanoTons", "quick_price_nano_tons",
        "quickPriceNanoTons", "offer_price_nano_tons", "offerPriceNanoTons", "price_nano_tons",
        "priceNanoTons", "floor_price_nano_tons", "floorPriceNanoTons", "price_nano", "nano_tons",
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
    if not PIL_AVAILABLE or art_img is None or box_w <= 0 or box_h <= 0:
        return None
    src_img = art_img.convert("RGBA")
    src_w, src_h = src_img.size
    if src_w <= 0 or src_h <= 0:
        return None

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
                    
                    if item_num:
                        item_url = "https://t.me/nft/" + str(item_slug_clean) + "-" + str(item_num)
                    else:
                        item_url = None
                        
                    listings_pool.append({
                        "title": result["title"],
                        "number": item_num,
                        "price": p_ton,
                        "model": it_model,
                        "backdrop": it_backdrop,
                        "symbol": it_symbol,
                        "image_url": it_img,
                        "url": item_url,
                    })

            listings_pool.sort(key=lambda x: x["price"])
            top_three = listings_pool[:3]
            for entry in top_three:
                if not entry.get("image_url") and entry.get("number"):
                    slug_for_sub = normalize_slug(col_name_exact) or target_slug
                    sub_url = "https://t.me/nft/" + str(slug_for_sub) + "-" + str(entry["number"])
                    sub_meta = await _fetch_tg_nft_meta_and_image(sub_url)
                    if sub_meta.get("image_url"):
                        entry["image_url"] = sub_meta["image_url"]

            if result["quick_ton"] is None:
                top_order_ton, top_order_dbg = await fetch_top_order_ton(col_name_exact)
                print(f"[QUICK_DEBUG] top_order={top_order_ton} | {top_order_dbg[:300]}", flush=True)
                if top_order_ton is not None:
                    result["quick_ton"] = top_order_ton
                    result["source"] = "mrkt_live"

            result["cheapest_three"] = top_three
            if top_three:
                if result["floor_ton"] is None:
                    result["floor_ton"] = top_three[0]["price"]
                result["source"] = "mrkt_live"
            if offer_prices:
                result["quick_ton"] = max(offer_prices)
                result["source"] = "mrkt_live"
            print(f"[QUICK_DEBUG] done quick_ton={result['quick_ton']} floor_ton={result['floor_ton']} cheapest_three={[(x.get('number'), x.get('price'), bool(x.get('image_url'))) for x in top_three]}", flush=True)
    except Exception as e:
        print(f"[QUICK_DEBUG] MarketClient exception: {type(e).__name__}: {e}", flush=True)

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
            ImageFont.truetype(bold_path, 72),
            ImageFont.truetype(bold_path, 52),
            ImageFont.truetype(reg_path, 38),
            ImageFont.truetype(bold_path, 30),
        )
    d = ImageFont.load_default()
    return d, d, d, d


def _draw_clipboard_icon(draw: ImageDraw.ImageDraw, cx: int, cy: int, color_rgb: tuple):
    draw.rounded_rectangle([cx - 34, cy - 38, cx + 34, cy + 44], radius=10, fill=color_rgb)
    draw.rounded_rectangle([cx - 16, cy - 48, cx + 16, cy - 28], radius=6, fill=color_rgb)
    draw.rounded_rectangle([cx - 20, cy - 16, cx - 10, cy - 7], radius=2, fill=(246, 204, 24))
    draw.rounded_rectangle([cx - 2, cy - 14, cx + 20, cy - 8], radius=2, fill=(246, 204, 24))
    draw.rounded_rectangle([cx - 20, cy + 5, cx - 10, cy + 14], radius=2, fill=(246, 204, 24))
    draw.rounded_rectangle([cx - 2, cy + 7, cx + 20, cy + 13], radius=2, fill=(246, 204, 24))


def _draw_cart_icon(draw: ImageDraw.ImageDraw, x_right: int, y_mid: int, color_rgb: tuple):
    draw.line([(x_right - 38, y_mid - 16), (x_right - 29, y_mid - 16), (x_right - 23, y_mid + 5), (x_right - 6, y_mid + 5)], fill=color_rgb, width=3)
    draw.line([(x_right - 26, y_mid - 8), (x_right - 5, y_mid - 8), (x_right - 7, y_mid + 5)], fill=color_rgb, width=2)
    draw.ellipse([x_right - 25, y_mid + 9, x_right - 18, y_mid + 16], fill=color_rgb)
    draw.ellipse([x_right - 12, y_mid + 9, x_right - 5, y_mid + 16], fill=color_rgb)


def _draw_filter_icon(draw: ImageDraw.ImageDraw, x_left: int, y_mid: int, color_rgb: tuple):
    draw.line([(x_left, y_mid - 10), (x_left + 22, y_mid - 10)], fill=color_rgb, width=3)
    draw.line([(x_left + 4, y_mid), (x_left + 18, y_mid)], fill=color_rgb, width=3)
    draw.line([(x_left + 8, y_mid + 10), (x_left + 14, y_mid + 10)], fill=color_rgb, width=3)


def _format_clean_ton(val) -> str:
    if val is None:
        return ""
    try:
        fval = float(val)
    except Exception:
        return str(val)
    rounded = round(fval, 2)
    return f"{rounded:g}"


def _paste_fetched_photo_only(
    canvas_img: Image.Image,
    draw: ImageDraw.ImageDraw,
    x1: int,
    y1: int,
    x2: int,
    y2: int,
    frame_rgb: tuple,
    art_img: Optional[Image.Image],
    radius: int = 20,
):
    draw.rounded_rectangle([x1, y1, x2, y2], radius=radius, fill=frame_rgb)
    if art_img is None:
        return
    box_w = max(1, x2 - x1)
    box_h = max(1, y2 - y1)
    cropped = _crop_and_fit_artwork(art_img, box_w, box_h)
    if cropped is None:
        return

    mask = Image.new("L", (box_w, box_h), 0)
    mdraw = ImageDraw.Draw(mask)
    mdraw.rounded_rectangle([0, 0, box_w, box_h], radius=radius, fill=255)
    canvas_img.paste(cropped, (x1, y1), mask)


def _compose_gift_info_panel(img: Image.Image, draw: ImageDraw.ImageDraw, data: dict, main_art: Optional[Image.Image], fonts: tuple):
    f_title, f_price, f_sub, f_badge = fonts
    title_txt = str(data.get("title") or data.get("slug") or "")
    num_txt = f"#{data['number']}" if data.get("number") else ""

    draw.rounded_rectangle([18, 18, 412, 954], radius=28, fill=(28, 31, 36))
    _paste_fetched_photo_only(img, draw, 28, 28, 402, 440, (28, 31, 36), main_art, radius=24)
    
    if main_art is not None:
        draw.rounded_rectangle([310, 44, 390, 84], radius=8, fill=(18, 20, 24))
        draw.text((325, 52), "MRKT", fill=(246, 204, 24), font=f_badge)

    if title_txt:
        draw.text((30, 465), title_txt[:18], fill=(248, 250, 252), font=f_title)
    if num_txt:
        draw.text((30, 545), num_txt, fill=(168, 176, 188), font=f_price)

    attrs = [
        ("Model", data.get("model"), data.get("model_rarity")),
        ("Backdrop", data.get("backdrop"), data.get("backdrop_rarity")),
        ("Symbol", data.get("symbol"), data.get("symbol_rarity")),
    ]
    y_attr = 600
    for k_label, v_label, r_label in attrs:
        draw.rounded_rectangle([30, y_attr, 400, y_attr + 100], radius=16, fill=(38, 42, 49))
        draw.text((48, y_attr + 12), k_label, fill=(155, 163, 175), font=f_badge)
        
        if v_label:
            draw.text((48, y_attr + 52), str(v_label)[:18], fill=(248, 250, 252), font=f_price)
        if r_label:
            rb = draw.textbbox((0, 0), str(r_label), font=f_badge)
            draw.text((380 - (rb[2] - rb[0]), y_attr + 12), str(r_label), fill=(246, 204, 24), font=f_badge)
        y_attr += 115

    if data.get("model"):
        _draw_filter_icon(draw, 34, 912, (248, 250, 252))
        draw.rounded_rectangle([72, 885, 350, 940], radius=13, fill=(38, 42, 49))
        pill_txt = f"{data.get('model')}  ×"
        draw.text((92, 898), pill_txt[:20], fill=(246, 204, 24), font=f_badge)


def _compose_quick_sell_panel(img: Image.Image, draw: ImageDraw.ImageDraw, quick_val, fonts: tuple):
    f_title, f_price, f_sub, f_badge = fonts
    draw.rounded_rectangle([430, 18, 806, 480], radius=28, fill=(28, 31, 36))
    draw.rounded_rectangle([430, 18, 806, 310], radius=28, fill=(246, 204, 24))
    cx_y = (430 + 806) // 2
    
    draw.ellipse([756, 36, 786, 66], fill=(20, 22, 26))
    draw.text((765, 37), "!", fill=(246, 204, 24), font=f_price)
    _draw_clipboard_icon(draw, cx_y, 100, (20, 22, 26))

    if quick_val is not None:
        quick_str = f"{_format_clean_ton(quick_val)} TON"
        q_bbox = draw.textbbox((0, 0), quick_str, font=f_title)
        q_w = q_bbox[2] - q_bbox[0]
        draw.polygon([(cx_y - q_w // 2 - 35, 185), (cx_y - q_w // 2 - 15, 160), (cx_y - q_w // 2, 185), (cx_y - q_w // 2 - 15, 210)], fill=(20, 22, 26))
        draw.text((cx_y - q_w // 2 + 10, 160), quick_str, fill=(20, 22, 26), font=f_title)
    else:
        no_price = "Not Available"
        q_bbox = draw.textbbox((0, 0), no_price, font=f_title)
        q_w = q_bbox[2] - q_bbox[0]
        draw.text((cx_y - q_w // 2, 175), no_price, fill=(20, 22, 26), font=f_title)

    sub_q = "Any gift of this collection"
    sq_bbox = draw.textbbox((0, 0), sub_q, font=f_badge)
    draw.text((cx_y - (sq_bbox[2] - sq_bbox[0]) // 2, 255), sub_q, fill=(68, 58, 20), font=f_badge)

    draw.rounded_rectangle([450, 335, 786, 405], radius=14, fill=(44, 48, 55))
    ao_label = "All Orders"
    ao_bbox = draw.textbbox((0, 0), ao_label, font=f_price)
    draw.text((cx_y - (ao_bbox[2] - ao_bbox[0]) // 2, 355), ao_label, fill=(248, 250, 252), font=f_price)

    draw.rounded_rectangle([450, 420, 786, 480], radius=14, fill=(255, 231, 96))
    qb_label = "Quick sell"
    qb_bbox = draw.textbbox((0, 0), qb_label, font=f_price)
    draw.text((cx_y - (qb_bbox[2] - qb_bbox[0]) // 2, 438), qb_label, fill=(20, 22, 26), font=f_price)


def _compose_cheapest_gift_card(img: Image.Image, draw: ImageDraw.ImageDraw, box: tuple, item: Optional[dict], art_img: Optional[Image.Image], default_title: str, fonts: tuple):
    _, f_price, f_sub, _ = fonts
    x1, y1, x2, y2 = box
    draw.rounded_rectangle([x1, y1, x2, y2], radius=26, fill=(28, 31, 36))
    if item is None:
        return
        
    art_bottom = y1 + 310
    _paste_fetched_photo_only(img, draw, x1 + 14, y1 + 14, x2 - 14, art_bottom, (28, 31, 36), art_img, radius=20)
    
    c_title = str(item.get("title") or default_title)[:16]
    c_num = f"#{item['number']}" if item.get("number") else ""
    c_price = _format_clean_ton(item.get("price"))
    
    if c_title:
        draw.text((x1 + 20, art_bottom + 15), c_title, fill=(248, 250, 252), font=f_price)
    if c_num:
        draw.text((x1 + 20, art_bottom + 75), c_num, fill=(162, 170, 182), font=f_sub)
    if c_price:
        py_row = y2 - 45
        draw.polygon([(x1 + 22, py_row), (x1 + 32, py_row - 12), (x1 + 42, py_row), (x1 + 32, py_row + 12)], fill=(248, 250, 252))
        draw.text((x1 + 55, y2 - 70), c_price, fill=(248, 250, 252), font=f_price)
        _draw_cart_icon(draw, x2 - 20, py_row, (162, 170, 182))


# ===================== رسّام صورة الكويك سيل (دقة عالية، خطوط كبيرة) =====================
# مقاسات التصميم (نفس أبعاد صورة MRKT الأصلية) ثم نرسم بدقة مضاعفة لنعومة الحواف
QC_W, QC_H = 1524, 1310
QC_SS = 2

QC_BG = (17, 17, 20)
QC_CARD = (29, 29, 33)
QC_ROW = (40, 40, 46)
QC_BTN = (44, 44, 50)
QC_WHITE = (247, 248, 250)
QC_GRAY = (150, 154, 163)
QC_YELLOW = (255, 214, 24)
QC_YELLOW_SOFT = (255, 226, 92)
QC_BLACK = (18, 18, 20)

_qc_BOLD_PATHS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
]
_qc_REG_PATHS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
]
_qc_FONT_CACHE = {}


def _qc_font(size: int, bold: bool = False):
    """يرجع (خط, سماكة_تغميق). إن لم يوجد خط نظام نستخدم الخط المدمج في Pillow بحجم قابل للتغيير."""
    key = (size, bold)
    if key in _qc_FONT_CACHE:
        return _qc_FONT_CACHE[key]
    paths = _qc_BOLD_PATHS if bold else _qc_REG_PATHS
    for p in paths:
        if os.path.exists(p):
            try:
                res = (ImageFont.truetype(p, size), 0)
                _qc_FONT_CACHE[key] = res
                return res
            except Exception:
                pass
    try:
        f = ImageFont.load_default(size=size)  # Pillow >= 10.1: خط مدمج بحجم قابل للتحديد
        res = (f, max(1, size // 28) if bold else 0)
    except TypeError:
        res = (ImageFont.load_default(), 0)
    _qc_FONT_CACHE[key] = res
    return res


def _qc_text(d, x, y, txt, size, fill, bold=False, align="l"):
    """يرسم نصاً. align: l يسار، r يمين، c وسط (بالنسبة لـ x). y هو أعلى النص."""
    if txt is None or txt == "":
        return 0
    font, stroke = _qc_font(int(size * QC_SS), bold)
    bb = d.textbbox((0, 0), str(txt), font=font, stroke_width=stroke)
    w = bb[2] - bb[0]
    px = x * QC_SS
    if align == "r":
        px -= w
    elif align == "c":
        px -= w / 2
    d.text((px - bb[0], y * QC_SS - bb[1]), str(txt), font=font, fill=fill, stroke_width=stroke, stroke_fill=fill)
    return w / QC_SS


def _qc_rr(d, box, r, fill):
    x1, y1, x2, y2 = box
    d.rounded_rectangle([x1 * QC_SS, y1 * QC_SS, x2 * QC_SS, y2 * QC_SS], radius=r * QC_SS, fill=fill)


def _qc_poly(d, pts, fill):
    d.polygon([(x * QC_SS, y * QC_SS) for x, y in pts], fill=fill)


def _qc_diamond(d, cx, cy, w, h, fill):
    """رمز التون (ماسة) كما في MRKT."""
    top = cy - h / 2
    _qc_poly(d, [(cx - w / 2, top + h * 0.22), (cx - w * 0.28, top), (cx + w * 0.28, top), (cx + w / 2, top + h * 0.22),
              (cx, cy + h / 2)], fill)


def _qc_cover(src: Image.Image, w: int, h: int) -> Image.Image:
    src = src.convert("RGB")
    sw, sh = src.size
    scale = max(w / sw, h / sh)
    nw, nh = max(w, int(round(sw * scale))), max(h, int(round(sh * scale)))
    im = src.resize((nw, nh), Image.Resampling.LANCZOS)
    l, t = (nw - w) // 2, (nh - h) // 2
    return im.crop((l, t, l + w, t + h))


def _qc_paste_art(canvas, art, box, radius, corners=None):
    x1, y1, x2, y2 = [int(v * QC_SS) for v in box]
    w, h = x2 - x1, y2 - y1
    if art is None:
        return
    im = None
    try:
        im = _crop_and_fit_artwork(art, w, h)
    except Exception:
        im = None
    if im is None:
        im = _qc_cover(art, w, h)
    im = im.convert("RGB")
    mask = Image.new("L", (w, h), 0)
    md = ImageDraw.Draw(mask)
    if corners is None:
        md.rounded_rectangle([0, 0, w - 1, h - 1], radius=radius * QC_SS, fill=255)
    else:
        md.rounded_rectangle([0, 0, w - 1, h - 1], radius=radius * QC_SS, fill=255, corners=corners)
    canvas.paste(im, (x1, y1), mask)


def _qc_fmt(val) -> str:
    if val is None:
        return ""
    try:
        return f"{round(float(val), 2):g}"
    except Exception:
        return str(val)


def _qc_cart(d, cx, cy, fill):
    pts = [(cx - 18, cy - 14), (cx - 11, cy - 14), (cx - 6, cy + 6), (cx + 13, cy + 6)]
    d.line([(x * QC_SS, y * QC_SS) for x, y in pts], fill=fill, width=int(3 * QC_SS), joint="curve")
    d.line([((cx - 9) * QC_SS, (cy - 7) * QC_SS), ((cx + 16) * QC_SS, (cy - 7) * QC_SS), ((cx + 13) * QC_SS, (cy + 5) * QC_SS)],
           fill=fill, width=int(2.5 * QC_SS))
    for ex in (cx - 4, cx + 11):
        d.ellipse([(ex - 3.5) * QC_SS, (cy + 10) * QC_SS, (ex + 3.5) * QC_SS, (cy + 17) * QC_SS], fill=fill)


def _qc_clipboard(d, cx, cy):
    _qc_rr(d, (cx - 46, cy - 52, cx + 46, cy + 66), 16, QC_BLACK)
    _qc_rr(d, (cx - 22, cy - 66, cx + 22, cy - 38), 10, QC_BLACK)
    for i, yy in enumerate((cy - 22, cy + 8)):
        _qc_rr(d, (cx - 28, yy, cx - 14, yy + 14), 5, QC_YELLOW)
        _qc_rr(d, (cx - 4, yy + 3, cx + 30, yy + 11), 4, QC_YELLOW)


def _qc_small_card(d, canvas, box, item, art, default_title):
    x1, y1, x2, y2 = box
    _qc_rr(d, box, 36, QC_CARD)
    art_h = x2 - x1  # صورة مربعة
    if item is None:
        return
    _qc_paste_art(canvas, art, (x1, y1, x2, y1 + art_h), 36, corners=(True, True, False, False))
    title = str(item.get("title") or default_title or "")[:18]
    num = f"#{item['number']}" if item.get("number") else ""
    _qc_text(d, x1 + 22, y1 + art_h + 18, title, 36, QC_WHITE, bold=True)
    _qc_text(d, x1 + 22, y1 + art_h + 64, num, 28, QC_GRAY)
    py1, py2 = y2 - 78, y2 - 12
    _qc_rr(d, (x1 + 18, py1, x2 - 104, py2), 16, QC_BTN)
    price = _qc_fmt(item.get("price"))
    _qc_diamond(d, x1 + 62, (py1 + py2) / 2, 34, 28, QC_GRAY)
    _qc_text(d, x1 + 98, py1 + 13, price, 38, QC_WHITE, bold=True)
    _qc_rr(d, (x2 - 92, py1, x2 - 18, py2), 16, QC_BTN)
    _qc_cart(d, x2 - 55, (py1 + py2) / 2, QC_GRAY)


def _qc_build_card(data: dict, main_art: Optional[Image.Image], sub_arts: list) -> bytes:
    canvas = Image.new("RGB", (QC_W * QC_SS, QC_H * QC_SS), QC_BG)
    d = ImageDraw.Draw(canvas)

    # ---------- البطاقة اليسرى: الهدية ----------
    _qc_rr(d, (28, 28, 628, 1236), 36, QC_CARD)
    _qc_paste_art(canvas, main_art, (52, 52, 604, 604), 30)
    if main_art is not None:
        _qc_rr(d, (490, 52, 590, 108), 18, QC_YELLOW)
        _qc_text(d, 540, 66, "MRKT", 28, QC_BLACK, bold=True, align="c")

    title = str(data.get("title") or data.get("slug") or "")[:20]
    _qc_text(d, 56, 626, title, 58, QC_WHITE, bold=True)
    if data.get("number"):
        _qc_text(d, 58, 700, f"#{data['number']}", 38, QC_GRAY, bold=True)

    rows = [("Model", data.get("model"), data.get("model_rarity")),
            ("Backdrop", data.get("backdrop"), data.get("backdrop_rarity")),
            ("Symbol", data.get("symbol"), data.get("symbol_rarity"))]
    y = 766
    for label, val, rar in rows:
        _qc_rr(d, (52, y, 604, y + 92), 16, QC_ROW)
        _qc_text(d, 74, y + 12, label, 23, QC_GRAY)
        _qc_text(d, 74, y + 42, str(val or "—")[:20], 34, QC_WHITE, bold=True)
        if rar:
            _qc_text(d, 582, y + 46, str(rar), 29, QC_YELLOW, bold=True, align="r")
        y += 104

    _qc_rr(d, (52, 1088, 116, 1152), 16, QC_ROW)
    for i, (xa, xb) in enumerate(((68, 100), (74, 94), (80, 88))):
        yy = 1104 + i * 16
        d.line([(xa * QC_SS, yy * QC_SS), (xb * QC_SS, yy * QC_SS)], fill=QC_WHITE, width=int(3 * QC_SS))
    if data.get("model"):
        _qc_rr(d, (128, 1088, 400, 1152), 16, QC_ROW)
        chip_w = _qc_text(d, 152, 1104, str(data["model"])[:16], 28, QC_YELLOW, bold=True)
        cx0 = 152 + chip_w + 22
        d.line([(cx0 * QC_SS, 1112 * QC_SS), ((cx0 + 14) * QC_SS, 1126 * QC_SS)], fill=QC_YELLOW, width=int(3 * QC_SS))
        d.line([((cx0 + 14) * QC_SS, 1112 * QC_SS), (cx0 * QC_SS, 1126 * QC_SS)], fill=QC_YELLOW, width=int(3 * QC_SS))

    # ---------- المربع الأصفر: الكويك سيل ----------
    _qc_rr(d, (652, 28, 1062, 620), 36, QC_CARD)
    _qc_rr(d, (652, 28, 1062, 438), 36, QC_YELLOW)
    d.rectangle([652 * QC_SS, 400 * QC_SS, 1062 * QC_SS, 438 * QC_SS], fill=QC_YELLOW)
    _qc_rr(d, (988, 52, 1040, 104), 14, QC_YELLOW_SOFT)
    d.ellipse([1002 * QC_SS, 66 * QC_SS, 1026 * QC_SS, 90 * QC_SS], fill=QC_BLACK)
    _qc_text(d, 1014, 67, "!", 22, QC_YELLOW, bold=True, align="c")
    _qc_clipboard(d, 857, 172)

    quick = data.get("quick_ton")
    if quick is not None:
        qs = _qc_fmt(quick)
        font, stroke = _qc_font(int(88 * QC_SS), True)
        bb = d.textbbox((0, 0), qs, font=font, stroke_width=stroke)
        tw = (bb[2] - bb[0]) / QC_SS
        total = tw + 70
        left = 857 - total / 2
        _qc_diamond(d, left + 24, 338, 48, 40, QC_BLACK)
        _qc_text(d, left + 70, 288, qs, 88, QC_BLACK, bold=True)
    else:
        _qc_text(d, 857, 296, "—", 88, QC_BLACK, bold=True, align="c")
    _qc_text(d, 857, 372, "Any gift of this collection", 27, (46, 40, 10), align="c")

    _qc_rr(d, (670, 460, 1044, 520), 16, QC_BTN)
    _qc_text(d, 857, 474, "All Orders", 30, QC_WHITE, align="c")
    _qc_rr(d, (670, 534, 1044, 600), 16, QC_YELLOW)
    _qc_text(d, 857, 550, "Quick sell", 32, QC_BLACK, bold=True, align="c")

    # ---------- أرخص ثلاث هدايا ----------
    cheap = data.get("cheapest_three") or []
    boxes = [(1086, 28, 1496, 620), (652, 644, 1062, 1236), (1086, 644, 1496, 1236)]
    for i, box in enumerate(boxes):
        item = cheap[i] if i < len(cheap) else None
        art = sub_arts[i] if i < len(sub_arts) else None
        _qc_small_card(d, canvas, box, item, art, title)

    _qc_text(d, 34, 1256, "MRKT · Quick sell", 27, QC_GRAY)
    _qc_text(d, 1490, 1256, "Giftpulse", 27, QC_GRAY, bold=True, align="r")

    buf = io.BytesIO()
    canvas.save(buf, format="JPEG", quality=93, optimize=True, subsampling=0)
    return buf.getvalue()


async def render_quick_sell_card_image(data: dict) -> Optional[bytes]:
    if not PIL_AVAILABLE:
        return None
    try:
        cheapest_three = data.get("cheapest_three") or []
        main_art = await _fetch_gift_artwork_image(data.get("image_url"))
        sub_arts = []
        for idx in range(3):
            u = cheapest_three[idx].get("image_url") if idx < len(cheapest_three) else None
            sub_img = await _fetch_gift_artwork_image(u) if u else None
            if sub_img is None and idx < len(cheapest_three) and main_art is not None:
                sub_img = main_art.copy()
            sub_arts.append(sub_img)

        return await asyncio.to_thread(_qc_build_card, data, main_art, sub_arts)
    except Exception as e:
        print(f"[CROP_DEBUG] render error: {type(e).__name__}: {e}", flush=True)
        return None


def build_quick_sell_reply(data: dict, lang="ar") -> str:
    num_part = f" #{data['number']}" if data.get("number") else ""
    display_name = f"{data.get('title') or data.get('slug')}{num_part}"
    gift_link = f'<a href="{data["url"]}">{display_name}</a>'

    attrs = [a for a in (data.get("model"), data.get("backdrop"), data.get("symbol")) if a]
    attrs_line = " · ".join(attrs) if attrs else ("بيانات الهدية من ماركت" if lang == "ar" else "Gift data from MRKT")

    quick_val = data.get("quick_ton")
    floor_val = data.get("floor_ton")

    if lang == "ar":
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
            "🎯الطلب على: الكولكشن",
            floor_line,
            relation_line,
            "⏱ 0.5ث · MRKT",
        ]
    else:
        if quick_val is not None:
            usd_val = round(float(quick_val) * TON_USD_RATE, 2)
            quick_line = f"⚡Quick Sell: {quick_val:g} TON ≈ ${usd_val:g}"
        else:
            quick_line = "⚡Quick Sell: Not available on MRKT"

        if floor_val is not None:
            floor_line = f"📉Floor Price: {floor_val:g} TON"
        else:
            floor_line = "📉Floor Price: Not available"

        if quick_val is not None and floor_val is not None and floor_val > 0:
            diff_pct = round(((floor_val - quick_val) / floor_val) * 100, 1)
            relation_line = f"↕ Difference: {diff_pct:g}% below floor" if diff_pct >= 0 else f"↕ Difference: {abs(diff_pct):g}% above floor"
        else:
            relation_line = "↕ Difference: Not available"

        lines = [
            f"🎁 {gift_link}",
            attrs_line,
            "",
            quick_line,
            "🎯Demand: Collection",
            floor_line,
            relation_line,
            "⏱ 0.5s · MRKT",
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
        raise RuntimeError("TG_API_ID / TG_API_HASH missing.")

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
            await asyncio.sleep(max(SYNC_EVERY, 60))
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


# دالة لإنشاء أزرار اللغة
def get_lang_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🇬🇧 English", callback_data="lang_en"),
            InlineKeyboardButton(text="🇸🇦 العربية", callback_data="lang_ar")
        ]
    ])


@dp.message(lambda message: message.text and message.text.startswith("/start"))
async def cmd_start(message: types.Message):
    user_conversations.pop(message.from_user.id, None)
    lang = user_langs.get(message.from_user.id, "ar")
    
    text = (
        "أهلاً بك في Giftpulse — هدايا تيليجرام وسوق "
        f'<a href="{DEFAULT_MARKET_URL}">ماركت</a>.\n'
        f'المطور: <a href="https://t.me/{DEVELOPER_USERNAME}">{DEVELOPER_NAME}</a>\n\n'
        "اختر لغتك المفضلة من الأسفل:"
    ) if lang == "ar" else (
        "Welcome to Giftpulse — Telegram Gifts and the "
        f'<a href="{DEFAULT_MARKET_URL}">MRKT</a>.\n'
        f'Developer: <a href="https://t.me/{DEVELOPER_USERNAME}">{DEVELOPER_NAME}</a>\n\n'
        "Please choose your preferred language below:"
    )
    await message.answer(text, parse_mode="HTML", disable_web_page_preview=True, reply_markup=get_lang_keyboard())


# معالج زر تغيير اللغة
@dp.callback_query(lambda c: c.data.startswith("lang_"))
async def change_lang_callback(callback_query: types.CallbackQuery):
    lang_code = callback_query.data.split("_")[1]
    user_langs[callback_query.from_user.id] = lang_code
    
    if lang_code == "ar":
        text = "تم تغيير اللغة إلى العربية بنجاح ✅"
    else:
        text = "Language changed to English successfully ✅"
        
    await callback_query.answer(text)
    
    # تحديث الرسالة لإزالة الأزرار بعد الاختيار
    await callback_query.message.edit_reply_markup(reply_markup=None)
    await callback_query.message.answer(text)


@dp.message()
async def handle_user_message(message: types.Message):
    if not message.text:
        lang = user_langs.get(message.from_user.id, "ar")
        await message.answer("أرسل نصاً عن الهدايا أو TON أو ماركت." if lang == "ar" else "Send a message about gifts, TON, or MRKT.")
        return

    user_id = message.from_user.id
    user_text = message.text.strip()
    lang = user_langs.get(user_id, "ar")

    if ADMIN_ID is not None and user_id == ADMIN_ID:
        if user_text == "db_status":
            await message.answer(get_db_status_report(), disable_web_page_preview=True)
            return
        if user_text == "sync_prices":
            if not os.path.exists(mrkt_session_path()):
                await message.answer("mrkt.session file not found.")
                return
            try:
                n = await sync_prices_from_mrkt()
                await message.answer(f"Synced {n} collections (Numbers: {_SYNC_STATUS['last_numbers_count']}).")
            except Exception as e:
                await message.answer(f"Sync failed: {e}")
            return

    quick_info = extract_quick_sell_query(user_text)
    if quick_info:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="فتح سوق MRKT" if lang == "ar" else "Open MRKT", url=DEFAULT_MARKET_URL)],
            [InlineKeyboardButton(text="تغيير اللغة 🌐" if lang == "ar" else "Change Language 🌐", callback_data="toggle_lang")]
        ])
        
        # للحصول على الأزرار الصحيحة في التبديل
        if lang == "ar":
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="فتح سوق MRKT", url=DEFAULT_MARKET_URL)],
                [InlineKeyboardButton(text="تغيير اللغة 🌐", callback_data="lang_en")]
            ])
        else:
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="Open MRKT", url=DEFAULT_MARKET_URL)],
                [InlineKeyboardButton(text="Change Language 🌐", callback_data="lang_ar")]
            ])

        exact_data = await fetch_exact_quick_sell_from_mrkt(quick_info)
        caption_txt = build_quick_sell_reply(exact_data, lang=lang)
        card_bytes = await render_quick_sell_card_image(exact_data)
        if card_bytes:
            await message.answer_photo(photo=BufferedInputFile(card_bytes, filename="quick_sell_card.jpg"), caption=caption_txt, parse_mode="HTML", reply_markup=kb)
        else:
            await message.answer(caption_txt, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)
        return

    nft_link = extract_nft_query(user_text)
    if nft_link:
        await message.answer(f'رابط الهدية: <a href="{nft_link}">فتح الهدية</a>' if lang == "ar" else f'Gift link: <a href="{nft_link}">Open Gift</a>', parse_mode="HTML", disable_web_page_preview=True)
        return

    if user_id not in user_conversations:
        user_conversations[user_id] = [{"role": "system", "content": EXPERT_SYSTEM_PROMPT}]

    db_snapshot = get_relevant_gifts_summary(user_text)
    user_conversations[user_id].append({"role": "user", "content": f"{user_text}\n\n[Database data: {db_snapshot}]"})
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
        
        if lang == "ar":
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="فتح سوق MRKT", url=DEFAULT_MARKET_URL)],
                [InlineKeyboardButton(text="تغيير اللغة 🌐", callback_data="lang_en")]
            ])
        else:
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="Open MRKT", url=DEFAULT_MARKET_URL)],
                [InlineKeyboardButton(text="Change Language 🌐", callback_data="lang_ar")]
            ])

        try:
            await message.answer(linked_reply, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)
        except Exception:
            await message.answer(re.sub(r"<[^>]+>", "", linked_reply), reply_markup=kb, disable_web_page_preview=True)
    except Exception as e:
        print(f"Groq/Bot error: {e}")
        await message.answer("حدث خطأ تقني مؤقت، حاول مجدداً." if lang == "ar" else "A temporary technical error occurred, please try again.")


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
