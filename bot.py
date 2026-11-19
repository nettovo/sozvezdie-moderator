from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message
from .config import BOT_TOKEN
from .db import Session, Event

bot = Bot(BOT_TOKEN)
dp = Dispatcher()

@dp.message(Command("start"), F.chat.type == "private")
async def start(message: Message):
    await message.answer(
        "Бот модерации «Созвездия» запущен в тестовом режиме. "
        "Пока он только фиксирует технические события и не наказывает участников."
    )

@dp.message(Command("panel"))
async def panel(message: Message):
    await message.answer("Панель управления пока в разработке.")

@dp.message(Command("id"))
async def show_id(message: Message):
    await message.answer(f"ID чата: {message.chat.id}\nВаш ID: {message.from_user.id if message.from_user else 'не определён'}")

@dp.message()
async def observe(message: Message):
    if message.from_user and not message.from_user.is_bot:
        async with Session() as session:
            session.add(Event(
                chat_id=message.chat.id,
                user_id=message.from_user.id,
                message_id=message.message_id,
                event_type="message_seen",
                details="Текст сообщения намеренно не сохраняется на этом этапе."
            ))
            await session.commit()
