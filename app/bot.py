"""PlexBot entry point — handler registration and bot startup."""

import logging
import os
import tempfile
import traceback

from telegram import Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

from app.config import load_env_file, load_settings
from app.handlers.menu import (
    start,
    menu_cmd,
    search_cmd,
    cancel_cmd,
    cancel_all_cmd,
    queue_cmd,
    queue_cancel,
    handle_action,
    clean_tmp,
)
from app.handlers.ingest import handle_download_message
from app.handlers.search import (
    handle_page,
    handle_tmdb_select,
    handle_season,
    handle_autolib,
    handle_library,
    handle_manual_entry,
    handle_cancel_flow,
    build_results_keyboard,
)
from app.services.tmdb import search as tmdb_search, tmdb_last_error
from app.state import (
    STATE_SEARCH,
    STATE_MANUAL_TITLE,
    STATE_MANUAL_SEASON,
    set_state,
    reset_flow_state,
)


def _build_log_handlers():
    stream = logging.StreamHandler()
    handlers = [stream]
    log_path = os.getenv("PLEXBOT_LOG_PATH", "bot.log")
    try:
        log_dir = os.path.dirname(log_path)
        if log_dir:
            os.makedirs(log_dir, exist_ok=True)
        handlers.insert(0, logging.FileHandler(log_path, encoding="utf-8"))
    except Exception as e:
        fallback_dir = os.path.join(tempfile.gettempdir(), "plexbot")
        fallback_path = os.path.join(fallback_dir, "bot.log")
        try:
            os.makedirs(fallback_dir, exist_ok=True)
            handlers.insert(0, logging.FileHandler(fallback_path, encoding="utf-8"))
            print(f"Falling back to {fallback_path}: {e}")
        except Exception as e2:
            print(f"Console-only logging; cannot open log files: {e2}")
    return handlers


async def _text_router(update, context):
    text = update.message.text.strip()
    state = context.user_data.get("state")

    if state == STATE_SEARCH:
        results = tmdb_search(text)
        context.user_data["tmdb_results"] = results
        context.user_data["tmdb_page"] = 0
        context.user_data.pop("state", None)
        if results:
            first = results[0]
            markup = build_results_keyboard(results, 0)
            if first.poster:
                await update.message.reply_photo(
                    photo=first.poster,
                    caption="Results:",
                    reply_markup=markup,
                )
            else:
                await update.message.reply_text(
                    "Results:", reply_markup=markup
                )
        else:
            err = tmdb_last_error()
            note = f" TMDb: {err}" if err else ""
            await update.message.reply_text(f"No results.{note}")
        return

    if state == STATE_MANUAL_TITLE:
        context.user_data["pending_title"] = text
        context.user_data.pop("state", None)
        from app.handlers.search import build_library_keyboard

        await update.message.reply_text(
            f"Title: {text}\n\nSelect destination library:",
            reply_markup=build_library_keyboard(),
        )
        return

    if state == STATE_MANUAL_SEASON:
        try:
            season = int(text)
        except ValueError:
            await update.message.reply_text("Enter a valid season number.")
            return
        context.user_data["pending_season"] = season
        context.user_data.pop("state", None)
        from app.handlers.search import build_library_keyboard

        title = context.user_data.get("pending_title") or "Content"
        await update.message.reply_text(
            f"{title} — Season {season}\n\nSelect destination library:",
            reply_markup=build_library_keyboard(),
        )
        return

    # No state — treat as potential link or search
    if "https://t.me" in text:
        await handle_download_message(update, context)
        return

    # Default: ignore or suggest search
    return


def main():
    load_env_file()
    st = load_settings()
    token = st.telegram_token or os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("Missing TELEGRAM_BOT_TOKEN environment variable")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=_build_log_handlers(),
    )
    for noisy in (
        "httpx", "httpcore", "telegram.request", "telegram.bot",
        "telegram.ext._application",
    ):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    app = Application.builder().token(token).build()

    # Store settings in bot_data for runtime access
    app.bot_data["settings"] = st

    async def _error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        logging.getLogger().error(
            "Unhandled error: %s\n%s",
            context.error,
            traceback.format_exc(),
        )
        if update and hasattr(update, "effective_chat"):
            try:
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text="Something went wrong. Use /cancel to reset and try again.",
                )
            except Exception:
                pass

    app.add_error_handler(_error_handler)

    # Commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("search", search_cmd))
    app.add_handler(CommandHandler("cancel", cancel_cmd))
    app.add_handler(CommandHandler("cancel_all", cancel_all_cmd))
    app.add_handler(CommandHandler("menu", menu_cmd))
    app.add_handler(CommandHandler("queue", queue_cmd))
    app.add_handler(CommandHandler("clean_tmp", clean_tmp))

    # Callback handlers — order matters (most specific first)
    app.add_handler(CallbackQueryHandler(handle_action, pattern=r"^action\|"))
    app.add_handler(CallbackQueryHandler(handle_tmdb_select, pattern=r"^tmdb\|"))
    app.add_handler(CallbackQueryHandler(handle_season, pattern=r"^season\|"))
    app.add_handler(CallbackQueryHandler(handle_library, pattern=r"^lib\|"))
    app.add_handler(CallbackQueryHandler(handle_page, pattern=r"^page\|"))
    app.add_handler(CallbackQueryHandler(handle_cancel_flow, pattern=r"^cancel\|"))
    app.add_handler(CallbackQueryHandler(handle_manual_entry, pattern=r"^manual\|start"))
    app.add_handler(CallbackQueryHandler(queue_cancel, pattern=r"^cancel_task\|"))
    app.add_handler(CallbackQueryHandler(handle_autolib, pattern=r"^autolib\|"))

    # Message handlers
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, _text_router)
    )
    app.add_handler(
        MessageHandler(~filters.TEXT & ~filters.COMMAND, handle_download_message)
    )

    print("PlexBot ready. Forward links/files or use /search.")
    app.run_polling()


if __name__ == "__main__":
    main()
