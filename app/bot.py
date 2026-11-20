import hashlib
import re

from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.filters.command import CommandObject
from aiogram.types import Message

from sqlalchemy import select, func, desc

from .config import BOT_TOKEN, ADMIN_ID, TARGET_CHAT_ID
from .db import Session, Event


bot = Bot(BOT_TOKEN)
dp = Dispatcher()


# ==========================================
# НАСТРОЙКИ ДЕТЕКТОРОВ
# ==========================================

# Флуд
FLOOD_LIMIT = 5
FLOOD_WINDOW_SECONDS = 10

# Спам
SPAM_LIMIT = 3
SPAM_WINDOW_SECONDS = 60
SPAM_MIN_LENGTH = 5

# CAPS
CAPS_LIMIT = 5
CAPS_WINDOW_SECONDS = 60
CAPS_MIN_LETTERS = 12
CAPS_RATIO = 0.75


SUSPICIOUS_TYPES = [
    "flood_suspected",
    "spam_suspected",
    "caps_suspected",
]

REVIEW_TYPES = [
    "review_confirmed",
    "review_dismissed",
]


# Временная память детекторов
spam_history = defaultdict(deque)
caps_history = defaultdict(deque)


# ==========================================
# ОБЩИЕ ФУНКЦИИ
# ==========================================

def is_admin(message: Message) -> bool:
    return bool(
        message.from_user
        and message.from_user.id == ADMIN_ID
    )


def normalize_text(text: str) -> str:
    text = text.lower()
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def get_fingerprint(text: str) -> str:
    normalized = normalize_text(text)

    return hashlib.sha256(
        normalized.encode("utf-8")
    ).hexdigest()


def is_caps_message(text: str) -> bool:
    letters = [
        char for char in text
        if char.isalpha()
    ]

    if len(letters) < CAPS_MIN_LETTERS:
        return False

    uppercase = sum(
        1 for char in letters
        if char.isupper()
    )

    return (
        uppercase / len(letters)
        >= CAPS_RATIO
    )


def is_spam_suspected(
    user_id: int,
    text: str
) -> bool:

    normalized = normalize_text(text)

    if len(normalized) < SPAM_MIN_LENGTH:
        return False

    fingerprint = get_fingerprint(normalized)

    key = (user_id, fingerprint)

    now = datetime.now(timezone.utc)

    cutoff = now - timedelta(
        seconds=SPAM_WINDOW_SECONDS
    )

    history = spam_history[key]

    while history and history[0] < cutoff:
        history.popleft()

    history.append(now)

    return len(history) == SPAM_LIMIT


# ==========================================
# START
# ==========================================

@dp.message(Command("start"), F.chat.type == "private")
async def start(message: Message):

    await message.answer(
        "Модератор «Созвездия» запущен.\n"
        "Режим: наблюдение.\n"
        "Автоматические наказания отключены."
    )


# ==========================================
# ID
# ==========================================

@dp.message(Command("id"))
async def show_id(message: Message):

    user_id = (
        message.from_user.id
        if message.from_user
        else "не определён"
    )

    await message.answer(
        f"ID чата: {message.chat.id}\n"
        f"Ваш ID: {user_id}"
    )


# ==========================================
# ПАНЕЛЬ
# ==========================================

@dp.message(Command("panel"), F.chat.type == "private")
async def panel(message: Message):

    if not is_admin(message):
        await message.answer("Нет доступа.")
        return

    await message.answer(
        "ПАНЕЛЬ МОДЕРАТОРА «СОЗВЕЗДИЕ»\n\n"
        "/stats — статистика\n"
        "/events — последние события\n"
        "/review — очередь проверки\n"
        "/confirm ID — подтвердить подозрение\n"
        "/dismiss ID — отклонить подозрение\n"
        "/id — узнать ID\n\n"
        "Режим: наблюдение.\n"
        "Автоматические наказания отключены."
    )


# ==========================================
# СТАТИСТИКА
# ==========================================

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
            select(
                func.count(
                    func.distinct(Event.user_id)
                )
            ).where(
                Event.chat_id == TARGET_CHAT_ID
            )
        )

        flood = await session.scalar(
            select(func.count(Event.id)).where(
                Event.chat_id == TARGET_CHAT_ID,
                Event.event_type == "flood_suspected"
            )
        )

        spam = await session.scalar(
            select(func.count(Event.id)).where(
                Event.chat_id == TARGET_CHAT_ID,
                Event.event_type == "spam_suspected"
            )
        )

        caps = await session.scalar(
            select(func.count(Event.id)).where(
                Event.chat_id == TARGET_CHAT_ID,
                Event.event_type == "caps_suspected"
            )
        )

        confirmed = await session.scalar(
            select(func.count(Event.id)).where(
                Event.chat_id == TARGET_CHAT_ID,
                Event.event_type == "review_confirmed"
            )
        )

        dismissed = await session.scalar(
            select(func.count(Event.id)).where(
                Event.chat_id == TARGET_CHAT_ID,
                Event.event_type == "review_dismissed"
            )
        )

    await message.answer(
        "СТАТИСТИКА «СОЗВЕЗДИЯ»\n\n"
        f"Зарегистрировано событий: {total or 0}\n"
        f"Участников в журнале: {users or 0}\n\n"
        f"Подозрений на флуд: {flood or 0}\n"
        f"Подозрений на спам: {spam or 0}\n"
        f"Подозрений на CAPS: {caps or 0}\n\n"
        f"Подтверждено: {confirmed or 0}\n"
        f"Отклонено: {dismissed or 0}\n\n"
        "Автоматические наказания: выключены."
    )


