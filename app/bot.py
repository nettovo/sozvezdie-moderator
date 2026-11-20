
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
    ChatPermissions,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from sqlalchemy import select, func, desc

from .config import BOT_TOKEN, ADMIN_ID, TARGET_CHAT_ID
from .db import Session, Event, Appeal, Penalty, RuleAcceptance
from .policy import RULES, format_rule, get_penalty_options


# ==========================================
# ОСНОВНЫЕ НАСТРОЙКИ
# ==========================================

AUTO_ACTIONS_ENABLED = False
ADMIN_APPROVAL_REQUIRED = True

bot = Bot(BOT_TOKEN)
dp = Dispatcher()

FLOOD_LIMIT = 5
FLOOD_WINDOW_SECONDS = 10

SPAM_LIMIT = 3
SPAM_WINDOW_SECONDS = 120
SPAM_MIN_LENGTH = 3

CAPS_LIMIT = 5
CAPS_WINDOW_SECONDS = 120
CAPS_MIN_LETTERS = 3
CAPS_RATIO = 1.0

MAX_PENALTY_AGE_HOURS = 12

SUSPICIOUS_TYPES = [
    "flood_suspected",
    "spam_suspected",
    "caps_suspected",
]

REVIEW_TYPES = [
    "review_confirmed",
    "review_dismissed",
]

EVENT_RULES = {
    "flood_suspected": "2.2",
    "spam_suspected": "2.3",
    "caps_suspected": "2.4",
}

spam_history = defaultdict(deque)
caps_history = defaultdict(deque)
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


def utc_now():
    return datetime.now(timezone.utc)


