# Sozvezdie Moderator — starter skeleton

This is an early technical skeleton, not a finished moderation system.

## Current behavior
- Receives Telegram updates through a webhook.
- `/start` responds in private chat.
- `/id` shows chat and user IDs.
- Records message metadata (chat/user/message IDs and timestamp).
- Does not save message text.
- Does not delete messages, mute, ban, or issue warnings.
- Automatic actions are disabled.

## Important
Do not commit `.env` or your Telegram bot token to GitHub.
Set secrets as environment variables in the hosting provider.

## Telegram setup
1. Add the bot to the group.
2. Disable Group Privacy in BotFather.
3. For this initial observer stage, do not grant delete/restrict permissions.
4. Use `/id` in the group to obtain the chat ID.

## Render
Create a Web Service from this repository. Render's free service may sleep when idle,
and its local filesystem is ephemeral. For persistent event history, configure an
external PostgreSQL database and set `DATABASE_URL`.

## Next development stages
- Webhook registration and deployment automation.
- Deterministic flood/spam/CAPS detectors.
- Admin authorization and review queue.
- Appeal workflow and audit log.
- Context-aware AI analysis with human review for ambiguous cases.
- Data retention and privacy controls.
