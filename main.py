"""Telegram entrypoint — polls for messages and hands them to the agent.

Only the whitelisted user (TELEGRAM_ALLOWED_USER_ID in .env) is allowed.
If the env var is empty, the bot replies with the sender's user ID so you
can paste it into .env and restart.
"""

import logging
import os

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    MessageHandler,
    filters,
)

import agent

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s")
log = logging.getLogger("nelly")

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ALLOWED_ID = os.environ.get("TELEGRAM_ALLOWED_USER_ID", "").strip()


async def on_message(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = str(update.effective_user.id)
    text = (update.message.text or "").strip()
    log.info("msg from %s: %s", user_id, text)

    if not ALLOWED_ID:
        await update.message.reply_text(
            f"Bot not yet configured. Your Telegram user ID is `{user_id}` — "
            "add it as TELEGRAM_ALLOWED_USER_ID in .env and restart.",
            parse_mode="Markdown",
        )
        return

    if user_id != ALLOWED_ID:
        await update.message.reply_text("Not authorized.")
        return

    try:
        reply = await agent.handle_message(text)
    except Exception as e:
        log.exception("agent error")
        reply = f"⚠️ {type(e).__name__}: {e}"
    await update.message.reply_text(reply)


def main() -> None:
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    log.info("Nelly is listening...")
    app.run_polling()


if __name__ == "__main__":
    main()
