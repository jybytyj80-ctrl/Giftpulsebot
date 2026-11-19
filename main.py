import asyncio
import os
import re
import sqlite3
from aiogram import Bot, Dispatcher, types
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from groq import Groq
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.functions.messages import RequestWebViewRequest

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

DEVELOPER_USERNAME = "vvopnm8"
DEVELOPER_NAME = "عباس"
DEFAULT_MARKET_URL = "https://t.me/mrkt/app"

if not TELEGRAM_BOT_TOKEN or not GROQ_API_KEY:
    raise ValueError("Missing TELEGRAM_BOT_TOKEN or GROQ_API_KEY in Railway Variables")

client = Groq(api_key=GROQ_API_KEY)
bot = Bot(token=TELEGRAM_BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

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
    ("آيس كريم مخروطي", "IceCreamCone", 1.2, 300, 1.2, 1.1, None, None),
    ("نودلز", "InstantRamen", 1.8, 438, 1.8, 1.7, None, None),
    ("آيس كريم شوكولاتة", "ChocolateIceCream", 2.0, 449, 2.0, 1.9, None, None),
    ("شعلة", "Torch", 2.5, 460, 2.5, 2.4, None, None),
    ("تمثال الحرية", "LibertyFigure", 3.5, 480, 3.5, 3.3, None, None),
    ("كلب الهيب هوب", "HipHopDog", 4.0, 480, 4.0, 3.8, None, None),
    ("فانوس", "GenieLamp", 4.5, 480, 4.5, 4.3, None, None),
    ("مصاصة ملونة", "LolPop", 5.0, 480, 5.0, 4.8, None, None),
    ("عوامة فلامنجو", "FlamingoFloat", 6.5, 480, 6.5, 6.2, None, None),
    ("حقيبة تيليجرام", "TelegramBag", 8.0, 480, 8.0, 7.6, None, None),
    ("قناع", "Mask", 13.8, 843, 13.81, 13.30, None, None),
]