# ==========================================
# ПОСЛЕДНИЕ СОБЫТИЯ
# ==========================================

@dp.message(Command("events"), F.chat.type == "private")
async def events(message: Message):

    if not is_admin(message):
        await message.answer("Нет доступа.")
        return

    async with Session() as session:

        result = await session.scalars(
            select(Event)
            .where(
                Event.chat_id == TARGET_CHAT_ID
            )
            .order_by(desc(Event.id))
            .limit(10)
        )

        rows = result.all()

    if not rows:
        await message.answer("Журнал пока пуст.")
        return

    lines = ["ПОСЛЕДНИЕ СОБЫТИЯ:\n"]

    for event in rows:

        lines.append(
            f"ID записи: {event.id}\n"
            f"Участник: {event.user_id}\n"
            f"Тип: {event.event_type}\n"
            f"Причина: {event.details}\n"
        )

    await message.answer(
        "\n".join(lines)
    )


# ==========================================
# ОЧЕРЕДЬ ПРОВЕРКИ
# ==========================================

@dp.message(Command("review"), F.chat.type == "private")
async def review(message: Message):

    if not is_admin(message):
        await message.answer("Нет доступа.")
        return

    async with Session() as session:

        decisions = await session.scalars(
            select(Event.details).where(
                Event.chat_id == TARGET_CHAT_ID,
                Event.event_type.in_(REVIEW_TYPES)
            )
        )

        reviewed_ids = set()

        for detail in decisions.all():

            if detail and detail.startswith("event_id="):

                try:
                    reviewed_ids.add(
                        int(detail.split("=", 1)[1])
                    )
                except ValueError:
                    continue

        result = await session.scalars(
            select(Event)
            .where(
                Event.chat_id == TARGET_CHAT_ID,
                Event.event_type.in_(SUSPICIOUS_TYPES)
            )
            .order_by(desc(Event.id))
        )

        rows = [
            event
            for event in result.all()
            if event.id not in reviewed_ids
        ][:10]

    if not rows:
        await message.answer(
            "Очередь проверки пуста.\n"
            "Нерассмотренных подозрений нет."
        )
        return

    lines = ["СОБЫТИЯ ДЛЯ ПРОВЕРКИ:\n"]

    for event in rows:

        lines.append(
            f"ID: {event.id}\n"
            f"Участник: {event.user_id}\n"
            f"Тип: {event.event_type}\n"
            f"Причина: {event.details}\n"
            f"Подтвердить: /confirm {event.id}\n"
            f"Отклонить: /dismiss {event.id}\n"
        )

    lines.append(
        "Решение остаётся за администрацией."
    )

    await message.answer(
        "\n".join(lines)
    )


# ==========================================
# ОБРАБОТКА РЕШЕНИЙ
# ==========================================

async def process_review_decision(
    message: Message,
    command: CommandObject,
    decision_type: str,
    decision_text: str
):

    if not is_admin(message):
        await message.answer("Нет доступа.")
        return

    if not command.args:
        await message.answer(
            "Укажи ID события.\n"
            "Пример: /confirm 12"
        )
        return

    try:
        event_id = int(command.args.strip())
    except ValueError:
        await message.answer(
            "ID должен быть числом."
        )
        return

    async with Session() as session:

        original = await session.scalar(
            select(Event).where(
                Event.id == event_id,
                Event.chat_id == TARGET_CHAT_ID,
                Event.event_type.in_(SUSPICIOUS_TYPES)
            )
        )

        if original is None:
            await message.answer(
                "Подозрительное событие не найдено."
            )
            return

        existing = await session.scalar(
            select(Event.id).where(
                Event.chat_id == TARGET_CHAT_ID,
                Event.event_type.in_(REVIEW_TYPES),
                Event.details == f"event_id={event_id}"
            ).limit(1)
        )

        if existing is not None:
            await message.answer(
                "Это событие уже рассмотрено."
            )
            return

        session.add(
            Event(
                chat_id=TARGET_CHAT_ID,
                user_id=message.from_user.id,
                message_id=original.message_id,
                event_type=decision_type,
                details=f"event_id={event_id}"
            )
        )

        await session.commit()

    await message.answer(
        f"Решение сохранено: {decision_text}.\n"
        f"ID исходного события: {event_id}\n"
        "Наказания не применялись."
    )


