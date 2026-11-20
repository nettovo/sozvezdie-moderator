from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy import select, func, desc

from .config import BOT_TOKEN, ADMIN_ID, TARGET_CHAT_ID
from .db import Session, Event

bot = Bot(BOT_TOKEN)
dp = Dispatcher()


def is_admin(message: Message) -> bool:
    return bool(
        message.from_user
        and message.from_user.id == ADMIN_ID
    )


@dp.message(Command("start"), F.chat.type == "private")
async def start(message: Message):
    await message.answer(
        "Модератор «Созвездия» запущен в тестовом режиме.\n"
        "Автоматические наказания отключены."
    )


@dp.message(Command("id"))
async def show_id(message: Message):
    user_id = message.from_user.id if message.from_user else "не определён"
    await message.answer(
        f"ID чата: {message.chat.id}\nВаш ID: {user_id}"
    )


@dp.message(Command("panel"), F.chat.type == "private")
async def panel(message: Message):
    if not is_admin(message):
        await message.answer("У тебя нет доступа к панели управления.")
        return

    await message.answer(
        "ПАНЕЛЬ МОДЕРАТОРА «СОЗВЕЗДИЕ»\n\n"
        "/stats — статистика событий\n"
        "/events — последние зарегистрированные события\n\n"
        "Режим: наблюдение. Наказания отключены."
    )


@dp.message(Command("stats"), F.chat.type == "private")
async def stats(message: Message):
    if not is_admin(message):
        await message.answer("Нет доступа.")
        return

    async with Session() as session:
        total = await session.scalar(
            select(func.count(Event.id)).where(
                Event.chat_id == TARGET_CHAT_ID
            )
        )
        users = await session.scalar(
            select(func.count(func.distinct(Event.user_id))).where(
                Event.chat_id == TARGET_CHAT_ID
            )
        )

    await message.answer(
        "СТАТИСТИКА «СОЗВЕЗДИЯ»\n\n"
        f"Зарегистрировано событий: {total or 0}\n"
        f"Участников в журнале: {users or 0}\n"
        "Автоматические наказания: выключены."
    )


@dp.message(Command("events"), F.chat.type == "private")
async def events(message: Message):
    if not is_admin(message):
        await message.answer("Нет доступа.")
        return

    async with Session() as session:
        result = await session.scalars(
            select(Event)
            .where(Event.chat_id == TARGET_CHAT_ID)
            .order_by(desc(Event.id))
            .limit(5)
        )
        rows = result.all()

    if not rows:
        await message.answer("Пока событий в журнале нет.")
        return

    lines = ["ПОСЛЕДНИЕ СОБЫТИЯ:"]
    for event in rows:
        lines.append(
            f"ID записи: {event.id}\n"
            f"Участник: {event.user_id}\n"
            f"Тип: {event.event_type}\n"
        )

    await message.answer("\n".join(lines))


@dp.message()
async def observe(message: Message):
    if (
        message.chat.id != TARGET_CHAT_ID
        or not message.from_user
        or message.from_user.is_bot
    ):
        return

    async with Session() as session:
        session.add(
            Event(
                chat_id=message.chat.id,
                user_id=message.from_user.id,
                message_id=message.message_id,
                event_type="message_seen",
                details="Текст сообщения не сохраняется."
            )
        )
        await session.commit()
