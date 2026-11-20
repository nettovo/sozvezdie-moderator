from datetime import datetime, timezone

from sqlalchemy import (
    DateTime,
    Integer,
    String,
    Text,
    ForeignKey,
    UniqueConstraint,
    Index,
)
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from .config import DATABASE_URL


def utc_now():
    return datetime.now(timezone.utc)


engine = create_async_engine(DATABASE_URL, echo=False)

Session = async_sessionmaker(
    engine,
    expire_on_commit=False,
)


class Base(DeclarativeBase):
    pass


# Журнал событий.
class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[int] = mapped_column(Integer, index=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    message_id: Mapped[int] = mapped_column(Integer)
    event_type: Mapped[str] = mapped_column(
        String(40),
        default="message",
    )
    details: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
    )


# Старая таблица апелляций.
class Appeal(Base):
    __tablename__ = "appeals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[int] = mapped_column(Integer, index=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    text: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(
        String(30),
        default="pending",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
    )


# Решения администрации о наказаниях.
class Penalty(Base):
    __tablename__ = "penalties"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    chat_id: Mapped[int] = mapped_column(Integer, index=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)

    event_id: Mapped[int | None] = mapped_column(
        ForeignKey("events.id"),
        nullable=True,
        index=True,
    )

    rule_id: Mapped[str] = mapped_column(String(20))
    penalty_type: Mapped[str] = mapped_column(String(30))

    duration_text: Mapped[str] = mapped_column(
        String(100),
        default="",
    )

    duration_minutes: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )

    reason: Mapped[str] = mapped_column(Text, default="")

    status: Mapped[str] = mapped_column(
        String(30),
        default="issued",
        index=True,
    )

    issued_by: Mapped[int] = mapped_column(Integer)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
    )

    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )


# Апелляция по конкретному наказанию.
class ModerationAppeal(Base):
    __tablename__ = "moderation_appeals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    penalty_id: Mapped[int] = mapped_column(
        ForeignKey("penalties.id"),
        index=True,
    )

    user_id: Mapped[int] = mapped_column(Integer, index=True)

    text: Mapped[str] = mapped_column(Text)

    status: Mapped[str] = mapped_column(
        String(30),
        default="pending",
        index=True,
    )

    handled_by: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )

    decision_note: Mapped[str] = mapped_column(
        Text,
        default="",
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
    )

    closed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    __table_args__ = (
        UniqueConstraint(
            "penalty_id",
            name="uq_moderation_appeal_penalty",
        ),
    )


# Сообщения в переписке по апелляции.
class AppealMessage(Base):
    __tablename__ = "appeal_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    appeal_id: Mapped[int] = mapped_column(
        ForeignKey("moderation_appeals.id"),
        index=True,
    )

    sender_id: Mapped[int] = mapped_column(Integer)

    sender_role: Mapped[str] = mapped_column(String(20))

    text: Mapped[str] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
    )


# Фиксация принятия правил пользователем.
class RuleAcceptance(Base):
    __tablename__ = "rule_acceptances"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    user_id: Mapped[int] = mapped_column(
        Integer,
        unique=True,
        index=True,
    )

    accepted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
    )


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
