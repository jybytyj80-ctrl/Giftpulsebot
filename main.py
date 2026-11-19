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

if not TELEGRAM_BOT_TOKEN or not GROQ_API_KEY:
    raise ValueError("Missing TELEGRAM_BOT_TOKEN or GROQ_API_KEY in Railway Variables")

client = Groq(api_key=GROQ_API_KEY)
bot = Bot(token=TELEGRAM_BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

GIFT_ITEMS = [
    {"title": "آيس كريم مخروطي", "stars": 300, "url": "tg://premium_multigift"},
    {"title": "نودلز", "stars": 438, "url": "tg://premium_multigift"},
    {"title": "آيس كريم شوكولاتة", "stars": 449, "url": "tg://premium_multigift"},
    {"title": "شعلة", "stars": 460, "url": "tg://premium_multigift"},
    {"title": "تمثال الحرية", "stars": 480, "url": "tg://premium_multigift"},
    {"title": "كلب الهيب هوب", "stars": 480, "url": "tg://premium_multigift"},
    {"title": "فانوس", "stars": 480, "url": "tg://premium_multigift"},
    {"title": "مصاصة ملونة", "stars": 480, "url": "tg://premium_multigift"},
    {"title": "عوامة فلامنجو", "stars": 480, "url": "tg://premium_multigift"},
    {"title": "حقيبة تيليجرام", "stars": 480, "url": "tg://premium_multigift"},
    {"title": "قناع", "stars": 843, "url": "tg://premium_multigift"},
]

EXPERT_SYSTEM_PROMPT = """
You are "Giftpulse", a strictly specialized Telegram Gifts & Telegram Stars expert inside Telegram.

DOMAIN & TERMS:
- Answer ONLY about Telegram Gifts, Telegram Stars, collectible gifts/rarity, gift marketplaces, and Telegram features.
- Interpret terms like "الفلور" / "Floor" (floor price / lowest market price for a gift or collectible) and "الكويك" / "Quick" (fast/instant action or quick market flow) strictly within the context of Telegram Gifts, Stars, and collectible marketplaces. Never give chemical/school meanings.
- If a question is truly outside Telegram Gifts, Stars, or Telegram, reply in one short line:
  "مختص فقط بهدايا تيليجرام، النجوم، وميزات تيليجرام."
- Start on-topic answers directly with "أفضل النتائج:" when giving options/recommendations, keep it ultra-short and practical, and NEVER include links unless the user asks to buy a gift within a specific Stars budget.
- Match the user's language strictly.
"""

user_conversations = {}


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
        "ميزانيه", "ميزانية", "سعر", "بقيمه", "بقيمة", "بـ",
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
    await message.answer(
        "**Giftpulse** ⚡ خبير هدايا ونجوم تيليجرام.",
        parse_mode="Markdown"
    )


@dp.message()
async def handle_user_message(message: types.Message):
    if not message.text:
        await message.answer("أرسل نصاً عن الهدايا أو النجوم.")
        return

    user_id = message.from_user.id
    user_text = message.text.strip()

    budget = extract_buy_budget(user_text)
    if budget is not None:
        matched = sorted(
            [g for g in GIFT_ITEMS if g["stars"] <= budget],
            key=lambda x: x["stars"],
            reverse=True
        )[:5]
        if matched:
            lines = ["أفضل النتائج:"]
            for item in matched:
                lines.append(f"• {item['title']} — {item['stars']}+ ⭐")
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="هدايا تيليجرام", url="tg://premium_multigift")],
                [InlineKeyboardButton(text="السوق MRKT", url="https://t.me/mrkt")],
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
            messages=user_conversations[user_id],
            temperature=0.1,
            max_tokens=160
        )
        ai_reply = response.choices[0].message.content
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
