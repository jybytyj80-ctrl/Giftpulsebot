import asyncio
import base64
from datetime import datetime
import gzip
import importlib
import inspect
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
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from groq import Groq
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.functions.messages import RequestWebViewRequest
import amrkt
from amrkt import MarketClient

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_MODEL = os.getenv("GROQ_MODEL")
ADMIN_ID_ENV = os.getenv("ADMIN_ID", "").strip()
ADMIN_ID = int(ADMIN_ID_ENV) if ADMIN_ID_ENV.isdigit() else None

API_ID_ENV = (os.getenv("TG_API_ID") or os.getenv("API_ID") or "").strip()
API_HASH_ENV = (os.getenv("TG_API_HASH") or os.getenv("API_HASH") or "").strip()
TG_SESSION_STRING = os.getenv("TG_SESSION_STRING", "").strip()
MRKT_BOT_USERNAME = os.getenv("MRKT_BOT_USERNAME", "mrkt").strip().lstrip("@")
MRKT_WEB_URL = os.getenv("MRKT_WEB_URL", "https://t.me/mrkt/app").strip()
DB_PATH = os.getenv("DB_PATH", "gifts_market.db").strip()
API_PORT = int(os.getenv("PORT", "8080"))

# --- إعدادات مزامنة أسعار MRKT (مكتبة amrkt) ---
MRKT_SESSION_DIR = os.getenv("MRKT_SESSION_DIR", ".").strip()
MRKT_SESSION_B64 = os.getenv("MRKT_SESSION_B64", "").strip()
SYNC_EVERY = int(os.getenv("SYNC_EVERY_SECONDS", "300"))
FETCH_GIFT_NUMBERS = os.getenv("FETCH_GIFT_NUMBERS", "1").strip().lower() in (
    "1",
    "true",
    "yes",
)

DEVELOPER_USERNAME = "vvopnm8"
DEVELOPER_NAME = "عباس"
DEFAULT_MARKET_URL = "https://t.me/mrkt/app"

if not TELEGRAM_BOT_TOKEN or not GROQ_API_KEY:
  raise ValueError("Missing TELEGRAM_BOT_TOKEN or GROQ_API_KEY in Railway Variables")


def patch_amrkt_gift_collection_model():
  """يبحث في جميع وحدات مكتبة amrkt ويسمح بأن يكون floorPriceNanoTons قابلاً لـ None."""
  try:
    if hasattr(amrkt, "__path__"):
      for _, mod_name, _ in pkgutil.walk_packages(
          amrkt.__path__, amrkt.__name__ + "."
      ):
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
  print(f"تم ترقيع {patched_count} نموذج داخل مكتبة amrkt لدعم الأسعار الفارغة.")


patch_amrkt_gift_collection_model()

client = Groq(api_key=GROQ_API_KEY)
bot = Bot(token=TELEGRAM_BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

_CACHED_MODEL: Optional[str] = None
_LOGGED_AMRKT_METHODS = False
_GIFTS_CACHE = {
    "rows": [],
    "candidates": [],
    "updated_at": 0.0,
}
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
    (
        "آيس كريم مخروطي",
        "IceCreamCone",
        1.2,
        300,
        1.2,
        1.1,
        None,
        "Standard",
        "Classic",
        "None",
        None,
    ),
    (
        "نودلز",
        "InstantRamen",
        1.8,
        438,
        1.8,
        1.7,
        None,
        "Standard",
        "Classic",
        "None",
        None,
    ),
    (
        "آيس كريم شوكولاتة",
        "ChocolateIceCream",
        2.0,
        449,
        2.0,
        1.9,
        None,
        "Standard",
        "Classic",
        "None",
        None,
    ),
    (
        "شعلة",
        "Torch",
        2.5,
        460,
        2.5,
        2.4,
        None,
        "Standard",
        "Classic",
        "None",
        None,
    ),
    (
        "تمثال الحرية",
        "LibertyFigure",
        3.5,
        480,
        3.5,
        3.3,
        None,
        "Standard",
        "Classic",
        "None",
        None,
    ),
    (
        "كلب الهيب هوب",
        "HipHopDog",
        4.0,
        480,
        4.0,
        3.8,
        None,
        "Standard",
        "Classic",
        "None",
        None,
    ),
    (
        "فانوس",
        "GenieLamp",
        4.5,
        480,
        4.5,
        4.3,
        53,
        "Blueprint",
        "Lavender",
        "Crusader",
        None,
    ),
    (
        "مصاصة ملونة",
        "LolPop",
        5.0,
        480,
        5.0,
        4.8,
        None,
        "Standard",
        "Classic",
        "None",
        None,
    ),
    (
        "عوامة فلامنجو",
        "FlamingoFloat",
        6.5,
        480,
        6.5,
        6.2,
        None,
        "Standard",
        "Classic",
        "None",
        None,
    ),
    (
        "حقيبة تيليجرام",
        "TelegramBag",
        8.0,
        480,
        8.0,
        7.6,
        None,
        "Standard",
        "Classic",
        "None",
        None,
    ),
    (
        "قناع",
        "Mask",
        13.8,
        843,
        13.81,
        13.30,
        None,
        "Standard",
        "Classic",
        "None",
        None,
    ),
]

