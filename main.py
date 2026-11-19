import asyncio
import os
from aiogram import Bot, Dispatcher, types
from aiogram.fsm.storage.memory import MemoryStorage
from groq import Groq

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.1-8b-instant")

if not TELEGRAM_BOT_TOKEN or not GROQ_API_KEY:
    raise ValueError("Missing TELEGRAM_BOT_TOKEN or GROQ_API_KEY in Railway Variables")

client = Groq(api_key=GROQ_API_KEY)
bot = Bot(token=TELEGRAM_BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

EXPERT_SYSTEM_PROMPT = """
You are "Giftpulse", an elite and comprehensive expert, analyst, and advisor specializing in Telegram Gifts, Telegram Stars, digital collectibles/NFTs on Telegram, associated marketplaces (like t.me/mrkt), and Telegram as a whole (features, bots, channels, groups, privacy, accounts, and the Telegram ecosystem).

CRITICAL RULES YOU MUST FOLLOW:
1. **Broad Telegram & Gifts/Stars Scope:** Answer questions about Telegram Gifts, Telegram Stars, their values, rarities (models, backdrops, symbols), link parsing, marketplaces, AND general Telegram features, usage, bots, channels, groups, and ecosystem topics.
2. **Helpful & Clear Guidance:** Give practical, structured, and accurate advice tailored to the user's question (such as gift recommendations within a Stars budget, rarity factors, or Telegram features).
3. **Language Matching:** Automatically detect the user's language and reply in the exact same language.
"""

user_conversations = {}


@dp.message(lambda message: message.text and message.text.startswith('/start'))
async def cmd_start(message: types.Message):
    welcome_text = (
        "مرحباً بك في **Giftpulse** 🌟\n\n"
        "أنا مساعدك الخبير للإجابة عن كل ما يخص **هدايا تيليجرام (Telegram Gifts)**، **النجوم (Stars)**، وأسواقها، بالإضافة إلى **تيليجرام بصورة عامة**."
    )
    await message.answer(welcome_text, parse_mode="Markdown")


@dp.message()
async def handle_user_message(message: types.Message):
    if not message.text:
        await message.answer("يرجى إرسال نص مكتوب بخصوص هدايا تيليجرام، النجوم، أو تيليجرام.")
        return

    user_id = message.from_user.id
    user_text = message.text

    if user_id not in user_conversations:
        user_conversations[user_id] = [
            {"role": "system", "content": EXPERT_SYSTEM_PROMPT}
        ]

    user_conversations[user_id].append({"role": "user", "content": user_text})

    if len(user_conversations[user_id]) > 11:
        user_conversations[user_id] = [user_conversations[user_id][0]] + user_conversations[user_id][-10:]

    try:
        response = client.chat.completions.create(
            model=GROQ_MODEL,
            messages=user_conversations[user_id],
            temperature=0.3
        )
        ai_reply = response.choices[0].message.content
        user_conversations[user_id].append({"role": "assistant", "content": ai_reply})
        await message.answer(ai_reply)
    except Exception as e:
        print(f"Groq/Bot error: {e}")
        await message.answer("عذراً، حدث خطأ تقني مؤقت. يرجى المحاولة مرة أخرى لاحقاً.")


async def main():
    print("Giftpulse Bot is starting...")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
