
import os

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "sqlite+aiosqlite:///./moderator.db"
)

ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
TARGET_CHAT_ID = int(os.getenv("TARGET_CHAT_ID", "0"))
