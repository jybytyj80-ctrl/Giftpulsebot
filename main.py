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

# ضع هنا الهدايا وروابط Fragment الحقيقية الخاصة بها (وتقدير النجوم للمقارنة)
FRAGMENT_GIFTS = [
    # مثال (استبدل الرابط برابط حقيقي من Fragment):
    # {"title": "اسم الهدية", "stars": 500, "url": "https://fragment.com/gift/..."},
]

EXPERT_SYSTEM_PROMPT = """
You are "Giftpulse", an ultra-concise Telegram Gifts & Stars assistant.
Reply ONLY in the user's language.
Start directly with "أفضل النتائج:" (or equivalent).
When the user asks to buy a gift with Stars, keep the reply very short, mention best matching items, and include verified links (use https://fragment.com when no specific gift URL is in context).
NEVER invent or guess specific gift URLs.
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


def extract_budget(text: str):
    match = re.search(r"\b(\d{2,6})\b", text)
    return int(match.group(1)) if match else None


@dp.message(lambda message: message.text and message.text.startswith('/start'))
async def cmd_start(message: types.Message):
    await message.answer("**Giftpulse** ⚡ أرسل ميزانيتك بالنجوم.", parse_mode="Markdown")


@dp.message()
async def handle_user_message(message: types.Message):
    if not message.text:
        await message.answer("أرسل نصاً فقط.")
        return

    user_id = message.from_user.id
    user_text = message.text.strip()

    budget = extract_budget(user_text)
    if budget is not None and FRAGMENT_GIFTS:
        matched = sorted(
            [g for g in FRAGMENT_GIFTS if g["stars"] <= budget],
            key=lambda x: x["stars"],
            reverse=True
        )[:5]
        if matched:
            lines = ["أفضل النتائج:"]
            buttons = []
            for item in matched:
                lines.append(f"• {item['title']} — {item['stars']} ⭐")
                buttons.append([InlineKeyboardButton(text=f"رابط {item['title']} على Fragment", url=item["url"])])
            kb = InlineKeyboardMarkup(inline_keyboard=buttons)
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
            temperature=0.15,
            max_tokens=180
        )
        ai_reply = response.choices[0].message.content
        user_conversations[user_id].append({"role": "assistant", "content": ai_reply})

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="فتح Fragment", url="https://fragment.com")],
            [InlineKeyboardButton(text="هدايا تيليجرام", url="tg://premium_multigift")],
        ])
        await message.answer(ai_reply, reply_markup=kb, disable_web_page_preview=True)
    except Exception as e:
        print(f"Groq/Bot error: {e}")
        await message.answer("حدث خطأ تقني مؤقت، حاول مجدداً.")


async def main():
    print("Giftpulse Bot is starting...")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
