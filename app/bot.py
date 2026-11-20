import hashlib
import re

from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.filters.command import CommandObject
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from sqlalchemy import select, func, desc

from .config import BOT_TOKEN, ADMIN_ID, TARGET_CHAT_ID
from .db import Session, Event, Appeal
from .policy import RULES, format_rule


# ==========================================
# ОСНОВНЫЕ НАСТРОЙКИ
# ==========================================

AUTO_ACTIONS_ENABLED = False
ADMIN_APPROVAL_REQUIRED = True

bot = Bot(BOT_TOKEN)
dp = Dispatcher()

FLOOD_LIMIT = 5
FLOOD_WINDOW_SECONDS = 10

SPAM_LIMIT = 4
SPAM_WINDOW_SECONDS = 120
SPAM_MIN_LENGTH = 5

CAPS_LIMIT = 5
CAPS_WINDOW_SECONDS = 120
CAPS_MIN_LETTERS = 3
CAPS_RATIO = 1.0

SUSPICIOUS_TYPES = [
    "flood_suspected",
    "spam_suspected",
    "caps_suspected",
]

REVIEW_TYPES = [
    "review_confirmed",
    "review_dismissed",
]

spam_history = defaultdict(deque)
caps_history = defaultdict(deque)

# Ожидание текста обращения в текущем процессе.
# Позже заменим постоянным хранением в БД.
appeal_waiting = set()


# ==========================================
# ОБЩИЕ ФУНКЦИИ
# ==========================================

def is_admin_id(user_id: int) -> bool:
    return user_id == ADMIN_ID


def is_admin(message: Message) -> bool:
    return bool(
        message.from_user
        and is_admin_id(message.from_user.id)
    )


def user_menu():
    builder = InlineKeyboardBuilder()

    builder.button(
        text="📋 Мои наказания",
        callback_data="my_penalties"
    )
    builder.button(
        text="✉️ Подать обращение",
        callback_data="contact_admin"
    )

    builder.adjust(1)
    return builder.as_markup()


def admin_menu():
    builder = InlineKeyboardBuilder()

    builder.button(
        text="📊 Статистика",
        callback_data="admin_stats"
    )
    builder.button(
        text="📋 Последние события",
        callback_data="admin_events"
    )
    builder.button(
        text="🔎 Очередь проверки",
        callback_data="admin_review"
    )

    builder.adjust(1)
    return builder.as_markup()