EXPERT_SYSTEM_PROMPT = """
You are "Giftpulse", an expert advisor on Telegram Gifts, Telegram Stars, collectible attributes (Model, Backdrop, Symbol), and trading in the MRKT marketplace.

STRICT RULES:
1. Keep replies VERY SHORT (maximum 2 concise lines). Include the exact price/floor/quick/model/number details provided in the database entries when answering about a collection.
2. NEVER invent NFT gift numbers (#...) if a gift number is not explicitly given in the database entry. Only mention a number if it is explicitly shown in the database entry.
3. NEVER say "لا يمكنني عرض روابط" or "لا أستطيع إرسال روابط". Gift names in replies are automatically turned into clickable links by the bot, and there is a "فتح سوق MRKT" button below.
4. Never use ** or markdown asterisks. Plain text only.
5. Prices in MRKT are in TON (not thousands of Stars). Floor = floor price; Quick = instant sale price (slightly below floor).
6. Answer ONLY about Telegram Gifts, Telegram Stars, collectibles, and MRKT. Outside scope: reply in one short line:
   هذا البوت مخصص لهدايا تيليجرام والنجوم وسوق MRKT.
"""

user_conversations = {}


def init_db():
  conn = sqlite3.connect(DB_PATH)
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
  count = cur.fetchone()[0]
  if count == 0:
    cur.executemany(
        """
            INSERT INTO gifts (
                title, slug, ton, stars, floor_price, quick_price,
                gift_number, model, backdrop, symbol, custom_url
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        INITIAL_GIFTS,
    )
  conn.commit()
  conn.close()
  refresh_gifts_cache(force=True)


def clean_text(text: str) -> str:
  return text.replace("**", "").strip()


def normalize_slug(name: str) -> str:
  words = re.findall(r"[A-Za-z0-9]+", name)
  if not words:
    return ""
  return "".join(w.capitalize() for w in words)


def resolve_slug(title: str, explicit_slug=None) -> str:
  if explicit_slug and explicit_slug.strip():
    cleaned = normalize_slug(explicit_slug)
    if cleaned:
      return cleaned
  if title in AR_TO_SLUG:
    return AR_TO_SLUG[title]
  return normalize_slug(title)


def build_auto_gift_url(
    title: str, gift_number=None, explicit_url=None, slug=None
) -> str:
  """يبني رابط NFT إذا وُجد رقم هدية مؤكد أو رابط NFT صريح؛ وإلا يوجّه إلى سوق MRKT."""
  if explicit_url and explicit_url.strip():
    clean_url = explicit_url.strip()
    if re.search(
        r"https?://t\.me/nft/[A-Za-z0-9_]+-\d+", clean_url, re.IGNORECASE
    ):
      return clean_url

  if gift_number is not None:
    num_str = str(gift_number).strip().lstrip("#")
    if num_str.isdigit() and int(num_str) > 0:
      resolved = resolve_slug(title, slug)
      if resolved:
        return f"https://t.me/nft/{resolved}-{num_str}"

  return DEFAULT_MARKET_URL


def build_nft_link(
    title: str, gift_number=None, explicit_url=None, slug=None
) -> str:
  return build_auto_gift_url(
      title=title,
      gift_number=gift_number,
      explicit_url=explicit_url,
      slug=slug,
  )


def _query_all_gifts_from_db():
  conn = sqlite3.connect(DB_PATH)
  conn.row_factory = sqlite3.Row
  cur = conn.cursor()
  cur.execute("""
        SELECT title, slug, ton, stars, floor_price, quick_price,
               gift_number, model, backdrop, symbol, custom_url
        FROM gifts
        ORDER BY ton DESC
    """)
  rows = [dict(r) for r in cur.fetchall()]
  conn.close()
  return rows


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
    if not gift_url:
      continue
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
  matched = [
      r for r in rows if r.get("ton") is not None and float(r["ton"]) <= max_ton
  ]
  return matched[:limit]


def _format_gift_entry(r: dict) -> str:
  ton_val = r.get("ton")
  ton_str = f"{float(ton_val):g} TON" if ton_val is not None else "غير محدد"
  s = f"{r['title']}: السعر {ton_str}"
  if r.get("gift_number") is not None and int(r["gift_number"]) > 0:
    s += f" | رقم #{r['gift_number']}"
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
  """يرسل ملخصاً من قاعدة البيانات للكولكشنز المطابقة أو الأعلى سعراً."""
  rows = get_all_gifts_rows()
  if not rows:
    return "لا توجد هدايا مسجلة."

  q_low = user_text.lower()
  matched = []
  for r in rows:
    title = (r.get("title") or "").strip()
    slug = (r.get("slug") or "").strip()
    if title and title.lower() in q_low:
      matched.append(r)
    elif slug and len(slug) >= 2 and slug.lower() in q_low:
      matched.append(r)

  budget_match = re.search(r"(\d+(?:\.\d+)?)\s*(?:تون|ton)", q_low)
  if budget_match and not matched:
    budget_val = float(budget_match.group(1))
    matched = [
        r for r in rows if r.get("ton") is not None and float(r["ton"]) <= budget_val
    ][:8]

  target_rows = matched[:8] if matched else rows[:15]
  return " || ".join(_format_gift_entry(r) for r in target_rows)


def get_db_status_report() -> str:
  """يعرض تقريراً سريعاً للأدمن عن حالة قاعدة البيانات وآخر مزامنة."""
  rows = refresh_gifts_cache(force=True)
  total = len(rows)
  with_nums = sum(
      1 for r in rows if r.get("gift_number") is not None and int(r["gift_number"]) > 0
  )
  top_names = ", ".join(r.get("title", "") for r in rows[:5] if r.get("title"))
  last_sync = _SYNC_STATUS["last_sync_at"] or "لم تتم مزامنة بعد في هذه الدورة"
  last_count = _SYNC_STATUS["last_synced_count"]
  last_nums = _SYNC_STATUS["last_numbers_count"]
  last_err = _SYNC_STATUS["last_error"]
  lines = [
      "حالة قاعدة البيانات والمزامنة:",
      f"- إجمالي الهدايا/الكولكشنز المحفوظة: {total} (أرقام مؤكدة: {with_nums})",
      f"- آخر مزامنة: {last_sync} (كولكشنز: {last_count} | أرقام مؤكدة: {last_nums})",
      f"- الفاصل الزمني للمزامنة: {SYNC_EVERY} ثانية (~{SYNC_EVERY // 60} دقيقة)",
      f"- أبرز الأسماء المسجلة: {top_names or 'لا يوجد'}",
  ]
  if last_err:
    lines.append(f"- آخر خطأ مسجل: {last_err}")
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
  conn = sqlite3.connect(DB_PATH)
  conn.row_factory = sqlite3.Row
  cur = conn.cursor()
  cur.execute("SELECT * FROM gifts WHERE LOWER(title) = LOWER(?)", (title,))
  existing = cur.fetchone()

  resolved_slug = resolve_slug(
      title, slug or (existing["slug"] if existing else None)
  )
  effective_num = (
      num_val if num_val is not None else (existing["gift_number"] if existing else None)
  )
  effective_url = (
      url_val if url_val is not None else (existing["custom_url"] if existing else None)
  )
  auto_url = build_auto_gift_url(
      title=title,
      gift_number=effective_num,
      explicit_url=effective_url,
      slug=resolved_slug,
  )

  if existing:
    new_ton = ton_val if ton_val is not None else existing["ton"]
    new_stars = stars_val if stars_val is not None else existing["stars"]
    new_floor = floor_val if floor_val is not None else existing["floor_price"]
    new_quick = quick_val if quick_val is not None else existing["quick_price"]
    new_num = effective_num
    new_model = model_val if model_val is not None else existing["model"]
    new_backdrop = (
        backdrop_val if backdrop_val is not None else existing["backdrop"]
    )
    new_symbol = symbol_val if symbol_val is not None else existing["symbol"]
    new_url = auto_url

    cur.execute(
        """
            UPDATE gifts
            SET slug = ?, ton = ?, stars = ?, floor_price = ?, quick_price = ?,
                gift_number = ?, model = ?, backdrop = ?, symbol = ?, custom_url = ?
            WHERE id = ?
        """,
        (
            resolved_slug,
            new_ton,
            new_stars,
            new_floor,
            new_quick,
            new_num,
            new_model,
            new_backdrop,
            new_symbol,
            new_url,
            existing["id"],
        ),
    )
  else:
    cur.execute(
        """
            INSERT INTO gifts (
                title, slug, ton, stars, floor_price, quick_price,
                gift_number, model, backdrop, symbol, custom_url
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
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
        ),
    )

  conn.commit()
  conn.close()
  refresh_gifts_cache(force=True)


