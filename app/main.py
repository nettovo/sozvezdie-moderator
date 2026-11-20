import os

from fastapi import FastAPI, Request, Header, HTTPException
from aiogram.types import Update

from .bot import bot, dp
from .config import WEBHOOK_SECRET
from .db import init_db

app = FastAPI()


@app.on_event("startup")
async def startup():
    await init_db()

    webhook_base = os.getenv("RENDER_EXTERNAL_URL")
    if not webhook_base:
        raise RuntimeError("RENDER_EXTERNAL_URL is not set")

    if not WEBHOOK_SECRET:
        raise RuntimeError("WEBHOOK_SECRET is not set")

    await bot.set_webhook(
        url=f"{webhook_base}/webhook",
        secret_token=WEBHOOK_SECRET,
        allowed_updates=dp.resolve_used_update_types(),
    )


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/webhook")
async def webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
):
    if x_telegram_bot_api_secret_token != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Invalid webhook secret")

    update = Update.model_validate(
        await request.json(),
        context={"bot": bot},
    )
    await dp.feed_update(bot, update)
    return {"ok": True}