def normalize_text(text: str) -> str:
    text = text.lower()
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def get_fingerprint(text: str) -> str:
    return hashlib.sha256(
        normalize_text(text).encode("utf-8")
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

    return uppercase / len(letters) >= CAPS_RATIO


def is_spam_suspected(user_id: int, text: str) -> bool:
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

    return len(history) >= SPAM_LIMIT


async def send_admin(text: str):
    try:
        await bot.send_message(
            ADMIN_ID,
            text
        )
    except Exception:
        pass


# ==========================================
# START И ЛИЧНЫЙ КАБИНЕТ
# ==========================================

@dp.message(CommandStart(), F.chat.type == "private")
async def start(message: Message):

    await message.answer(
        "⭐ Добро пожаловать в систему модерации "
        "«Созвездие»!\n\n"
        "Здесь ты можешь:\n"
        "📋 Посмотреть информацию о наказаниях.\n"
        "✉️ Обратиться к администрации.\n\n"
        "Если ты получил наказание, подробности "
        "можно узнать здесь.",
        reply_markup=user_menu()
    )

    if is_admin(message):
        await message.answer(
            "Панель администратора:",
            reply_markup=admin_menu()
        )


# ==========================================
# МОИ НАКАЗАНИЯ
# ==========================================

@dp.callback_query(F.data == "my_penalties")
async def my_penalties(callback: CallbackQuery):

    user_id = callback.from_user.id

    async with Session() as session:

        result = await session.scalars(
            select(Event)
            .where(
                Event.chat_id == TARGET_CHAT_ID,
                Event.user_id == user_id,
                Event.event_type.in_([
                    "penalty",
                    "warning",
                    "mute",
                    "ban",
                    "remark",
                    "penalty_cancelled",
                ])
            )
            .order_by(desc(Event.id))
            .limit(20)
        )

        rows = result.all()

    if not rows:
        await callback.message.answer(
            "У тебя пока нет записей о наказаниях."
        )
        await callback.answer()
        return

    lines = ["📋 ТВОЯ ИСТОРИЯ МОДЕРАЦИИ\n"]

    for event in rows:
        lines.append(
            f"Запись №{event.id}\n"
            f"{event.details}\n"
            "--------------------"
        )

    await callback.message.answer(
        "\n".join(lines)
    )

    await callback.answer()


# ==========================================
# ОБРАЩЕНИЯ И АПЕЛЛЯЦИИ
# ==========================================

@dp.callback_query(F.data == "contact_admin")
async def contact_admin(callback: CallbackQuery):

    user_id = callback.from_user.id
    appeal_waiting.add(user_id)

    await callback.message.answer(
        "✉️ ОБРАЩЕНИЕ К АДМИНИСТРАЦИИ\n\n"
        "Напиши одним сообщением, что произошло "
        "и с каким вопросом ты обращаешься.\n\n"
        "Если ты не согласен с наказанием, укажи "
        "его номер и объясни причину.\n\n"
        "Твоё сообщение будет передано администрации."
    )

    await callback.answer()


@dp.message(F.chat.type == "private", F.text, ~F.text.startswith("/"))
async def receive_appeal(message: Message):
    
    if not message.from_user:
        return

    user_id = message.from_user.id

    if user_id not in appeal_waiting:
        return

    if is_admin(message):
        return

    text = message.text or message.caption or ""

    if not text.strip():
        await message.answer(
            "Пожалуйста, отправь текстовое обращение."
        )
        return

    appeal_waiting.discard(user_id)

    async with Session() as session:

        appeal = Appeal(
            event_id=0,
            user_id=user_id,
            text=text,
            status="pending",
        )

        session.add(appeal)
        await session.commit()
        await session.refresh(appeal)

        appeal_id = appeal.id

    await message.answer(
        f"✅ Обращение №{appeal_id} отправлено "
        "администрации.\n\n"
        "Ожидай ответа. Пожалуйста, не отправляй "
        "повторные обращения по тому же вопросу."
    )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Ответить",
                    callback_data=f"reply_appeal_{appeal_id}"
                )
            ],
            [
                InlineKeyboardButton(
                    text="Рассмотрено",
                    callback_data=f"close_appeal_{appeal_id}"
                )
            ],
        ]
    )

    await bot.send_message(
        ADMIN_ID,
        (
            f"✉️ НОВОЕ ОБРАЩЕНИЕ №{appeal_id}\n\n"
            f"Пользователь: {user_id}\n\n"
            f"Текст:\n{text}"
        ),
        reply_markup=keyboard
    )


# ==========================================
# ОТВЕТ АДМИНИСТРАТОРА
# ==========================================

