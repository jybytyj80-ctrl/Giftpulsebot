import asyncio
import os
from aiogram import Bot, Dispatcher, types
from aiogram.fsm.storage.memory import MemoryStorage
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

EXPERT_SYSTEM_PROMPT = """
You are "Giftpulse", an ultra-concise Telegram Gifts & Stars assistant.

STRICT RULES:
1. **Ultra-Short & Direct:** No long paragraphs, no intros, no "how to buy" steps unless asked. Start directly with "أفضل النتائج:" (or equivalent in the user's language).
2. **Budget-Matched Items:** List only top gift options/categories matching the requested Stars budget.
3. **Telegram Links Only When Verified:** Include real Telegram links only when explicitly provided/known (such as https://t.me/mrkt or links shared by the user). NEVER invent or guess gift URLs. If a direct item link isn't available, give the exact item name/filter and https://t.me/mrkt.
4. **Language Matching:** Match the user's language strictly.
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


@dp.message(lambda message: message.text and message.text.startswith('/start'))
async def cmd_start(message: types.Message):
    await message.answer("**Giftpulse** ⚡ أرسل ميزانيتك أو سؤالك.", parse_mode="Markdown")


@dp.message()
async def handle_user_message(message: types.Message):
    if not message.text:
        await message.answer("أرسل نصاً فقط.")
        return

    user_id = message.from_user.id
    user_text = message.text

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
            temperature=0.2,
            max_tokens=220
        )
        ai_reply = response.choices[0].message.content
        user_conversations[user_id].append({"role": "assistant", "content": ai_reply})
        await message.answer(ai_reply)
    except Exception as e:
        print(f"Groq/Bot error: {e}")
        await message.answer("حدث خطأ تقني مؤقت، حاول مجدداً.")


async def main():
    print("Giftpulse Bot is starting...")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
