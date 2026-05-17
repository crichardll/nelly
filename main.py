"""Telegram entrypoint — polls for messages and hands them to the agent.

Only whitelisted users are allowed. TELEGRAM_ALLOWED_USER_ID in .env holds a
comma-separated list of Telegram user IDs (a single ID also works). If the env
var is empty, the bot replies with the sender's user ID so you can paste it
into .env and restart.
"""

import logging
import os
import tempfile

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
# httpx logs every request URL at INFO — for Telegram that URL embeds the bot
# token (.../bot<TOKEN>/getUpdates), leaking it into the systemd journal.
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("nelly")

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ALLOWED_IDS = {
    uid.strip()
    for uid in os.environ.get("TELEGRAM_ALLOWED_USER_ID", "").split(",")
    if uid.strip()
}


def _authorized(update: Update) -> tuple[bool, str]:
    """Returns (is_authorized, user_id_str). Replies are caller's job."""
    uid = str(update.effective_user.id)
    return uid in ALLOWED_IDS, uid


async def on_message(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = str(update.effective_user.id)
    text = (update.message.text or "").strip()
    log.info("msg from %s: %s", user_id, text)

    if not ALLOWED_IDS:
        await update.message.reply_text(
            f"Bot not yet configured. Your Telegram user ID is `{user_id}` — "
            "add it as TELEGRAM_ALLOWED_USER_ID in .env and restart.",
            parse_mode="Markdown",
        )
        return

    if user_id not in ALLOWED_IDS:
        await update.message.reply_text(
            f"Not authorized. Your Telegram user ID is `{user_id}`.",
            parse_mode="Markdown",
        )
        return

    try:
        reply = await agent.handle_message(text)
    except Exception as e:
        log.exception("agent error")
        reply = f"⚠️ {type(e).__name__}: {e}"
    await update.message.reply_text(reply)


_IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif")


async def _handle_fridge_photo(update: Update, tg_file, suffix: str) -> None:
    """Download a photo to a temp file, hand it to the agent so it can Read
    the image and update the pantry stock, then clean up."""
    caption = (update.message.caption or "").strip()
    raw = bytes(await tg_file.download_as_bytearray())
    fd, path = tempfile.mkstemp(suffix=suffix or ".jpg", prefix="nelly_fridge_")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(raw)
        prompt = (
            "The user sent a photo of their fridge/pantry. "
            f"Their caption: {caption!r}."
        )
        reply = await agent.handle_message(prompt, image_path=path)
    except Exception as e:
        log.exception("agent error on photo")
        reply = f"⚠️ {type(e).__name__}: {e}"
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    await update.message.reply_text(reply or "(no reply)")


async def on_photo(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    """Telegram-compressed photos (sent inline, not as a file)."""
    ok, user_id = _authorized(update)
    log.info("photo from %s", user_id)
    if not ALLOWED_IDS or not ok:
        await update.message.reply_text(
            f"Not authorized. Your Telegram user ID is `{user_id}`.",
            parse_mode="Markdown",
        )
        return
    # photo is a list of sizes, smallest → largest; take the largest.
    f = await update.message.photo[-1].get_file()
    await _handle_fridge_photo(update, f, ".jpg")


async def on_document(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle file uploads. CSV → bank import; image → fridge photo. Both use
    the same one-turn agent flow as text messages."""
    ok, user_id = _authorized(update)
    doc = update.message.document
    log.info("doc from %s: %s (%s bytes)", user_id, doc.file_name, doc.file_size)

    if not ALLOWED_IDS or not ok:
        await update.message.reply_text(
            f"Not authorized. Your Telegram user ID is `{user_id}`.",
            parse_mode="Markdown",
        )
        return

    name = (doc.file_name or "").lower()
    mime = (doc.mime_type or "").lower()
    if mime.startswith("image/") or name.endswith(_IMAGE_EXTS):
        ext = os.path.splitext(name)[1] or ".jpg"
        f = await doc.get_file()
        await _handle_fridge_photo(update, f, ext)
        return
    if not name.endswith(".csv"):
        await update.message.reply_text(
            "Send a .csv file from your bank, or a photo of your fridge.")
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
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, on_document))
    log.info("Nelly is listening...")
    app.run_polling()


if __name__ == "__main__":
    main()