@dp.callback_query(F.data.startswith("reply_appeal_"))
async def reply_appeal(callback: CallbackQuery):

    if not is_admin_id(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return

    appeal_id = int(
        callback.data.rsplit("_", 1)[1]
    )

    await callback.message.answer(
        f"Напиши ответ на обращение №{appeal_id} "
        "в формате:\n\n"
        f"/answer {appeal_id} текст ответа"
    )

    await callback.answer()


@dp.message(Command("answer"), F.chat.type == "private")
async def answer_appeal(
    message: Message,
    command: CommandObject
):

    if not is_admin(message):
        await message.answer("Нет доступа.")
        return

    if not command.args:
        await message.answer(
            "Формат: /answer ID текст ответа"
        )
        return

    parts = command.args.split(maxsplit=1)

    if len(parts) != 2:
        await message.answer(
            "Нужно указать ID и текст ответа."
        )
        return

    try:
        appeal_id = int(parts[0])
    except ValueError:
        await message.answer("Неверный ID.")
        return

    answer_text = parts[1]

    async with Session() as session:

        appeal = await session.scalar(
            select(Appeal).where(
                Appeal.id == appeal_id
            )
        )

        if appeal is None:
            await message.answer(
                "Обращение не найдено."
            )
            return

        target_user = appeal.user_id

    try:
        await bot.send_message(
            target_user,
            (
                f"📩 ОТВЕТ АДМИНИСТРАЦИИ\n"
                f"Обращение №{appeal_id}\n\n"
                f"{answer_text}"
            )
        )
    except Exception:
        await message.answer(
            "Не удалось отправить ответ. "
            "Возможно, пользователь не запустил бота."
        )
        return

    await message.answer(
        f"Ответ на обращение №{appeal_id} отправлен."
    )


@dp.callback_query(F.data.startswith("close_appeal_"))
async def close_appeal(callback: CallbackQuery):

    if not is_admin_id(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return

    appeal_id = int(
        callback.data.rsplit("_", 1)[1]
    )

    async with Session() as session:

        appeal = await session.scalar(
            select(Appeal).where(
                Appeal.id == appeal_id
            )
        )

        if appeal is None:
            await callback.answer(
                "Обращение не найдено.",
                show_alert=True
            )
            return

        appeal.status = "reviewed"
        await session.commit()

    await callback.message.answer(
        f"Обращение №{appeal_id} отмечено рассмотренным."
    )

    await callback.answer("Сохранено.")


# ==========================================
# ВЫДАЧА НАКАЗАНИЙ
# ==========================================

@dp.message(Command("rules"), F.chat.type == "private")
async def show_rules(message: Message):

    if not is_admin(message):
        await message.answer("Нет доступа.")
        return

    lines = ["📚 СПИСОК ПУНКТОВ ПРАВИЛ\n"]

    for rule_id, rule in RULES.items():
        lines.append(
            f"{rule_id} — {rule['title']}"
        )

    lines.append(
        "\nДля подробностей: /rule 2.3"
    )

    await message.answer("\n".join(lines))


@dp.message(Command("rule"), F.chat.type == "private")
async def show_rule(
    message: Message,
    command: CommandObject
):

    if not is_admin(message):
        await message.answer("Нет доступа.")
        return

    if not command.args:
        await message.answer(
            "Пример: /rule 2.3"
        )
        return

    rule_id = command.args.strip()
    rule = RULES.get(rule_id)

    if not rule:
        await message.answer("Пункт не найден.")
        return

    await message.answer(
        format_rule(rule_id)
    )


@dp.message(Command("penalty"), F.chat.type == "private")
async def issue_penalty(
    message: Message,
    command: CommandObject
):

    if not is_admin(message):
        await message.answer("Нет доступа.")
        return

    if not command.args:
        await message.answer(
            "Формат:\n"
            "/penalty USER_ID ПУНКТ ТИП СРОК\n\n"
            "Пример:\n"
            "/penalty 123456789 2.3 warning 3\n\n"
            "Типы: remark, warning, mute, ban"
        )
        return

    parts = command.args.split(maxsplit=3)

    if len(parts) < 3:
        await message.answer("Недостаточно параметров.")
        return

    try:
        target_user = int(parts[0])
    except ValueError:
        await message.answer("Неверный ID пользователя.")
        return

    rule_id = parts[1]
    penalty_type = parts[2]

    if rule_id not in RULES:
        await message.answer("Такого пункта правил нет.")
        return

    if penalty_type not in [
        "remark",
        "warning",
        "mute",
        "ban",
    ]:
        await message.answer("Неизвестный тип наказания.")
        return

    duration = parts[3] if len(parts) > 3 else "без срока"

    rule_text = format_rule(rule_id)

    details = (
        f"Наказание: {penalty_type}\n"
        f"Пункт: {rule_id}\n"
        f"Срок: {duration}\n\n"
        f"{rule_text}"
    )

    async with Session() as session:

        event = Event(
            chat_id=TARGET_CHAT_ID,
            user_id=target_user,
            message_id=0,
            event_type="penalty",
            details=details,
        )

        session.add(event)
        await session.commit()
        await session.refresh(event)

        penalty_id = event.id

    try:
        await bot.send_message(
            target_user,
            (
                f"⚖️ УВЕДОМЛЕНИЕ О НАКАЗАНИИ\n\n"
                f"Номер: {penalty_id}\n"
                f"{details}\n\n"
                "Если ты не согласен с решением, "
                "открой меню бота и подай апелляцию.\n"
                "Срок подачи апелляции — 24 часа."
            ),
            reply_markup=user_menu()
        )

        await message.answer(
            f"Запись о наказании №{penalty_id} создана. "
            "Уведомление отправлено пользователю."
        )

    except Exception:
        await message.answer(
            f"Запись №{penalty_id} сохранена, "
            "но бот не смог написать пользователю.\n"
            "Пользователь должен самостоятельно запустить бота."
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
        "/rules — список правил\n"
        "/rule ID — подробности правила\n"
        "/penalty USER_ID RULE TYPE DAYS — запись наказания\n"
        "/answer ID ТЕКСТ — ответить на обращение\n"
        "/id — узнать ID\n\n"
        "Автоматические наказания отключены.",
        reply_markup=admin_menu()
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
            select(func.count(func.distinct(Event.user_id))).where(
                Event.chat_id == TARGET_CHAT_ID
            )
        )

        counts = {}

        for event_type in [
            "flood_suspected",
            "spam_suspected",
            "caps_suspected",
            "review_confirmed",
            "review_dismissed",
            "penalty",
        ]:
            counts[event_type] = await session.scalar(
                select(func.count(Event.id)).where(
                    Event.chat_id == TARGET_CHAT_ID,
                    Event.event_type == event_type
                )
            )

    await message.answer(
        "СТАТИСТИКА «СОЗВЕЗДИЯ»\n\n"
        f"Событий: {total or 0}\n"
        f"Участников: {users or 0}\n\n"
        f"Флуд: {counts['flood_suspected'] or 0}\n"
        f"Спам: {counts['spam_suspected'] or 0}\n"
        f"CAPS: {counts['caps_suspected'] or 0}\n\n"
        f"Подтверждено: {counts['review_confirmed'] or 0}\n"
        f"Отклонено: {counts['review_dismissed'] or 0}\n"
        f"Записей о наказаниях: {counts['penalty'] or 0}\n"
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
            .where(Event.chat_id == TARGET_CHAT_ID)
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
            f"ID: {event.id}\n"
            f"Участник: {event.user_id}\n"
            f"Тип: {event.event_type}\n"
            f"Причина: {event.details}\n"
        )

    await message.answer("\n".join(lines))


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
            event for event in result.all()
            if event.id not in reviewed_ids
        ][:10]

    if not rows:
        await message.answer("Очередь проверки пуста.")
        return

    lines = ["СОБЫТИЯ ДЛЯ ПРОВЕРКИ:\n"]

    for event in rows:
        lines.append(
            f"ID: {event.id}\n"
            f"Участник: {event.user_id}\n"
            f"Тип: {event.event_type}\n"
            f"Причина: {event.details}\n"
            f"/confirm {event.id}\n"
            f"/dismiss {event.id}\n"
        )

    await message.answer("\n".join(lines))


async def process_review_decision(
    message: Message,
    command: CommandObject,
    decision_type: str,
    decision_text: str,
):

    if not is_admin(message):
        await message.answer("Нет доступа.")
        return

    if not command.args:
        await message.answer("Укажи ID события.")
        return

    try:
        event_id = int(command.args.strip())
    except ValueError:
        await message.answer("ID должен быть числом.")
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
            await message.answer("Событие не найдено.")
            return

        existing = await session.scalar(
            select(Event.id).where(
                Event.chat_id == TARGET_CHAT_ID,
                Event.event_type.in_(REVIEW_TYPES),
                Event.details == f"event_id={event_id}"
            ).limit(1)
        )

        if existing is not None:
            await message.answer("Событие уже рассмотрено.")
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
        f"{decision_text}. ID события: {event_id}.\n"
        "Наказания не применялись."
    )


