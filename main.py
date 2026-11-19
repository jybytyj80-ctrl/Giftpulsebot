‏import asyncio
‏from aiogram import Bot, Dispatcher, types
‏from aiogram.fsm.storage.memory import MemoryStorage
‏from groq import Groq
‏
‏TELEGRAM_BOT_TOKEN = "9169040757:AAEPPnxFAiz_93l-GT6sNn2Txwr0_l9qDYo"
‏GROQ_API_KEY = "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"
‏
‏client = Groq(api_key=GROQ_API_KEY)
‏bot = Bot(token=TELEGRAM_BOT_TOKEN)
‏storage = MemoryStorage()
‏dp = Dispatcher(storage=storage)
‏
‏EXPERT_SYSTEM_PROMPT = """
‏You are "Giftpulse", an elite and world-class expert, analyst, and advisor specializing *exclusively* and *strictly* in Telegram Gifts, Telegram Stars, digital collectibles/NFTs on Telegram, and associated marketplaces (like t.me/mrkt).
‏
‏CRITICAL RULES YOU MUST FOLLOW:
‏1. **Strict Scope Enforcement:** You MUST ONLY answer questions related to Telegram Gifts, Telegram Stars, their values, rarities (models, backdrops, symbols), link parsing, and the Telegram digital economy.
‏2. **Rejection Policy:** If the user asks about *anything* outside of Telegram Gifts, Stars, or their marketplace ecosystem, you must politely refuse and state that you are exclusively a Telegram Gifts & Stars expert.
‏3. **Language Matching:** Automatically detect the user's language and reply in the exact same language.
‏"""
‏
‏user_conversations = {}
‏
‏
‏@dp.message(lambda message: message.text and message.text.startswith('/start'))
‏async def cmd_start(message: types.Message):
‏    welcome_text = (
‏        "مرحباً بك في **Giftpulse** 🌟\n\n"
‏        "أنا مساعدك الخبير والمخصص حصرياً للإجابة عن كل ما يخص **هدايا تيليجرام (Telegram Gifts)**، **النجوم (Stars)**، وأسواقها التحليلية."
‏    )
‏    await message.answer(welcome_text, parse_mode="Markdown")
‏
‏
‏@dp.message()
‏async def handle_user_message(message: types.Message):
‏    user_id = message.from_user.id
‏    user_text = message.text
‏
‏    if user_id not in user_conversations:
‏        user_conversations[user_id] = [
‏            {"role": "system", "content": EXPERT_SYSTEM_PROMPT}
‏        ]
‏
‏    user_conversations[user_id].append({"role": "user", "content": user_text})
‏
‏    if len(user_conversations[user_id]) > 11:
‏        user_conversations[user_id] = [user_conversations[user_id][0]] + user_conversations[user_id][-10:]
‏
‏    try:
‏        response = client.chat.completions.create(
‏            model="llama-3.3-70b-versatile",
‏            messages=user_conversations[user_id],
‏            temperature=0.2
‏        )
‏        ai_reply = response.choices[0].message.content
‏        user_conversations[user_id].append({"role": "assistant", "content": ai_reply})
‏        await message.answer(ai_reply)
‏    except Exception:
‏        await message.answer("عذراً، حدث خطأ تقني مؤقت. يرجى المحاولة مرة أخرى لاحقاً.")
‏
‏
‏async def main():
‏    print("Giftpulse Bot is starting...")
‏    await dp.start_polling(bot)
‏
‏
‏if __name__ == "__main__":
‏    asyncio.run(main())
‏