EXPERT_SYSTEM_PROMPT = """
You are "Giftpulse", a specialized expert on Telegram Gifts, Telegram Stars, collectible attributes (model, backdrop, symbol), and the MRKT marketplace.

MARKET TERMS (follow exact MRKT trading logic):
- أسعار السوق في MRKT تُقاس بعملة TON (مثل 5t أو 5 TON)، ويجب عدم تحويل 5t إلى آلاف النجوم أو التعامل معها كنقاط عشوائية.
- الكويك / البيع الفوري (Quick / Quick sell): سعر البيع الفوري المباشر في السوق، ويكون عادةً أقل بنسبة بسيطة من الأرضية/الفلور لإتمام البيع فوراً، مثل بيع فوري عند 13.30 بينما الأرضية للموديل 13.81 (أقل بـ 4%).
- الفلور / الأرضية: سعر الأرضية للموديل أو الهدية في السوق (مثل 13.81 في مثال الموديل)، وهو المرجع الأساسي قبل خصم نسبة البيع الفوري.

STRICT RULES:
1. NEVER use ** or markdown asterisks. Plain clean text only.
2. Keep every reply short (1 to 3 concise lines), accurate, and practical with a simple example when helpful.
3. Answer ONLY about Telegram Gifts, Telegram Stars, collectibles, MRKT marketplace, and Telegram features. If outside this scope, reply in one short plain line:
   هذا البوت مخصص لهدايا تيليجرام والنجوم وسوق MRKT.
4. Never include links in normal replies. Match the user's language.
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
            custom_url TEXT
        )
    """)
    cur.execute("SELECT COUNT(*) FROM gifts")
    count = cur.fetchone()[0]
    if count == 0:
        cur.executemany("""
            INSERT INTO gifts (title, slug, ton, stars, floor_price, quick_price, gift_number, custom_url)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, INITIAL_GIFTS)
    conn.commit()
    conn.close()


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


def build_nft_link(title: str, gift_number=None, explicit_url=None, slug=None) -> str:
    if explicit_url and explicit_url.strip():
        return explicit_url.strip()

    if gift_number is not None:
        num_str = str(gift_number).strip().lstrip("#")
        if num_str.isdigit():
            resolved = resolve_slug(title, slug)
            if resolved:
                return f"https://t.me/nft/{resolved}-{num_str}"

    return DEFAULT_MARKET_URL


def get_gifts_by_ton_budget(max_ton: float, limit: int = 5):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("""
        SELECT * FROM gifts
        WHERE ton IS NOT NULL AND ton <= ?
        ORDER BY ton DESC
        LIMIT ?
    """, (max_ton, limit))
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def get_gifts_by_stars_budget(max_stars: float, limit: int = 5):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("""
        SELECT * FROM gifts
        WHERE stars IS NOT NULL AND stars <= ?
        ORDER BY stars DESC
        LIMIT ?
    """, (max_stars, limit))
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def upsert_gift_in_db(title, slug, ton_val, stars_val, floor_val, quick_val, num_val, url_val):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT * FROM gifts WHERE LOWER(title) = LOWER(?)", (title,))
    existing = cur.fetchone()

    resolved_slug = resolve_slug(title, slug or (existing["slug"] if existing else None))

    if existing:
        new_ton = ton_val if ton_val is not None else existing["ton"]
        new_stars = stars_val if stars_val is not None else existing["stars"]
        new_floor = floor_val if floor_val is not None else existing["floor_price"]
        new_quick = quick_val if quick_val is not None else existing["quick_price"]
        new_num = num_val if num_val is not None else existing["gift_number"]
        new_url = url_val if url_val is not None else existing["custom_url"]

        cur.execute("""
            UPDATE gifts
            SET slug = ?, ton = ?, stars = ?, floor_price = ?, quick_price = ?, gift_number = ?, custom_url = ?
            WHERE id = ?
        """, (resolved_slug, new_ton, new_stars, new_floor, new_quick, new_num, new_url, existing["id"]))
    else:
        cur.execute("""
            INSERT INTO gifts (title, slug, ton, stars, floor_price, quick_price, gift_number, custom_url)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            title,
            resolved_slug,
            ton_val if ton_val is not None else 0.0,
            stars_val if stars_val is not None else 0,
            floor_val,
            quick_val,
            num_val,
            url_val,
        ))

    conn.commit()
    conn.close()


def get_active_model() -> str:
    if GROQ_MODEL and GROQ_MODEL.strip():
        return GROQ_MODEL.strip()

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
            return preferred

    chat_candidates = [
        mid for mid in model_ids
        if not any(bad in mid.lower() for bad in ("whisper", "orpheus", "guard"))
    ]
    if chat_candidates:
        return chat_candidates[0]

    raise RuntimeError(f"No suitable chat model found in: {model_ids}")