@dp.message(Command("confirm"), F.chat.type == "private")
async def confirm(message: Message, command: CommandObject):
    await process_review_decision(
        message, command,
        "review_confirmed",
        "Подозрение подтверждено"
    )


@dp.message(Command("dismiss"), F.chat.type == "private")
async def dismiss(message: Message, command: CommandObject):
    await process_review_decision(
        message, command,
        "review_dismissed",
        "Подозрение отклонено"
    )


# ==========================================
# КНОПКИ АДМИНИСТРАТОРА
# ==========================================

@dp.callback_query(F.data == "admin_stats")
async def admin_stats_button(callback: CallbackQuery):
    if not is_admin_id(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await callback.message.answer("Используй /stats.")
    await callback.answer()


@dp.callback_query(F.data == "admin_events")
async def admin_events_button(callback: CallbackQuery):
    if not is_admin_id(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await callback.message.answer("Используй /events.")
    await callback.answer()


@dp.callback_query(F.data == "admin_review")
async def admin_review_button(callback: CallbackQuery):
    if not is_admin_id(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await callback.message.answer("Используй /review.")
    await callback.answer()


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

        # Флуд — только подозрение для проверки.
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
                            f"{FLOOD_LIMIT}+ сообщений за "
                            f"{FLOOD_WINDOW_SECONDS} секунд. "
                            "Нужна проверка контекста."
                        )
                    )
                )

        if text and not text.lstrip().startswith("/"):

            # Спам
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
                                "Повтор одинакового сообщения "
                                f"{SPAM_LIMIT}+ раза за "
                                f"{SPAM_WINDOW_SECONDS} секунд."
                            )
                        )
                    )

            # CAPS
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
                                "3+ CAPS-сообщения за 2 минуты. "
                                "Нужна проверка, действительно ли "
                                "это мешает общению."
                            )
                        )
                    )

        await session.commit()
