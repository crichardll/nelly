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


def _authorized(update: Update) -> tuple[bool, str]:
    """Returns (is_authorized, user_id_str). Replies are caller's job."""
    return str(update.effective_user.id) == ALLOWED_ID, str(update.effective_user.id)


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


async def on_document(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle file uploads (CSV from the bank). Downloads, decodes to text,
    and wraps into a prompt for the agent — same one-turn flow as text msgs."""
    ok, user_id = _authorized(update)
    doc = update.message.document
    log.info("doc from %s: %s (%s bytes)", user_id, doc.file_name, doc.file_size)

    if not ALLOWED_ID or not ok:
        await update.message.reply_text("Not authorized.")
        return

    name = (doc.file_name or "").lower()
    if not name.endswith(".csv"):
        await update.message.reply_text("Send a .csv file from your bank.")
        return

    try:
        f = await doc.get_file()
        raw = bytes(await f.download_as_bytearray())
        try:
            csv_text = raw.decode("utf-8")
        except UnicodeDecodeError:
            csv_text = raw.decode("latin-1")
        caption = (update.message.caption or "classify these expenses").strip()
        prompt = (
            f"The user uploaded a CSV file named {doc.file_name!r}. "
            f"Their caption: {caption!r}.\n\n"
            f"CSV CONTENT:\n{csv_text}"
        )
        reply = await agent.handle_message(prompt)
    except Exception as e:
        log.exception("agent error on doc")
        reply = f"⚠️ {type(e).__name__}: {e}"
    await update.message.reply_text(reply or "(no reply)")


def main() -> None:
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    app.add_handler(MessageHandler(filters.Document.ALL, on_document))
    log.info("Nelly is listening...")
    app.run_polling()


if __name__ == "__main__":
    main()