async def test_mrkt_session() -> str:
    if not (API_ID_ENV.isdigit() and API_HASH_ENV and TG_SESSION_STRING):
        return "متغيرات الجلسة (TG_API_ID / TG_API_HASH / TG_SESSION_STRING) غير مكتملة."

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
                    f"اتصال Telethon ناجح بالحساب (@{me.username or me.id}) "
                    f"وتم فتح WebView: {'نعم' if has_url else 'لا'}."
                )

            msgs = await tg_client.get_messages(peer, limit=5)
            return (
                f"اتصال Telethon ناجح بالحساب (@{me.username or me.id}) "
                f"والوصول إلى @{MRKT_BOT_USERNAME} سليم (آخر رسائل محفوظة: {len(msgs)})."
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
            lines.append(f"الروابط الظاهرة: {', '.join(unique_urls[:6]) if unique_urls else 'لا توجد روابط مباشرة'}")
            lines.append(f"الأزرار الظاهرة: {', '.join(buttons_info[:6]) if buttons_info else 'لا توجد أزرار'}")
            return "\n".join(lines)
    except Exception as e:
        return f"خطأ أثناء فحص محتوى MRKT: {e}"


def extract_nft_query(text: str):
    url_match = re.search(r"(https?://t\.me/nft/[^\s]+)", text, re.IGNORECASE)
    if url_match:
        return url_match.group(1)

    num_match = re.search(r"#\s*(\d{1,9})\b", text)
    if not num_match:
        num_match = re.search(r"\b(\d{1,9})\b", text)
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


def extract_buy_budget(text: str):
    low = text.lower().strip()
    buy_keywords = (
        "شراء", "اشتري", "أشتري", "اريد", "أريد", "هديه", "هدية",
        "ميزانيه", "ميزانية", "سعر", "بقيمه", "بقيمة", "بـ", "ماركت", "mrkt",
        "buy", "gift", "budget", "ton", "تون"
    )
    has_buy_intent = any(k in low for k in buy_keywords)
    if not has_buy_intent:
        return None

    ton_match = re.search(r"(\d+(?:\.\d+)?)\s*(?:t|ton|تون)\b", low)
    if ton_match:
        return ("ton", float(ton_match.group(1)))

    stars_match = re.search(r"(\d+(?:\.\d+)?)\s*(?:نجم|نجمة|نجوم|star|stars)\b", low)
    if stars_match:
        return ("stars", float(stars_match.group(1)))

    plain_num = re.search(r"\b(\d+(?:\.\d+)?)\b", low)
    if plain_num:
        return ("ton", float(plain_num.group(1)))

    return None


def handle_admin_update(user_text: str):
    raw = user_text.split(":", 1)[1].strip()
    parts = [p.strip() for p in raw.split("|")]
    if not parts or not parts[0]:
        return "صيغة التحديث: تحديث: اسم الهدية | تون 5.0 | فلور 5.2 | كويك 4.9 | رقم 53 | رابط https://..."

    title = parts[0]
    ton_val = None
    stars_val = None
    floor_val = None
    quick_val = None
    url_val = None
    num_val = None
    slug_val = None

    for p in parts[1:]:
        low = p.lower()
        url_match = re.search(r"(https?://\S+)", p)
        if url_match and ("رابط" in p or "url" in low or "link" in low or url_match.group(1).startswith("http")):
            url_val = url_match.group(1).strip()
            continue

        if "slug" in low or "انجليزي" in p or "إنجليزي" in p:
            slug_candidate = normalize_slug(p.split(":", 1)[-1] if ":" in p else p)
            if slug_candidate:
                slug_val = slug_candidate
                continue

        num_match = re.search(r"(\d+(?:\.\d+)?)", p)
        if not num_match:
            continue
        val = float(num_match.group(1))
        if "رقم" in p or "number" in low or "#" in p:
            num_val = int(val)
        elif "فلور" in low or "أرضية" in low or "ارضية" in low or "floor" in low:
            floor_val = val
        elif "كويك" in low or "quick" in low:
            quick_val = val
        elif "نجم" in low or "star" in low:
            stars_val = int(val)
        elif "تون" in low or "ton" in low or re.search(r"\b\d+(?:\.\d+)?\s*t\b", low):
            ton_val = val

    upsert_gift_in_db(
        title=title,
        slug=slug_val or resolve_slug(title),
        ton_val=ton_val,
        stars_val=stars_val,
        floor_val=floor_val,
        quick_val=quick_val,
        num_val=num_val,
        url_val=url_val,
    )
    return f"تم حفظ وتحديث بيانات {title} في قاعدة البيانات."


@dp.message(lambda message: message.text and message.text.startswith('/start'))
async def cmd_start(message: types.Message):
    user_conversations.pop(message.from_user.id, None)
    start_text = (
        "اهلاً بك عزيزي معك البوت Giftpulse مختص بهدايا تيليجرام ومايدور حول تيليجرام فقط.\n"
        f'المطور: <a href="https://t.me/{DEVELOPER_USERNAME}">{DEVELOPER_NAME}</a>'
    )
    await message.answer(start_text, parse_mode="HTML", disable_web_page_preview=True)


@dp.message()
async def handle_user_message(message: types.Message):
    if not message.text:
        await message.answer("أرسل نصاً عن الهدايا أو TON أو سوق MRKT.")
        return

    user_id = message.from_user.id
    user_text = message.text.strip()

    if user_text == "فحص_الجلسة" and ADMIN_ID is not None and user_id == ADMIN_ID:
        status_msg = await test_mrkt_session()
        await message.answer(status_msg, disable_web_page_preview=True)
        return

    if user_text == "فحص_ماركت" and ADMIN_ID is not None and user_id == ADMIN_ID:
        info_msg = await inspect_mrkt_content()
        await message.answer(info_msg, disable_web_page_preview=True)
        return

    if user_text.startswith("تحديث:") and ADMIN_ID is not None and user_id == ADMIN_ID:
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

    budget_info = extract_buy_budget(user_text)
    if budget_info is not None:
        currency, amount = budget_info
        if currency == "ton":
            matched = get_gifts_by_ton_budget(amount, limit=5)
            if matched:
                lines = [f"هدايا MRKT ضمن ميزانية {amount:g} TON:"]
                for item in matched:
                    gift_url = build_nft_link(
                        title=item.get("title", ""),
                        gift_number=item.get("gift_number"),
                        explicit_url=item.get("custom_url"),
                        slug=item.get("slug"),
                    )
                    line = f'• <a href="{gift_url}">{item["title"]}</a> — {item["ton"]:g} TON'
                    if item.get("floor_price") is not None and item.get("quick_price") is not None:
                        line += f" (فلور {item['floor_price']} / كويك {item['quick_price']})"
                    lines.append(line)
                kb = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="فتح سوق MRKT", url=DEFAULT_MARKET_URL)],
                ])
                await message.answer("\n".join(lines), parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)
                return
            else:
                await message.answer(f"لا توجد هدية مسجلة حالياً بسعر {amount:g} TON أو أقل.")
                return

        elif currency == "stars":
            matched = get_gifts_by_stars_budget(amount, limit=5)
            if matched:
                lines = [f"هدايا ضمن {int(amount)} نجمة:"]
                for item in matched:
                    gift_url = build_nft_link(
                        title=item.get("title", ""),
                        gift_number=item.get("gift_number"),
                        explicit_url=item.get("custom_url"),
                        slug=item.get("slug"),
                    )
                    lines.append(f'• <a href="{gift_url}">{item["title"]}</a> — {item["stars"]} ⭐ ({item.get("ton", 0):g} TON)')
                kb = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="فتح سوق MRKT", url=DEFAULT_MARKET_URL)],
                ])
                await message.answer("\n".join(lines), parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)
                return

    if user_id not in user_conversations:
        user_conversations[user_id] = [
            {"role": "system", "content": EXPERT_SYSTEM_PROMPT}
        ]

    user_conversations[user_id].append({"role": "user", "content": user_text})
    if len(user_conversations[user_id]) > 5:
        user_conversations[user_id] = [user_conversations[user_id][0]] + user_conversations[user_id][-4:]

    try:
        model_to_use = get_active_model()
        response = client.chat.completions.create(
            model=model_to_use,
            messages=user_conversations[user_id],
            temperature=0.15,
            max_tokens=160,
        )
        ai_reply = clean_text(response.choices[0].message.content)
        user_conversations[user_id].append({"role": "assistant", "content": ai_reply})
        await message.answer(ai_reply, disable_web_page_preview=True)
    except Exception as e:
        print(f"Groq/Bot error: {e}")
        await message.answer("حدث خطأ تقني مؤقت، حاول مجدداً.")


async def main():
    init_db()
    print("Giftpulse Bot is starting...")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
