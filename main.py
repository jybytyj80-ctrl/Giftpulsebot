import asyncio
import os
import re
from aiogram import Bot, Dispatcher, types
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from groq import Groq

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_MODEL = os.getenv("GROQ_MODEL")
DEVELOPER_USERNAME = "vvopnm8"
DEVELOPER_NAME = "عباس"

if not TELEGRAM_BOT_TOKEN or not GROQ_API_KEY:
    raise ValueError("Missing TELEGRAM_BOT_TOKEN or GROQ_API_KEY in Railway Variables")

client = Groq(api_key=GROQ_API_KEY)
bot = Bot(token=TELEGRAM_BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

MRKT_GIFTS = [
    {"title": "آيس كريم مخروطي", "stars": 300},
    {"title": "نودلز", "stars": 438},
    {"title": "آيس كريم شوكولاتة", "stars": 449},
    {"title": "شعلة", "stars": 460},
    {"title": "تمثال الحرية", "stars": 480},
    {"title": "كلب الهيب هوب", "stars": 480},
    {"title": "فانوس", "stars": 480},
    {"title": "مصاصة ملونة", "stars": 480},
    {"title": "عوامة فلامنجو", "stars": 480},
    {"title": "حقيبة تيليجرام", "stars": 480},
    {"title": "قناع", "stars": 843},
]

EXPERT_SYSTEM_PROMPT = """
You are "Giftpulse", a specialized expert on Telegram Gifts, Telegram Stars, collectible attributes (model, backdrop, symbol), and the MRKT marketplace.

MARKET TERMS (follow exact MRKT trading logic):
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


def extract_buy_budget(text: str):
    buy_keywords = (
        "شراء", "اشتري", "أشتري", "هديه", "هدية", "نجمه", "نجمة", "نجوم",
        "ميزانيه", "ميزانية", "سعر", "بقيمه", "بقيمة", "بـ", "ماركت", "mrkt",
        "buy", "gift", "star", "stars", "budget"
    )
    has_buy_intent = any(k in text.lower() for k in buy_keywords)
    match = re.search(r"\b(\d{2,6})\b", text)
    if match and has_buy_intent:
        return int(match.group(1))
    return None


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
        await message.answer("أرسل نصاً عن الهدايا أو النجوم أو سوق MRKT.")
        return

    user_id = message.from_user.id
    user_text = message.text.strip()

    budget = extract_buy_budget(user_text)
    if budget is not None:
        matched = sorted(
            [g for g in MRKT_GIFTS if g["stars"] <= budget],
            key=lambda x: x["stars"],
            reverse=True
        )[:5]
        if matched:
            lines = ["أفضل النتائج من MRKT:"]
            for item in matched:
                lines.append(f"• {item['title']} — {item['stars']} ⭐")
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="فتح سوق MRKT", url="https://t.me/mrkt")],
            ])
            await message.answer("\n".join(lines), reply_markup=kb, disable_web_page_preview=True)
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
            messages=user_conversations
