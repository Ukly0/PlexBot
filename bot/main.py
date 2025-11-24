import logging
import os

from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from bot.handlers.commands import buscar, cancel, start, menu, handle_action, scan_libraries
from bot.handlers.download import handle_download_message
from bot.handlers.search import (
    handle_manual_entry,
    handle_manual_type,
    handle_season_selection,
    handle_category_selection,
    handle_page,
    handle_tmdb_selection,
    handle_cancel_flow,
    text_router,
)
from bot.handlers.db import db_search, db_stats, db_page
from core.env import load_env_file
from config.settings import load_settings


def main():
    load_env_file()
    st = load_settings()
    token = st.telegram_token or os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("Falta TELEGRAM_BOT_TOKEN en entorno")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.FileHandler("bot.log", encoding="utf-8"), logging.StreamHandler()],
    )

    app = Application.builder().token(token).build()

    # Comandos
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("buscar", buscar))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CommandHandler("menu", menu))
    app.add_handler(CommandHandler("scan", scan_libraries))
    app.add_handler(CommandHandler("dbsearch", db_search))
    app.add_handler(CommandHandler("dbstats", db_stats))

    # Callbacks de inline keyboards
    app.add_handler(CallbackQueryHandler(handle_action, pattern=r"^action\|"))
    app.add_handler(CallbackQueryHandler(handle_tmdb_selection, pattern=r"^tmdb\|"))
    app.add_handler(CallbackQueryHandler(handle_category_selection, pattern=r"^cat\|"))
    app.add_handler(CallbackQueryHandler(handle_season_selection, pattern=r"^season\|"))
    app.add_handler(CallbackQueryHandler(handle_page, pattern=r"^page\|"))
    app.add_handler(CallbackQueryHandler(handle_cancel_flow, pattern=r"^cancel\|"))
    app.add_handler(CallbackQueryHandler(db_page, pattern=r"^dbpage\|"))
    app.add_handler(CallbackQueryHandler(handle_manual_entry, pattern=r"^manual\|start"))
    app.add_handler(CallbackQueryHandler(handle_manual_type, pattern=r"^manual_type\|"))

    # Mensajes de texto → router de estados/búsqueda/descarga
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))
    # Mensajes no-texto (documento/video/audio/foto) → descarga directa
    app.add_handler(MessageHandler(~filters.TEXT & ~filters.COMMAND, handle_download_message))

    print("Bot listo. Usa /buscar para fijar destino y descargar.")
    app.run_polling()


if __name__ == "__main__":
    main()
