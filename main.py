import asyncio
import os
import re
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

API_ID_ENV = os.getenv("API_ID", "").strip()
API_HASH_ENV = os.getenv("API_HASH", "").strip()
TG_SESSION_STRING = os.getenv("TG_SESSION_STRING", "").strip()
MRKT_BOT_USERNAME = os.getenv("MRKT_BOT_USERNAME", "mrkt").strip().lstrip("@")
MRKT_WEB_URL = os.getenv("MRKT_WEB_URL", "https://t.me/mrkt").strip()

DEVELOPER_USERNAME = "vvopnm8"
DEVELOPER_NAME = "عباس"
DEFAULT_MARKET_URL = "https://t.me/mrkt"

if not TELEGRAM_BOT_TOKEN or not GROQ_API_KEY:
    raise ValueError("Missing TELEGRAM_BOT_TOKEN or GROQ_API_KEY in Railway Variables")

client = Groq(api_key=GROQ_API_KEY)
bot = Bot(token=TELEGRAM_BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

MRKT_GIFTS = [
    {"title": "آيس كريم مخروطي", "ton": 1.2, "stars": 300, "floor": 1.2, "quick": 1.1, "url": DEFAULT_MARKET_URL},
    {"title": "نودلز", "ton": 1.8, "stars": 438, "floor": 1.8, "quick": 1.7, "url": DEFAULT_MARKET_URL},
    {"title": "آيس كريم شوكولاتة", "ton": 2.0, "stars": 449, "floor": 2.0, "quick": 1.9, "url": DEFAULT_MARKET_URL},
    {"title": "شعلة", "ton": 2.5, "stars": 460, "floor": 2.5, "quick": 2.4, "url": DEFAULT_MARKET_URL},
    {"title": "تمثال الحرية", "ton": 3.5, "stars": 480, "floor": 3.5, "quick": 3.3, "url": DEFAULT_MARKET_URL},
    {"title": "كلب الهيب هوب", "ton": 4.0, "stars": 480, "floor": 4.0, "quick": 3.8, "url": DEFAULT_MARKET_URL},
    {"title": "فانوس", "ton": 4.5, "stars": 480, "floor": 4.5, "quick": 4.3, "url": DEFAULT_MARKET_URL},
    {"title": "مصاصة ملونة", "ton": 5.0, "stars": 480, "floor": 5.0, "quick": 4.8, "url": DEFAULT_MARKET_URL},
    {"title": "عوامة فلامنجو", "ton": 6.5, "stars": 480, "floor": 6.5, "quick": 6.2, "url": DEFAULT_MARKET_URL},
    {"title": "حقيبة تيليجرام", "ton": 8.0, "stars": 480, "floor": 8.0, "quick": 7.6, "url": DEFAULT_MARKET_URL},
    {"title": "قناع", "ton": 13.8, "stars": 843, "floor": 13.81, "quick": 13.30, "url": DEFAULT_MARKET_URL},
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


def clean_text(text: str) -> str:
    return text.replace("**", "").strip()


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
        return "متغيرات الجلسة (API_ID / API_HASH / TG_SESSION_STRING) غير مكتملة."

    try:
        async with TelegramClient(
            StringSession(TG_SESSION_STRING),
            int(API_ID_ENV),
            API_HASH_ENV,
        ) as tg_client:
            peer = await tg_client.get_input_entity(MRKT_BOT_USERNAME)
            webview = await tg_client(
                RequestWebViewRequest(
                    peer=peer,
                    bot=peer,
                    platform="android",
                    url=MRKT_WEB_URL,
                )
            )
            has_url = bool(getattr(webview, "url", None))
            return f"اتصال Telethon بـ MRKT ناجح. تم استلام رابط الجلسة: {'نعم' if has_url else 'لا'}."
    except Exception as e:
        return f"فشل اتصال Telethon بـ MRKT: {e}"


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
        return "صيغة التحديث: تحديث: اسم الهدية | تون 5.0 | فلور 5.2 | كويك 4.9 | رابط https://..."

    title = parts[0]
    ton_val = None
    stars_val = None
    floor_val = None
    quick_val = None
    url_val = None

    for p in parts[1:]:
        low = p.lower()
        url_match = re.search(r"(https?://\S+)", p)
        if url_match and ("رابط" in p or "url" in low or "link" in low or url_match.group(1).startswith("http")):
            url_val = url_match.group(1).strip()
            continue

        num_match = re.search(r"(\d+(?:\.\d+)?)", p)
        if not num_match:
            continue
        val = float(num_match.group(1))
        if "فلور" in low or "أرضية" in low or "ارضية" in low or "floor" in low:
            floor_val = val
        elif "كويك" in low or "quick" in low:
            quick_val = val
        elif "نجم" in low or "star" in low:
            stars_val = int(val)
        elif "تون" in low or "ton" in low or re.search(r"\b\d+(?:\.\d+)?\s*t\b", low):
            ton_val = val

    existing = next((g for g in MRKT_GIFTS if g["title"].lower() == title.lower()), None)
    if existing:
        if ton_val is not None:
            existing["ton"] = ton_val
        if stars_val is not None:
            existing["stars"] = stars_val
        if floor_val is not None:
            existing["floor"] = floor_val
        if quick_val is not None:
            existing["quick"] = quick_val
        if url_val is not None:
            existing["url"] = url_val
    else:
        MRKT_GIFTS.append({
            "title": title,
            "ton": ton_val if ton_val is not None else 0.0,
            "stars": stars_val if stars_val is not None else 0,
            "floor": floor_val,
            "quick": quick_val,
            "url": url_val or DEFAULT_MARKET_URL,
        })

    return f"تم تحديث بيانات {title} بنجاح."


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

    if user_text.startswith("تحديث:") and ADMIN_ID is not None and user_id == ADMIN_ID:
        msg = handle_admin_update(user_text)
        await message.answer(msg, disable_web_page_preview=True)
        return

    budget_info = extract_buy_budget(user_text)
    if budget_info is not None:
        currency, amount = budget_info
        if currency == "ton":
            matched = sorted(
                [g for g in MRKT_GIFTS if g.get("ton") is not None and g["ton"] <= amount],
                key=lambda x: x["ton"],
                reverse=True
            )[:5]
            if matched:
                lines = [f"هدايا MRKT ضمن ميزانية {amount:g} TON:"]
                for item in matched:
                    gift_url = item.get("url") or DEFAULT_MARKET_URL
                    line = f'• <a href="{gift_url}">{item["title"]}</a> — {item["ton"]:g} TON'
                    if item.get("floor") is not None and item.get("quick") is not None:
                        line += f" (فلور {item['floor']} / كويك {item['quick']})"
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
            matched = sorted(
                [g for g in MRKT_GIFTS if g.get("stars") is not None and g["stars"] <= amount],
                key=lambda x: x["stars"],
                reverse=True
            )[:5]
            if matched:
                lines = [f"هدايا ضمن {int(amount)} نجمة:"]
                for item in matched:
                    gift_url = item.get("url") or DEFAULT_MARKET_URL
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
    print("Giftpulse Bot is starting...")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
