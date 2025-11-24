import asyncio
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from bot.state import set_state, reset_flow_state, STATE_SEARCH


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "Welcome to PlexBot.\n"
        "Commands:\n"
        "- /menu to open quick actions\n"
        "- /buscar to search movie/series (TMDb) and set destination\n"
        "- /dbsearch <text> to search in DB, /dbstats for metrics\n"
        "- /cancel to cancel the current flow\n"
        "- Send a Telegram link or attach a file to download with TDL"
    )
    await update.message.reply_text(msg)


async def buscar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    set_state(context.user_data, STATE_SEARCH)
    await update.message.reply_text("Type a title to search (TMDb).")


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reset_flow_state(context)
    await update.message.reply_text("Flow cancelled. Use /buscar to start over.")


# Menu actions
async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    buttons = [
        [
            InlineKeyboardButton("🔍 Search", callback_data="action|search"),
            InlineKeyboardButton("📚 DB Search", callback_data="action|dbsearch"),
        ],
        [
            InlineKeyboardButton("📊 DB Stats", callback_data="action|dbstats"),
            InlineKeyboardButton("🧭 Scan libraries", callback_data="action|scan"),
        ],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel|flow")],
    ]
    kb = InlineKeyboardMarkup(buttons)
    await update.message.reply_text("Choose an action:", reply_markup=kb)


async def handle_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    action = (query.data or "").split("|")[1] if "|" in (query.data or "") else ""
    if action == "search":
        set_state(context.user_data, STATE_SEARCH)
        await query.message.reply_text("Type a title to search (TMDb).")
    elif action == "dbsearch":
        await query.message.reply_text("Use /dbsearch <title fragment> to search the database.")
    elif action == "dbstats":
        from bot.handlers.db import db_stats  # type: ignore
        await db_stats(update, context)
    elif action == "scan":
        await scan_libraries(update, context, from_callback=True)
    else:
        await query.message.reply_text("Unknown action.")


async def scan_libraries(update: Update, context: ContextTypes.DEFAULT_TYPE, from_callback: bool = False):
    # Run scan in background thread to avoid blocking
    from fs.scanner import scan_all_libraries
    from bot.db import get_session
    msg = await (update.callback_query.message.reply_text if from_callback else update.message.reply_text)(
        "Scanning libraries..."
    )

    def _run_scan():
        with get_session() as s:
            stats = scan_all_libraries(s, verbose=False)
            return stats

    stats = await asyncio.to_thread(_run_scan)
    await msg.edit_text(
        f"Scan done: +{stats.shows_new} shows, +{stats.seasons_new} seasons, +{stats.episodes_new} episodes "
        f"(files seen: {stats.files_seen})"
    )