def ensure_utc(value: datetime) -> datetime:
    """Приводит дату из БД к UTC, в том числе для SQLite."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def user_rules_keyboard():
    """Кнопки для просмотра отдельных пунктов правил."""
    builder = InlineKeyboardBuilder()

    for rule_id, rule in RULES.items():
        builder.button(
            text=f"{rule_id} — {rule['title']}",
            callback_data=f"user_rule:{rule_id}",
        )

    builder.adjust(1)
    return builder.as_markup()


def user_menu():
    builder = InlineKeyboardBuilder()

    builder.button(
        text="📋 Мои наказания",
        callback_data="my_penalties",
    )
    builder.button(
        text="📨 Мои обращения",
        callback_data="my_appeals",
    )
    builder.button(
        text="📚 Правила сообщества",
        callback_data="user_rules",
    )
    builder.button(
        text="✅ Подтвердить ознакомление с правилами",
        callback_data="accept_rules",
    )
    builder.button(
        text="✉️ Подать обращение",
        callback_data="contact_admin",
    )

    builder.adjust(1)
    return builder.as_markup()


def admin_menu():
    builder = InlineKeyboardBuilder()

    builder.button(
        text="📊 Статистика",
        callback_data="admin_stats",
    )
    builder.button(
        text="📋 Последние события",
        callback_data="admin_events",
    )
    builder.button(
        text="🔎 Очередь проверки",
        callback_data="admin_review",
    )

    builder.adjust(1)
    return builder.as_markup()


def penalty_keyboard(event_id, rule_id):
    options = get_penalty_options(rule_id)
    builder = InlineKeyboardBuilder()

    for index, option in enumerate(options):
        builder.button(
            text=option["label"],
            callback_data=f"penalty_pick:{event_id}:{index}",
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

    now = utc_now()
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
        await bot.send_message(ADMIN_ID, text)
    except Exception:
        pass


async def get_review_decision(session, event_id):
    """Возвращает существующее решение по событию, если оно есть."""
    decisions = await session.scalars(
        select(Event).where(
            Event.chat_id == TARGET_CHAT_ID,
            Event.event_type.in_(REVIEW_TYPES),
        )
    )

    for decision in decisions.all():
        if not decision.details:
            continue

        match = re.search(
            r"(?:^|[; ])event_id=(\d+)",
            decision.details,
        )

        if match and int(match.group(1)) == event_id:
            return decision

    return None


def get_option_for_duration(options, penalty_type, duration=None):
    """Проверяет, разрешён ли тип решения и его срок для правила."""
    matching = [
        option for option in options
        if option.get("type") == penalty_type
    ]

    if not matching:
        return False

    if penalty_type == "remark":
        return True

    if penalty_type == "warning":
        return any(
            option.get("min_days", 0)
            <= duration
            <= option.get("max_days", 0)
            for option in matching
        )

    if penalty_type == "mute":
        return any(
            option.get("min_minutes", 0)
            <= duration
            <= option.get("max_minutes", 0)
            for option in matching
        )

    return False


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
        "📨 Посмотреть свои обращения.\n"
        "📚 Ознакомиться с правилами сообщества.\n"
        "✅ Подтвердить ознакомление с правилами.\n"
        "✉️ Обратиться к администрации.\n\n"
        "Выбери нужный раздел:",
        reply_markup=user_menu(),
    )

    if is_admin(message):
        await message.answer(
            "Панель администратора:",
            reply_markup=admin_menu(),
        )


# ==========================================
# МОИ НАКАЗАНИЯ
# ==========================================

@dp.callback_query(F.data == "my_penalties")
async def my_penalties(callback: CallbackQuery):
    user_id = callback.from_user.id
    now = utc_now()

    async with Session() as session:
        result = await session.scalars(
            select(Penalty)
            .where(
                Penalty.chat_id == TARGET_CHAT_ID,
                Penalty.user_id == user_id,
            )
            .order_by(desc(Penalty.created_at))
            .limit(30)
        )

        rows = result.all()

        active = []
        history = []

        for penalty in rows:
            if (
                penalty.status == "issued"
                and penalty.expires_at is not None
                and ensure_utc(penalty.expires_at) <= now
            ):
                penalty.status = "completed"

            if (
                penalty.status == "issued"
                and (
                    penalty.expires_at is None
                    or ensure_utc(penalty.expires_at) > now
                )
            ):
                active.append(penalty)
            else:
                history.append(penalty)

        await session.commit()

    if not rows:
        await callback.message.answer(
            "У тебя пока нет записей о наказаниях."
        )
        await callback.answer()
        return

    lines = ["📋 ТВОЯ ИСТОРИЯ МОДЕРАЦИИ\n"]

    if active:
        lines.append("🟢 ДЕЙСТВУЮЩИЕ РЕШЕНИЯ\n")

        for penalty in active:
            lines.append(
                f"№{penalty.id}\n"
                f"Правило: {penalty.rule_id}\n"
                f"Тип: {penalty.penalty_type}\n"
                f"Срок: {penalty.duration_text or 'не указан'}\n"
                f"Причина: {penalty.reason}\n"
            )

    if history:
        lines.append("📚 ИСТОРИЯ\n")

        for penalty in history:
            if penalty.status == "cancelled":
                status = "Отменено"
            elif penalty.status == "completed":
                status = "Завершено"
            else:
                status = "Запись"

            lines.append(
                f"№{penalty.id} — {status}\n"
                f"Правило: {penalty.rule_id}\n"
                f"Тип: {penalty.penalty_type}\n"
                f"Срок: {penalty.duration_text or 'не указан'}\n"
                f"Причина: {penalty.reason}\n"
            )

    await callback.message.answer("\n".join(lines))
    await callback.answer()


# ==========================================
# МОИ ОБРАЩЕНИЯ
# ==========================================

@dp.callback_query(F.data == "my_appeals")
async def my_appeals(callback: CallbackQuery):
    user_id = callback.from_user.id

    async with Session() as session:
        result = await session.scalars(
            select(Appeal)
            .where(Appeal.user_id == user_id)
            .order_by(desc(Appeal.created_at))
            .limit(20)
        )

        rows = result.all()

    if not rows:
        await callback.message.answer(
            "У тебя пока нет обращений к администрации."
        )
        await callback.answer()
        return

    lines = ["📨 ТВОИ ОБРАЩЕНИЯ\n"]

    for appeal in rows:
        if appeal.status == "pending":
            status = "Ожидает рассмотрения"
        elif appeal.status == "reviewed":
            status = "Рассмотрено"
        else:
            status = appeal.status

        lines.append(
            f"Обращение №{appeal.id}\n"
            f"Статус: {status}\n"
            f"Дата: {ensure_utc(appeal.created_at).strftime('%d.%m.%Y %H:%M UTC')}\n"
            f"Текст: {appeal.text[:500]}\n"
        )

    await callback.message.answer("\n".join(lines))
    await callback.answer()


# ==========================================
# ПРАВИЛА: СПИСОК И ПРОСМОТР ПУНКТОВ
# ==========================================

@dp.callback_query(F.data == "user_rules")
async def user_rules_button(callback: CallbackQuery):
    await callback.message.answer(
        "📚 ПРАВИЛА СООБЩЕСТВА «СОЗВЕЗДИЕ»\n\n"
        "Выбери пункт, чтобы прочитать его:",
        reply_markup=user_rules_keyboard(),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("user_rule:"))
async def user_rule_button(callback: CallbackQuery):
    rule_id = callback.data.split(":", 1)[1]

    if rule_id not in RULES:
        await callback.answer(
            "Пункт правил не найден.",
            show_alert=True,
        )
        return

    await callback.message.answer(format_rule(rule_id))
    await callback.answer()


@dp.message(Command("rules"), F.chat.type == "private")
async def show_rules(message: Message):
    await message.answer(
        "📚 ПРАВИЛА СООБЩЕСТВА «СОЗВЕЗДИЕ»\n\n"
        "Выбери пункт, чтобы прочитать его:",
        reply_markup=user_rules_keyboard(),
    )


@dp.message(Command("rule"), F.chat.type == "private")
async def show_rule(message: Message):
    await message.answer(
        "📚 ПРАВИЛА СООБЩЕСТВА «СОЗВЕЗДИЕ»\n\n"
        "Выбери пункт, чтобы прочитать его:",
        reply_markup=user_rules_keyboard(),
    )


# ==========================================
# ПОДТВЕРЖДЕНИЕ ОЗНАКОМЛЕНИЯ С ПРАВИЛАМИ
# ==========================================

@dp.callback_query(F.data == "accept_rules")
async def accept_rules(callback: CallbackQuery):
    user_id = callback.from_user.id

    async with Session() as session:
        existing = await session.scalar(
            select(RuleAcceptance).where(
                RuleAcceptance.user_id == user_id
            )
        )

        if existing:
            await callback.message.answer(
                "✅ Ты уже подтверждал ознакомление с правилами."
            )
            await callback.answer()
            return

        session.add(
            RuleAcceptance(
                user_id=user_id,
                accepted_at=utc_now(),
            )
        )

        await session.commit()

    await callback.message.answer(
        "✅ Ознакомление с правилами зафиксировано.\n\n"
        "Спасибо! Если правила изменятся, рекомендуем "
        "ознакомиться с обновлённой версией."
    )
    await callback.answer("Сохранено.")


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

    text = message.text or ""

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
        f"✅ Обращение №{appeal_id} отправлено администрации.\n\n"
        "Ожидай ответа. Пожалуйста, не отправляй "
        "повторные обращения по тому же вопросу."
    )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Ответить",
                    callback_data=f"reply_appeal_{appeal_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="Рассмотрено",
                    callback_data=f"close_appeal_{appeal_id}",
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
        reply_markup=keyboard,
    )


# ==========================================
# ОТВЕТ АДМИНИСТРАТОРА
# ==========================================

@dp.callback_query(F.data.startswith("reply_appeal_"))
async def reply_appeal(callback: CallbackQuery):
    if not is_admin_id(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return

    appeal_id = int(callback.data.rsplit("_", 1)[1])

    await callback.message.answer(
        f"Напиши ответ на обращение №{appeal_id} "
        "в формате:\n\n"
        f"/answer {appeal_id} текст ответа"
    )

    await callback.answer()


@dp.message(Command("answer"), F.chat.type == "private")
async def answer_appeal(
    message: Message,
    command: CommandObject,
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
            select(Appeal).where(Appeal.id == appeal_id)
        )

        if appeal is None:
            await message.answer("Обращение не найдено.")
            return

        target_user = appeal.user_id

    try:
        await bot.send_message(
            target_user,
            (
                f"📩 ОТВЕТ АДМИНИСТРАЦИИ\n"
                f"Обращение №{appeal_id}\n\n"
                f"{answer_text}"
            ),
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

    appeal_id = int(callback.data.rsplit("_", 1)[1])

    async with Session() as session:
        appeal = await session.scalar(
            select(Appeal).where(Appeal.id == appeal_id)
        )

        if appeal is None:
            await callback.answer(
                "Обращение не найдено.",
                show_alert=True,
            )
            return

        appeal.status = "reviewed"
        await session.commit()
        target_user = appeal.user_id

    await callback.message.answer(
        f"Обращение №{appeal_id} отмечено рассмотренным."
    )

    try:
        await bot.send_message(
            target_user,
            f"📨 Твоё обращение №{appeal_id} отмечено как рассмотренное.",
        )
    except Exception:
        pass

    await callback.answer("Сохранено.")


# ==========================================
# СТАРАЯ КОМАНДА НАКАЗАНИЙ ОТКЛЮЧЕНА
# ==========================================

@dp.message(Command("penalty"), F.chat.type == "private")
async def old_penalty_disabled(message: Message):
    if not is_admin(message):
        await message.answer("Нет доступа.")
        return

    await message.answer(
        "Старая команда /penalty отключена.\n\n"
        "Теперь сначала открой очередь /review, "
        "проверь событие и используй /confirm ID."
    )


# ==========================================
# ПОДТВЕРЖДЕНИЕ СОБЫТИЯ И ВЫБОР РЕШЕНИЯ
# ==========================================

@dp.message(Command("confirm"), F.chat.type == "private")
async def confirm_event(
    message: Message,
    command: CommandObject,
):
    if not is_admin(message):
        await message.answer("Нет доступа.")
        return

    if not command.args:
        await message.answer("Использование: /confirm ID_СОБЫТИЯ")
        return

    try:
        event_id = int(command.args.strip())
    except ValueError:
        await message.answer("ID события должен быть числом.")
        return

    async with Session() as session:
        event = await session.scalar(
            select(Event).where(
                Event.id == event_id,
                Event.chat_id == TARGET_CHAT_ID,
                Event.event_type.in_(SUSPICIOUS_TYPES),
            )
        )

        if event is None:
            await message.answer("Подозрительное событие не найдено.")
            return

        decision = await get_review_decision(session, event_id)

        if decision:
            await message.answer("Это событие уже обработано.")
            return

        rule_id = EVENT_RULES.get(event.event_type)

        if not rule_id:
            await message.answer("Для события не найдено правило.")
            return

        options = get_penalty_options(rule_id)

        if not options:
            await message.answer("Для правила нет вариантов решения.")
            return

        await message.answer(
            f"Событие №{event_id}\n"
            f"Участник: {event.user_id}\n"
            f"Правило: {rule_id}\n\n"
            "Выбери вариант решения. Само нажатие "
            "кнопки ещё не применяет наказание:",
            reply_markup=penalty_keyboard(event_id, rule_id),
        )


@dp.callback_query(F.data.startswith("penalty_pick:"))
async def penalty_pick(callback: CallbackQuery):
    if not is_admin_id(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return

    try:
        _, event_id_text, index_text = callback.data.split(":")
        event_id = int(event_id_text)
        option_index = int(index_text)
    except (ValueError, AttributeError):
        await callback.answer("Некорректные данные.", show_alert=True)
        return

    async with Session() as session:
        event = await session.scalar(
            select(Event).where(
                Event.id == event_id,
                Event.chat_id == TARGET_CHAT_ID,
                Event.event_type.in_(SUSPICIOUS_TYPES),
            )
        )

        if event is None:
            await callback.answer("Событие не найдено.", show_alert=True)
            return

        decision = await get_review_decision(session, event_id)

        if decision:
            await callback.answer("Событие уже обработано.", show_alert=True)
            return

        rule_id = EVENT_RULES.get(event.event_type)

        if not rule_id:
            await callback.answer("Правило не найдено.", show_alert=True)
            return

        options = get_penalty_options(rule_id)

        if option_index < 0 or option_index >= len(options):
            await callback.answer("Вариант не найден.", show_alert=True)
            return

        option = options[option_index]
        option_type = option.get("type")

        if option_type not in ("remark", "warning", "mute"):
            await callback.message.answer(
                f"Выбран вариант: {option['label']}\n\n"
                "Этот вариант требует отдельного ручного решения. "
                "Бот не будет применять его автоматически."
            )
        else:
            await callback.message.answer(
                f"Выбран вариант: {option['label']}\n"
                f"Событие: №{event_id}\n\n"
                "Для замечания:\n"
                f"/apply {event_id} remark\n\n"
                "Для предупреждения:\n"
                f"/apply {event_id} warning ДНИ\n\n"
                "Для ограничения чата:\n"
                f"/apply {event_id} mute МИНУТЫ\n\n"
                "Перед применением ещё раз проверь обстоятельства "
                "и доказательства."
            )

    await callback.answer()


# ==========================================
# ПРИМЕНЕНИЕ РЕШЕНИЯ
# ==========================================

@dp.message(Command("apply"), F.chat.type == "private")
async def apply_penalty(
    message: Message,
    command: CommandObject,
):
    if not is_admin(message):
        await message.answer("Нет доступа.")
        return

    if not command.args:
        await message.answer(
            "Использование:\n"
            "/apply ID remark\n"
            "/apply ID warning ДНИ\n"
            "/apply ID mute МИНУТЫ"
        )
        return

    parts = command.args.split()

    if len(parts) < 2 or len(parts) > 3:
        await message.answer("Неверное количество параметров.")
        return

    try:
        event_id = int(parts[0])
    except ValueError:
        await message.answer("ID события должен быть числом.")
        return

    penalty_type = parts[1].lower()

    if penalty_type not in ("remark", "warning", "mute"):
        await message.answer("Неизвестный тип решения.")
        return

    duration = None

    if penalty_type == "remark" and len(parts) != 2:
        await message.answer("Для замечания срок указывать не нужно.")
        return

    if penalty_type in ("warning", "mute"):
        if len(parts) != 3:
            await message.answer("Укажи срок.")
            return

        try:
            duration = int(parts[2])
        except ValueError:
            await message.answer("Срок должен быть целым числом.")
            return

        if duration <= 0:
            await message.answer("Срок должен быть больше нуля.")
            return

    async with Session() as session:
        event = await session.scalar(
            select(Event).where(
                Event.id == event_id,
                Event.chat_id == TARGET_CHAT_ID,
                Event.event_type.in_(SUSPICIOUS_TYPES),
            )
        )

        if event is None:
            await message.answer("Событие не найдено.")
            return

        decision = await get_review_decision(session, event_id)

        if decision:
            await message.answer("Событие уже обработано.")
            return

        rule_id = EVENT_RULES.get(event.event_type)

        if not rule_id:
            await message.answer("Для события не найдено правило.")
            return

        options = get_penalty_options(rule_id)

        if not get_option_for_duration(
            options,
            penalty_type,
            duration,
        ):
            await message.answer(
                "Такой вариант или срок не предусмотрен "
                "для данного правила."
            )
            return

        event_created = ensure_utc(event.created_at)
        now = utc_now()

        if now - event_created > timedelta(
            hours=MAX_PENALTY_AGE_HOURS
        ):
            await message.answer(
                "Событие старше допустимого 12-часового срока. "
                "Наказание по нему не применено."
            )
            return

        expires_at = None
        duration_text = ""

        if penalty_type == "warning":
            expires_at = now + timedelta(days=duration)
            duration_text = f"{duration} дн."

        elif penalty_type == "mute":
            expires_at = now + timedelta(minutes=duration)
            duration_text = f"{duration} мин."

        # Ограничение применяет только администратор
        # после проверки события и допустимого срока.
        if penalty_type == "mute":
            try:
                await message.bot.restrict_chat_member(
                    chat_id=event.chat_id,
                    user_id=event.user_id,
                    permissions=ChatPermissions(
                        can_send_messages=False,
                        can_send_audios=False,
                        can_send_documents=False,
                        can_send_photos=False,
                        can_send_videos=False,
                        can_send_video_notes=False,
                        can_send_voice_notes=False,
                        can_send_polls=False,
                        can_send_other_messages=False,
                        can_add_web_page_previews=False,
                    ),
                    until_date=expires_at,
                )
            except Exception:
                await message.answer(
                    "Telegram не смог применить ограничение. "
                    "Запись о наказании не создана. Проверь права бота "
                    "и возможность ограничения этого участника."
                )
                return

        penalty = Penalty(
            chat_id=event.chat_id,
            user_id=event.user_id,
            event_id=event.id,
            rule_id=rule_id,
            penalty_type=penalty_type,
            duration_text=duration_text,
            duration_minutes=duration if penalty_type == "mute" else None,
            reason=f"Решение по событию №{event_id}",
            status="issued",
            issued_by=message.from_user.id,
            expires_at=expires_at,
        )

        session.add(penalty)

        session.add(
            Event(
                chat_id=event.chat_id,
                user_id=event.user_id,
                message_id=event.message_id,
                event_type="review_confirmed",
                details=(
                    f"event_id={event_id}; "
                    f"rule={rule_id}; "
                    f"type={penalty_type}"
                ),
            )
        )

        try:
            await session.commit()
            await session.refresh(penalty)
        except Exception:
            await session.rollback()

            await message.answer(
                "Не удалось сохранить решение в базе данных.\n\n"
                "Если Telegram уже применил ограничение, проверь "
                "статус участника вручную в настройках группы. "
                "Бот не будет автоматически менять права участника, "
                "чтобы случайно не снять другие ограничения."
            )
            return

        target_user = event.user_id
        saved_penalty_id = penalty.id

    notification = (
        "⚖️ УВЕДОМЛЕНИЕ О РЕШЕНИИ МОДЕРАЦИИ\n\n"
        f"Номер решения: {saved_penalty_id}\n"
        f"Правило: {rule_id}\n"
        f"Тип решения: {penalty_type}\n"
        f"Срок: {duration_text or 'без срока'}\n\n"
        "Если ты не согласен с решением, открой меню бота "
        "и подай обращение администрации."
    )

    try:
        await message.bot.send_message(
            target_user,
            notification,
            reply_markup=user_menu(),
        )
    except Exception:
        await message.answer(
            f"Решение №{saved_penalty_id} сохранено, "
            "но уведомление участнику отправить не удалось. "
            "Возможно, он ещё не запускал личный чат с ботом."
        )
        return

    await message.answer(
        f"Решение №{saved_penalty_id} сохранено.\n"
        f"Событие №{event_id} обработано.\n"
        "Уведомление участнику отправлено."
    )


# ==========================================
# ОТКЛОНЕНИЕ СОБЫТИЯ
# ==========================================

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
                Event.event_type.in_(SUSPICIOUS_TYPES),
            )
        )

        if original is None:
            await message.answer("Событие не найдено.")
            return

        existing = await get_review_decision(session, event_id)

        if existing is not None:
            await message.answer("Событие уже рассмотрено.")
            return

        session.add(
            Event(
                chat_id=TARGET_CHAT_ID,
                user_id=message.from_user.id,
                message_id=original.message_id,
                event_type=decision_type,
                details=f"event_id={event_id}",
            )
        )

        await session.commit()

    await message.answer(
        f"{decision_text}. ID события: {event_id}.\n"
        "Наказание не применялось."
    )


@dp.message(Command("dismiss"), F.chat.type == "private")
async def dismiss(message: Message, command: CommandObject):
    await process_review_decision(
        message,
        command,
        "review_dismissed",
        "Подозрение отклонено",
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
        "ПАНЕЛЬ МОДЕРАТОРА «СОЗВЕЗДИЯ»\n\n"
        "/stats — статистика\n"
        "/events — последние события\n"
        "/review — очередь проверки\n"
        "/confirm ID — открыть выбор решения\n"
        "/apply ID TYPE СРОК — применить решение\n"
        "/dismiss ID — отклонить подозрение\n"
        "/rules — список правил\n"
        "/rule — список пунктов с кнопками\n"
        "/answer ID ТЕКСТ — ответить на обращение\n"
        "/id — узнать ID\n\n"
        "Автоматические наказания отключены.",
        reply_markup=admin_menu(),
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
                    Event.event_type == event_type,
                )
            )

        penalty_count = await session.scalar(
            select(func.count(Penalty.id)).where(
                Penalty.chat_id == TARGET_CHAT_ID
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
        f"Старых записей о наказаниях: {counts['penalty'] or 0}\n"
        f"Новых решений: {penalty_count or 0}"
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
            select(Event).where(
                Event.chat_id == TARGET_CHAT_ID,
                Event.event_type.in_(REVIEW_TYPES),
            )
        )

        reviewed_ids = set()

        for decision in decisions.all():
            if not decision.details:
                continue

            match = re.search(
                r"(?:^|[; ])event_id=(\d+)",
                decision.details,
            )

            if match:
                reviewed_ids.add(int(match.group(1)))

        result = await session.scalars(
            select(Event)
            .where(
                Event.chat_id == TARGET_CHAT_ID,
                Event.event_type.in_(SUSPICIOUS_TYPES),
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
    now = utc_now()
    text = message.text or message.caption or ""

    async with Session() as session:
        session.add(
            Event(
                chat_id=TARGET_CHAT_ID,
                user_id=user_id,
                message_id=message.message_id,
                event_type="message_seen",
                details="Текст сообщения не сохраняется.",
            )
        )

        await session.commit()

        # Флуд — только подозрение для ручной проверки.
        flood_cutoff = now - timedelta(
            seconds=FLOOD_WINDOW_SECONDS
        )

        recent_count = await session.scalar(
            select(func.count(Event.id)).where(
                Event.chat_id == TARGET_CHAT_ID,
                Event.user_id == user_id,
                Event.event_type == "message_seen",
                Event.created_at >= flood_cutoff,
            )
        )

        if recent_count >= FLOOD_LIMIT:
            previous = await session.scalar(
                select(Event.id).where(
                    Event.chat_id == TARGET_CHAT_ID,
                    Event.user_id == user_id,
                    Event.event_type == "flood_suspected",
                    Event.created_at >= flood_cutoff,
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
                        ),
                    )
                )

        if text and not text.lstrip().startswith("/"):
            # Спам.
            if is_spam_suspected(user_id, text):
                spam_cutoff = now - timedelta(
                    seconds=SPAM_WINDOW_SECONDS
                )

                previous = await session.scalar(
                    select(Event.id).where(
                        Event.chat_id == TARGET_CHAT_ID,
                        Event.user_id == user_id,
                        Event.event_type == "spam_suspected",
                        Event.created_at >= spam_cutoff,
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
                            ),
                        )
                    )

            # CAPS: 5 сообщений за 2 минуты.
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
                        Event.created_at >= caps_cutoff,
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
                                f"{CAPS_LIMIT}+ CAPS-сообщений за "
                                f"{CAPS_WINDOW_SECONDS // 60} минуты. "
                                "Нужна проверка контекста."
                            ),
                        )
                    )

        await session.commit()