# ===================== مزامنة أسعار MRKT (amrkt) =====================
def mrkt_session_path() -> str:
  return os.path.join(MRKT_SESSION_DIR, "mrkt.session")


def restore_mrkt_session_from_env():
  """إذا كان ملف الجلسة غير موجود وتوجد نسخة base64 (عادية أو مضغوطة بـ gzip) في المتغيرات، نعيد إنشاءه."""
  path = mrkt_session_path()
  if not MRKT_SESSION_B64:
    return
  try:
    os.makedirs(MRKT_SESSION_DIR, exist_ok=True)
    cleaned_b64 = "".join(MRKT_SESSION_B64.split())
    missing_padding = len(cleaned_b64) % 4
    if missing_padding:
      cleaned_b64 += "=" * (4 - missing_padding)

    raw_bytes = base64.b64decode(cleaned_b64)
    if raw_bytes[:2] == b"\x1f\x8b":
      raw_bytes = gzip.decompress(raw_bytes)

    with open(path, "wb") as f:
      f.write(raw_bytes)
    print("تم استرجاع ملف جلسة MRKT من المتغيرات.")
  except Exception as e:
    print(f"فشل استرجاع جلسة MRKT: {e}")


def save_collection_price(name, title, floor_ton, gift_number=None, conn=None):
  """يحفظ/يحدّث سعر الكولكشن ورقم الهدية ويبني رابطها بأمان."""
  clean_title = (title or name or "").strip()
  slug = normalize_slug(name or "") or normalize_slug(clean_title)
  if not slug or floor_ton is None or floor_ton <= 0:
    return
  display_title = clean_title or slug

  own_conn = conn is None
  if own_conn:
    conn = sqlite3.connect(DB_PATH)
  cur = conn.cursor()
  cur.execute(
      "SELECT id, gift_number, custom_url FROM gifts WHERE LOWER(slug) = LOWER(?)",
      (slug,),
  )
  row = cur.fetchone()
  if row:
    effective_num = gift_number if gift_number is not None else row[1]
    auto_url = build_auto_gift_url(
        title=display_title,
        gift_number=effective_num,
        explicit_url=row[2],
        slug=slug,
    )
    cur.execute(
        "UPDATE gifts SET ton = ?, floor_price = ?, gift_number = ?,"
        " slug = COALESCE(slug, ?), custom_url = ? WHERE id = ?",
        (floor_ton, floor_ton, effective_num, slug, auto_url, row[0]),
    )
  else:
    auto_url = build_auto_gift_url(
        title=display_title, gift_number=gift_number, slug=slug
    )
    cur.execute(
        "INSERT OR IGNORE INTO gifts (title, slug, ton, floor_price,"
        " gift_number, custom_url) VALUES (?, ?, ?, ?, ?, ?)",
        (display_title, slug, floor_ton, floor_ton, gift_number, auto_url),
    )
  if own_conn:
    conn.commit()
    conn.close()