@dp.message(Command("confirm"), F.chat.type == "private")
async def confirm(
    message: Message,
    command: CommandObject
):

    await process_review_decision(
        message,
        command,
        "review_confirmed",
        "подозрение подтверждено"
    )


@dp.message(Command("dismiss"), F.chat.type == "private")
async def dismiss(
    message: Message,
    command: CommandObject
):

    await process_review_decision(
        message,
        command,
        "review_dismissed",
        "подозрение отклонено"
    )


# ==========================================
# НАБЛЮДЕНИЕ И ДЕТЕКТОРЫ
# ==========================================

@dp.message()
async def observe(message: Message):

    if (
        message.chat.id != TARGET_CHAT_ID
        or not message.from_user
        or message.from_user.is_bot
    ):
        return

    user_id = message.from_user.id

    now = datetime.now(timezone.utc)

    text = message.text or message.caption or ""

    async with Session() as session:

        # Записываем факт сообщения
        session.add(
            Event(
                chat_id=TARGET_CHAT_ID,
                user_id=user_id,
                message_id=message.message_id,
                event_type="message_seen",
                details="Текст сообщения не сохраняется."
            )
        )

        await session.commit()

        # ----------------------------------
        # ФЛУД
        # ----------------------------------

        flood_cutoff = now - timedelta(
            seconds=FLOOD_WINDOW_SECONDS
        )

        recent_count = await session.scalar(
            select(func.count(Event.id)).where(
                Event.chat_id == TARGET_CHAT_ID,
                Event.user_id == user_id,
                Event.event_type == "message_seen",
                Event.created_at >= flood_cutoff
            )
        )

        if recent_count >= FLOOD_LIMIT:

            previous = await session.scalar(
                select(Event.id).where(
                    Event.chat_id == TARGET_CHAT_ID,
                    Event.user_id == user_id,
                    Event.event_type == "flood_suspected",
                    Event.created_at >= flood_cutoff
                ).limit(1)
            )

            if previous is None:

                session.add(
                    Event(
                        chat_id=TARGET_CHAT_ID,
                        user_id=user_id,
                        message_id=message.message_id,
                        event_type="flood_suspected",
                        details=(
                            f"{FLOOD_LIMIT} или более сообщений "
                            f"за {FLOOD_WINDOW_SECONDS} секунд."
                        )
                    )
                )

        # Команды не проверяем на спам и CAPS
        if text and not text.lstrip().startswith("/"):

            # ------------------------------
            # СПАМ
            # ------------------------------

            if is_spam_suspected(user_id, text):

                spam_cutoff = now - timedelta(
                    seconds=SPAM_WINDOW_SECONDS
                )

                previous = await session.scalar(
                    select(Event.id).where(
                        Event.chat_id == TARGET_CHAT_ID,
                        Event.user_id == user_id,
                        Event.event_type == "spam_suspected",
                        Event.created_at >= spam_cutoff
                    ).limit(1)
                )

                if previous is None:

                    session.add(
                        Event(
                            chat_id=TARGET_CHAT_ID,
                            user_id=user_id,
                            message_id=message.message_id,
                            event_type="spam_suspected",
                            details=(
                                f"Повтор одного сообщения "
                                f"{SPAM_LIMIT} раза за "
                                f"{SPAM_WINDOW_SECONDS} секунд."
                            )
                        )
                    )

            # ------------------------------
            # CAPS
            # ------------------------------

            history = caps_history[user_id]

            if is_caps_message(text):
                history.append(now)
            else:
                history.clear()

            caps_cutoff = now - timedelta(
                seconds=CAPS_WINDOW_SECONDS
            )

            while history and history[0] < caps_cutoff:
                history.popleft()

            if len(history) >= CAPS_LIMIT:

                previous = await session.scalar(
                    select(Event.id).where(
                        Event.chat_id == TARGET_CHAT_ID,
                        Event.user_id == user_id,
                        Event.event_type == "caps_suspected",
                        Event.created_at >= caps_cutoff
                    ).limit(1)
                )

                if previous is None:

                    session.add(
                        Event(
                            chat_id=TARGET_CHAT_ID,
                            user_id=user_id,
                            message_id=message.message_id,
                            event_type="caps_suspected",
                            details=(
                                f"{CAPS_LIMIT} CAPS-сообщений "
                                f"подряд за "
                                f"{CAPS_WINDOW_SECONDS} секунд."
                            )
                        )
                    )

        await session.commit()
