import logging
import os
import tempfile

from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from app.telegram.handlers.commands import (
    buscar,
    search,
    cancel,
    cancel_all,
    start,
    menu,
    handle_action,
    scan_libraries,
    clean_tmp,
    season,
)
from app.telegram.handlers.download import handle_download_message
from app.telegram.handlers.search import (
    handle_manual_entry,
    handle_manual_type,
    handle_season_selection,
    handle_category_selection,
    handle_page,
    handle_tmdb_selection,
    handle_cancel_flow,
    text_router,
)
from app.telegram.handlers.db import db_search, db_stats, db_page
from config.settings import load_settings
from app.infra.env import load_env_file


def _build_log_handlers():
    """Prefer file log; fall back to /tmp if permission denied."""
    stream = logging.StreamHandler()
    handlers = [stream]
    log_path = os.getenv("PLEXBOT_LOG_PATH", "bot.log")
    try:
        log_dir = os.path.dirname(log_path)
        if log_dir:
            os.makedirs(log_dir, exist_ok=True)
        handlers.insert(0, logging.FileHandler(log_path, encoding="utf-8"))
        return handlers
    except Exception as e:
        fallback_dir = os.path.join(tempfile.gettempdir(), "plexbot")
        fallback_path = os.path.join(fallback_dir, "bot.log")
        try:
            os.makedirs(fallback_dir, exist_ok=True)
            handlers.insert(0, logging.FileHandler(fallback_path, encoding="utf-8"))
            print(f"⚠️ No se pudo abrir {log_path} ({e}); usando {fallback_path}")
        except Exception as e2:
            print(f"⚠️ Logs solo a consola; no se pudo abrir {log_path} ni {fallback_path}: {e2}")
        return handlers


def main():
    load_env_file()
    st = load_settings()
    token = st.telegram_token or os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("Missing TELEGRAM_BOT_TOKEN environment variable")

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", handlers=_build_log_handlers())
    # Drop noisy HTTP request chatter; keep warnings/errors visible.
    for noisy in ("httpx", "httpcore", "telegram.request", "telegram.bot", "telegram.ext._application"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    app = Application.builder().token(token).build()

    # Commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler(["buscar", "search"], search))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CommandHandler("cancel_all", cancel_all))
    app.add_handler(CommandHandler("menu", menu))
    app.add_handler(CommandHandler(["season", "temporada"], season))
    app.add_handler(CommandHandler("scan", scan_libraries))
    app.add_handler(CommandHandler("dbsearch", db_search))
    app.add_handler(CommandHandler("dbstats", db_stats))
    app.add_handler(CommandHandler("clean_tmp", clean_tmp))

    # Inline keyboard callbacks
    app.add_handler(CallbackQueryHandler(handle_action, pattern=r"^action\|"))
    app.add_handler(CallbackQueryHandler(handle_tmdb_selection, pattern=r"^tmdb\|"))
    app.add_handler(CallbackQueryHandler(handle_category_selection, pattern=r"^cat\|"))
    app.add_handler(CallbackQueryHandler(handle_season_selection, pattern=r"^season\|"))
    app.add_handler(CallbackQueryHandler(handle_page, pattern=r"^page\|"))
    app.add_handler(CallbackQueryHandler(handle_cancel_flow, pattern=r"^cancel\|"))
    app.add_handler(CallbackQueryHandler(db_page, pattern=r"^dbpage\|"))
    app.add_handler(CallbackQueryHandler(handle_manual_entry, pattern=r"^manual\|start"))
    app.add_handler(CallbackQueryHandler(handle_manual_type, pattern=r"^manual_type\|"))
    # Text messages → state/search router
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))
    # Non-text messages (document/video/audio/photo) → direct download
    app.add_handler(MessageHandler(~filters.TEXT & ~filters.COMMAND, handle_download_message))

    print("Bot ready. Use /search to pick a title and download.")
    app.run_polling()


if __name__ == "__main__":
    main()