def _parse_gift_slug_and_number(gift_obj) -> Optional[int]:
  """يستخرج رقم الهدية الحقيقي من حقل name مثل ResistanceDog-18222."""
  if gift_obj is None:
    return None
  raw_name = (
      gift_obj.get("name")
      if isinstance(gift_obj, dict)
      else getattr(gift_obj, "name", None)
  )
  if isinstance(raw_name, str) and raw_name.strip():
    m = re.search(r"-(\d{1,9})$", raw_name.strip())
    if m:
      num = int(m.group(1))
      if num > 0:
        return num
  return None


async def _fetch_gift_number_via_search(mc, col) -> Optional[int]:
  """يستخدم mc.search_gifts لجلب هدية حقيقية للكولكشن واستخراج رقمها من name."""
  if not FETCH_GIFT_NUMBERS:
    return None

  fn = getattr(mc, "search_gifts", None)
  if not callable(fn):
    return None

  col_name = (getattr(col, "name", "") or "").strip()
  col_title = (getattr(col, "title", "") or "").strip()
  col_slug = normalize_slug(col_name or col_title)

  try:
    sig = inspect.signature(fn)
    params = sig.parameters
  except Exception:
    params = {}

  queries = [q for q in (col_name, col_title, col_slug) if q]
  seen_q = set()
  for q in queries:
    if q.lower() in seen_q:
      continue
    seen_q.add(q.lower())

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
      res = await fn(**kwargs) if kwargs else await fn(q)
      items = []
      if isinstance(res, list):
        items = res
      elif isinstance(res, dict):
        for k in ("items", "gifts", "results", "data"):
          if isinstance(res.get(k), list):
            items = res[k]
            break
      else:
        for attr in ("items", "gifts", "results", "data"):
          val = getattr(res, attr, None)
          if isinstance(val, list):
            items = val
            break

      for item in items:
        g_num = _parse_gift_slug_and_number(item)
        if g_num is not None:
          return g_num
    except Exception:
      continue

  return None


async def sync_prices_from_mrkt() -> int:
  global _LOGGED_AMRKT_METHODS
  if not (API_ID_ENV.isdigit() and API_HASH_ENV):
    raise RuntimeError("TG_API_ID / TG_API_HASH غير مكتملة.")
  try:
    async with MarketClient(
        api_id=int(API_ID_ENV),
        api_hash=API_HASH_ENV,
        session_name="mrkt",
        workdir=MRKT_SESSION_DIR,
    ) as mc:
      if not _LOGGED_AMRKT_METHODS:
        public_methods = [
            m
            for m in dir(mc)
            if not m.startswith("_") and callable(getattr(mc, m, None))
        ]
        print("دوال MarketClient المتاحة:", public_methods)
        _LOGGED_AMRKT_METHODS = True

      try:
        cols = await mc.get_collections()
      except Exception as e:
        print(f"تحذير أثناء قراءة بعض الكولكشنز في get_collections: {e}")
        _SYNC_STATUS["last_error"] = str(e)
        return 0

      conn = sqlite3.connect(DB_PATH)
      count = 0
      numbers_found = 0
      try:
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
            save_collection_price(
                getattr(c, "name", ""),
                getattr(c, "title", ""),
                floor_ton,
                gift_number=gift_num,
                conn=conn,
            )
            count += 1
            if FETCH_GIFT_NUMBERS:
              await asyncio.sleep(0.08)
        conn.commit()
      finally:
        conn.close()

    refresh_gifts_cache(force=True)
    confirmed_nums = sum(
        1
        for r in _GIFTS_CACHE["rows"]
        if r.get("gift_number") is not None and int(r["gift_number"]) > 0
    )
    _SYNC_STATUS["last_sync_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _SYNC_STATUS["last_synced_count"] = count
    _SYNC_STATUS["last_numbers_count"] = max(numbers_found, confirmed_nums)
    _SYNC_STATUS["last_error"] = None
    return count
  except Exception as e:
    _SYNC_STATUS["last_error"] = str(e)
    raise


async def mrkt_sync_loop():
  while True:
    try:
      n = await sync_prices_from_mrkt()
      print(
          f"تمت مزامنة {n} كولكشن من MRKT (أرقام هدايا: {_SYNC_STATUS['last_numbers_count']})"
      )
    except Exception as e:
      print(f"فشل مزامنة MRKT: {e}")
    await asyncio.sleep(SYNC_EVERY)


async def inspect_amrkt_capabilities() -> str:
  if not (API_ID_ENV.isdigit() and API_HASH_ENV):
    return "متغيرات TG_API_ID / TG_API_HASH غير مكتملة."

  try:
    async with MarketClient(
        api_id=int(API_ID_ENV),
        api_hash=API_HASH_ENV,
        session_name="mrkt",
        workdir=MRKT_SESSION_DIR,
    ) as mc:
      methods = [
          m
          for m in dir(mc)
          if not m.startswith("_") and callable(getattr(mc, m, None))
      ]
      cols = await mc.get_collections()
      sample_fields = []
      if cols:
        first = cols[0]
        if hasattr(first, "model_dump"):
          sample_fields = list(first.model_dump().keys())
        elif hasattr(first, "__dict__"):
          sample_fields = list(first.__dict__.keys())

      search_sample = ""
      if hasattr(mc, "search_gifts"):
        try:
          g_res = await mc.search_gifts(limit=2)
          items = getattr(g_res, "items", None) or []
          if items:
            search_sample = f"search_gifts -> أول هدية: {getattr(items[0], 'name', items[0])}"
        except Exception as e:
          search_sample = f"search_gifts -> خطأ: {e}"

      lines = [
          f"دوال MarketClient ({len(methods)}): {', '.join(methods)}",
          f"حقول أول كولكشن: {', '.join(sample_fields) or 'غير ظاهرة'}",
          search_sample or "لا توجد عينة من search_gifts",
      ]
      return "\n".join(lines)[:3800]
  except Exception as e:
    return f"فشل فحص amrkt: {e}"


def save_feed_items(items):
  if not items:
    return
  conn = sqlite3.connect(DB_PATH)
  cur = conn.cursor()
  cur.execute("DELETE FROM mrkt_feed")
  for source_type, content, url in items:
    cur.execute(
        "INSERT INTO mrkt_feed (source_type, content, url) VALUES (?, ?, ?)",
        (source_type, content, url),
    )
  conn.commit()
  conn.close()


# --- API Endpoints (aiohttp) ---
async def api_health(request: web.Request) -> web.Response:
  return web.json_response({"status": "ok", "service": "Giftpulse MRKT API"})


async def api_get_gifts(request: web.Request) -> web.Response:
  max_ton_param = request.query.get("max_ton")
  if max_ton_param is not None:
    try:
      max_ton_val = float(max_ton_param)
      rows = get_gifts_within_budget(max_ton_val, limit=50)
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

  items = (
      payload.get("gifts")
      if isinstance(payload, dict) and "gifts" in payload
      else [payload]
  )
  if not isinstance(items, list) or not items:
    return web.json_response(
        {"error": "Provide a gift object or a 'gifts' list"}, status=400
    )

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


async def start_api_server():
  app = web.Application()
  app.router.add_get("/api/health", api_health)
  app.router.add_get("/api/gifts", api_get_gifts)
  app.router.add_post("/api/gifts", api_upsert_gifts)

  runner = web.AppRunner(app)
  await runner.setup()
  site = web.TCPSite(runner, "0.0.0.0", API_PORT)
  await site.start()
  print(f"Giftpulse API running on port {API_PORT}")


def get_active_model() -> str:
  global _CACHED_MODEL
  if _CACHED_MODEL:
    return _CACHED_MODEL

  if GROQ_MODEL and GROQ_MODEL.strip():
    _CACHED_MODEL = GROQ_MODEL.strip()
    return _CACHED_MODEL

  models = client.models.list().data
  model_ids = [m.id for m in models]

  preferred_order = [
      "qwen/qwen3.8-27b",
      "allam-2-7b",
      "openai/gpt-oss-120b",
      "openai/gpt-oss-20b",
  ]
  for preferred in preferred_order:
    if preferred in model_ids:
      _CACHED_MODEL = preferred
      return _CACHED_MODEL

  chat_candidates = [
      mid
      for mid in model_ids
      if not any(bad in mid.lower() for bad in ("whisper", "orpheus", "guard"))
  ]
  if chat_candidates:
    _CACHED_MODEL = chat_candidates[0]
    return _CACHED_MODEL

  raise RuntimeError(f"No suitable chat model found in: {model_ids}")


async def sync_mrkt_via_secondary_account() -> str:
  if not (API_ID_ENV.isdigit() and API_HASH_ENV and TG_SESSION_STRING):
    return (
        "متغيرات الجلسة (TG_API_ID / TG_API_HASH / TG_SESSION_STRING) غير"
        " مكتملة."
    )

  try:
    async with TelegramClient(
        StringSession(TG_SESSION_STRING),
        int(API_ID_ENV),
        API_HASH_ENV,
    ) as tg_client:
      me = await tg_client.get_me()
      peer = await tg_client.get_input_entity(MRKT_BOT_USERNAME)
      msgs = await tg_client.get_messages(peer, limit=15)

      collected_feed = []
      nft_links_found = 0
      app_links_found = 0

      for m in msgs:
        text = (getattr(m, "message", "") or "").strip()
        urls = re.findall(r"https?://\S+", text)

        for u in urls:
          clean_u = u.rstrip(").,]")
          collected_feed.append(("message_url", text[:160], clean_u))
          if "t.me/nft/" in clean_u:
            nft_links_found += 1
          elif "t.me/mrkt" in clean_u:
            app_links_found += 1

        if getattr(m, "buttons", None):
          for row in m.buttons:
            for btn in row:
              btn_text = (getattr(btn, "text", "") or "").strip()
              btn_url = (getattr(btn, "url", "") or "").strip()
              if btn_url:
                collected_feed.append(("button_url", btn_text, btn_url))
                if "t.me/nft/" in btn_url:
                  nft_links_found += 1
                elif "t.me/mrkt" in btn_url:
                  app_links_found += 1

        for nft_match in re.finditer(
            r"https?://t\.me/nft/([A-Za-z0-9_]+)-(\d+)", text
        ):
          slug_part, num_part = nft_match.group(1), nft_match.group(2)
          upsert_gift_in_db(
              title=slug_part,
              slug=slug_part,
              num_val=int(num_part),
              url_val=nft_match.group(0),
          )

      save_feed_items(collected_feed)
      return (
          f"تمت مزامنة الحساب الثانوي (@{me.username or me.id}) مع"
          f" @{MRKT_BOT_USERNAME}: حُفظ {len(collected_feed)} عنصر رابط/زر في"
          f" قاعدة البيانات (روابط NFT: {nft_links_found}، روابط MRKT:"
          f" {app_links_found})."
      )
  except Exception as e:
    return f"فشل مزامنة الحساب الثانوي: {e}"


async def test_mrkt_session() -> str:
  if not (API_ID_ENV.isdigit() and API_HASH_ENV and TG_SESSION_STRING):
    return (
        "متغيرات الجلسة (TG_API_ID / TG_API_HASH / TG_SESSION_STRING) غير"
        " مكتملة."
    )

  try:
    async with TelegramClient(
        StringSession(TG_SESSION_STRING),
        int(API_ID_ENV),
        API_HASH_ENV,
    ) as tg_client:
      me = await tg_client.get_me()
      peer = await tg_client.get_input_entity(MRKT_BOT_USERNAME)

      web_url = MRKT_WEB_URL.strip()
      if web_url and not web_url.startswith(("https://t.me/", "http://t.me/")):
        webview = await tg_client(
            RequestWebViewRequest(
                peer=peer,
                bot=peer,
                platform="android",
                url=web_url,
            )
        )
        has_url = bool(getattr(webview, "url", None))
        return (
            f"اتصال Telethon ناجح بالحساب (@{me.username or me.id}) وتم فتح"
            f" WebView: {'نعم' if has_url else 'لا'}."
        )

      msgs = await tg_client.get_messages(peer, limit=5)
      return (
          f"اتصال Telethon ناجح بالحساب (@{me.username or me.id}) والوصول"
          f" إلى @{MRKT_BOT_USERNAME} سليم (آخر رسائل محفوظة: {len(msgs)})."
      )
  except Exception as e:
    return f"فشل اتصال Telethon بـ MRKT: {e}"


async def inspect_mrkt_content() -> str:
  if not (API_ID_ENV.isdigit() and API_HASH_ENV and TG_SESSION_STRING):
    return "متغيرات الجلسة غير مكتملة."

  try:
    async with TelegramClient(
        StringSession(TG_SESSION_STRING),
        int(API_ID_ENV),
        API_HASH_ENV,
    ) as tg_client:
      peer = await tg_client.get_input_entity(MRKT_BOT_USERNAME)
      msgs = await tg_client.get_messages(peer, limit=8)

      found_urls = []
      buttons_info = []

      for m in msgs:
        text = getattr(m, "message", "") or ""
        found_urls.extend(re.findall(r"https?://\S+", text))

        if getattr(m, "buttons", None):
          for row in m.buttons:
            for btn in row:
              btn_text = getattr(btn, "text", "") or ""
              btn_url = getattr(btn, "url", "") or ""
              if btn_url:
                found_urls.append(btn_url)
              buttons_info.append(f"{btn_text} -> {btn_url or 'زر داخلي'}")

      unique_urls = list(dict.fromkeys(found_urls))
      lines = [f"فحص @{MRKT_BOT_USERNAME} (آخر {len(msgs)} رسائل):"]
      lines.append(
          "الروابط الظاهرة:"
          f" {', '.join(unique_urls[:6]) if unique_urls else 'لا توجد روابط مباشرة'}"
      )
      lines.append(
          "الأزرار الظاهرة:"
          f" {', '.join(buttons_info[:6]) if buttons_info else 'لا توجد أزرار'}"
      )
      return "\n".join(lines)
  except Exception as e:
    return f"خطأ أثناء فحص محتوى MRKT: {e}"


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

  eng_match = re.search(
      r"([A-Za-z][A-Za-z\s\-]{1,40}?)\s*#?\s*\d{1,9}\b", text
  )
  if eng_match:
    slug = normalize_slug(eng_match.group(1))
    if slug:
      return f"https://t.me/nft/{slug}-{gift_num}"

  return None


def handle_admin_update(user_text: str):
  raw = user_text.split(":", 1)[1].strip()
  parts = [p.strip() for p in raw.split("|")]
  if not parts or not parts[0]:
    return (
        "صيغة التحديث: تحديث: اسم الهدية | تون 5.0 | فلور 5.2 | كويك 4.9 | رقم"
        " 53 | موديل Blueprint | خلفية Lavender | رمز Crusader | رابط https://..."
    )

  title = parts[0]
  ton_val = None
  stars_val = None
  floor_val = None
  quick_val = None
  url_val = None
  num_val = None
  slug_val = None
  model_val = None
  backdrop_val = None
  symbol_val = None

  for p in parts[1:]:
    low = p.lower()
    url_match = re.search(r"(https?://\S+)", p)
    if url_match and (
        "رابط" in p
        or "url" in low
        or "link" in low
        or url_match.group(1).startswith("http")
    ):
      url_val = url_match.group(1).strip()
      continue

    if "slug" in low or "انجليزي" in p or "إنجليزي" in p:
      slug_candidate = normalize_slug(p.split(":", 1)[-1] if ":" in p else p)
      if slug_candidate:
        slug_val = slug_candidate
        continue

    if "موديل" in p or "نموذج" in p or "model" in low:
      model_val = (
          p.split(":", 1)[-1].replace("موديل", "").replace("نموذج", "").strip()
      )
      continue

    if "خلفية" in p or "backdrop" in low:
      backdrop_val = p.split(":", 1)[-1].replace("خلفية", "").strip()
      continue

    if "رمز" in p or "symbol" in low:
      symbol_val = p.split(":", 1)[-1].replace("رمز", "").strip()
      continue

    num_match = re.search(r"(\d+(?:\.\d+)?)", p)
    if not num_match:
      continue
    val = float(num_match.group(1))
    if "رقم" in p or "number" in low or "#" in p:
      num_val = int(val)
    elif (
        "فلور" in low
        or "أرضية" in low
        or "ارضية" in low
        or "floor" in low
    ):
      floor_val = val
    elif "كويك" in low or "quick" in low:
      quick_val = val
    elif "نجم" in low or "star" in low:
      stars_val = int(val)
    elif (
        "تون" in low
        or "ton" in low
        or re.search(r"\b\d+(?:\.\d+)?\s*t\b", low)
    ):
      ton_val = val

  upsert_gift_in_db(
      title=title,
      slug=slug_val or resolve_slug(title),
      ton_val=ton_val,
      stars_val=stars_val,
      floor_val=floor_val,
      quick_val=quick_val,
      num_val=num_val,
      model_val=model_val,
      backdrop_val=backdrop_val,
      symbol_val=symbol_val,
      url_val=url_val,
  )
  return f"تم حفظ وتحديث بيانات {title} في قاعدة البيانات."


def linkify_gifts_in_reply(ai_reply: str) -> str:
  """يحوّل تلقائياً جميع أسماء الكولكشنز/الهدايا خارج وسوم HTML إلى روابط قابلة للضغط."""
  refresh_gifts_cache(force=False)
  candidates = _GIFTS_CACHE["candidates"]
  token_counter = 0
  link_tokens = {}

  def _linkify_plain_segment(segment: str) -> str:
    nonlocal token_counter
    out = segment
    for label, gift_url in candidates:
      pattern = re.compile(
          r"(?<![A-Za-z0-9_])" + re.escape(label) + r"(?![A-Za-z0-9_])",
          re.IGNORECASE,
      )
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
    if not part:
      continue
    if part.lower().startswith("<a "):
      continue
    parts[idx] = _linkify_plain_segment(part)

  result = "".join(parts)
  for token, anchor in link_tokens.items():
    result = result.replace(token, anchor)
  return result


@dp.message(lambda message: message.text and message.text.startswith("/start"))
async def cmd_start(message: types.Message):
  user_conversations.pop(message.from_user.id, None)
  start_text = (
      "اهلاً بك عزيزي معك البوت Giftpulse مختص بهدايا تيليجرام ومايدور حول"
      " تيليجرام فقط.\n"
      f'المطور: <a href="https://t.me/{DEVELOPER_USERNAME}">{DEVELOPER_NAME}</a>'
  )
  await message.answer(
      start_text, parse_mode="HTML", disable_web_page_preview=True
  )


@dp.message()
async def handle_user_message(message: types.Message):
  if not message.text:
    await message.answer("أرسل نصاً عن الهدايا أو TON أو سوق MRKT.")
    return

  user_id = message.from_user.id
  user_text = message.text.strip()

  if user_text == "حالة_القاعدة" and ADMIN_ID is not None and user_id == ADMIN_ID:
    await message.answer(get_db_status_report(), disable_web_page_preview=True)
    return

  if user_text == "مزامنة_الحساب" and ADMIN_ID is not None and user_id == ADMIN_ID:
    sync_msg = await sync_mrkt_via_secondary_account()
    await message.answer(sync_msg, disable_web_page_preview=True)
    return

  if user_text == "مزامنة_الاسعار" and ADMIN_ID is not None and user_id == ADMIN_ID:
    if not os.path.exists(mrkt_session_path()):
      await message.answer("ملف جلسة MRKT (mrkt.session) غير موجود بعد.")
      return
    try:
      n = await sync_prices_from_mrkt()
      await message.answer(
          f"تمت مزامنة {n} كولكشن من MRKT (أرقام هدايا:"
          f" {_SYNC_STATUS['last_numbers_count']})."
      )
    except Exception as e:
      await message.answer(f"فشل المزامنة: {e}")
    return

  if user_text == "فحص_الجلسة" and ADMIN_ID is not None and user_id == ADMIN_ID:
    status_msg = await test_mrkt_session()
    await message.answer(status_msg, disable_web_page_preview=True)
    return

  if user_text == "فحص_ماركت" and ADMIN_ID is not None and user_id == ADMIN_ID:
    info_msg = await inspect_mrkt_content()
    await message.answer(info_msg, disable_web_page_preview=True)
    return

  if user_text == "فحص_amrkt" and ADMIN_ID is not None and user_id == ADMIN_ID:
    info_msg = await inspect_amrkt_capabilities()
    await message.answer(info_msg, disable_web_page_preview=True)
    return

  if (
      user_text.startswith("تحديث:")
      and ADMIN_ID is not None
      and user_id == ADMIN_ID
  ):
    msg = handle_admin_update(user_text)
    await message.answer(msg, disable_web_page_preview=True)
    return

  nft_link = extract_nft_query(user_text)
  if nft_link:
    await message.answer(
        f'رابط الهدية في تيليجرام: <a href="{nft_link}">فتح الهدية</a>',
        parse_mode="HTML",
        disable_web_page_preview=True,
    )
    return

  if user_id not in user_conversations:
    user_conversations[user_id] = [
        {"role": "system", "content": EXPERT_SYSTEM_PROMPT}
    ]

  db_snapshot = get_relevant_gifts_summary(user_text)
  enriched_prompt = f"{user_text}\n\n[بيانات قاعدة البيانات: {db_snapshot}]"

  user_conversations[user_id].append({"role": "user", "content": enriched_prompt})
  if len(user_conversations[user_id]) > 5:
    user_conversations[user_id] = [user_conversations[user_id][0]] + user_conversations[
        user_id
    ][-4:]

  try:
    model_to_use = get_active_model()
    response = await asyncio.to_thread(
        lambda: client.chat.completions.create(
            model=model_to_use,
            messages=user_conversations[user_id],
            temperature=0.1,
            max_tokens=120,
        )
    )
    ai_reply = clean_text(response.choices[0].message.content)
    user_conversations[user_id].append({"role": "assistant", "content": ai_reply})

    linked_reply = linkify_gifts_in_reply(ai_reply)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="فتح سوق MRKT", url=DEFAULT_MARKET_URL)],
        ]
    )
    try:
      await message.answer(
          linked_reply,
          parse_mode="HTML",
          reply_markup=kb,
          disable_web_page_preview=True,
      )
    except Exception as html_err:
      print(f"HTML parse fallback: {html_err}")
      plain_fallback = re.sub(r"<[^>]+>", "", linked_reply)
      await message.answer(
          plain_fallback, reply_markup=kb, disable_web_page_preview=True
      )
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

  # تشغيل مزامنة الأسعار فقط إذا كان ملف جلسة MRKT موجوداً (حتى لا يتعطل البوت بانتظار كود الدخول)
  restore_mrkt_session_from_env()
  if API_ID_ENV.isdigit() and API_HASH_ENV and os.path.exists(mrkt_session_path()):
    asyncio.create_task(mrkt_sync_loop())
    print("مزامنة أسعار MRKT تعمل في الخلفية.")
  else:
    print("مزامنة أسعار MRKT متوقفة: ملف الجلسة mrkt.session غير موجود.")

  print("Giftpulse Bot is starting...")
  await bot.delete_webhook(drop_pending_updates=True)
  await dp.start_polling(bot)


if __name__ == "__main__":
  asyncio.run(main())
