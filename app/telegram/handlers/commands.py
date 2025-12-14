import asyncio
from pathlib import Path
from typing import Optional
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from app.telegram.state import set_state, reset_flow_state, STATE_SEARCH
from app.telegram.handlers.download import set_season_for_selection


def _shorten(text: str, limit: int = 42) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _format_queue_view(running, queued, note: Optional[str] = None):
    lines: list[str] = []
    if note:
        lines.append(note)

    buttons: list[list[InlineKeyboardButton]] = []
    items_for_buttons = []

    if running:
        dest = Path(running.destination).name or running.destination
        lines.append(f"▶️ {running.label} → {dest}")
        items_for_buttons.append(running)

    if queued:
        lines.append("⏳ En cola:")
        for idx, item in enumerate(queued, start=1):
            dest = Path(item.destination).name or item.destination
            lines.append(f"{idx}. {item.label} → {dest}")
            items_for_buttons.append(item)

    if not running and not queued:
        lines.append(note or "Queue is empty.")
        return "\n".join(lines), None

    for item in items_for_buttons:
        label = _shorten(item.label)
        buttons.append([InlineKeyboardButton(f"❌ Cancel {label}", callback_data=f"queue_cancel|{item.id}")])

    markup = InlineKeyboardMarkup(buttons) if buttons else None
    return "\n".join(lines), markup


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "Welcome to PlexBot.\n"
        "Commands:\n"
        "- /menu to open quick actions\n"
        "- /search to search movie/series (TMDb) and set destination\n"
        "- /queue to see your pending/running downloads and cancel\n"
        "- /dbsearch <text> to search in DB, /dbstats for metrics\n"
        "- /clean_tmp to remove temporary auto-download folders\n"
        "- /season <n> to change the active series/docuseries season\n"
        "- /cancel to cancel the current flow and running downloads\n"
        "- /cancel_all to cancel flow, running and queued downloads\n"
        "- Send a Telegram link or attach a file to download with TDL"
    )
    await update.message.reply_text(msg)


async def buscar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    set_state(context.user_data, STATE_SEARCH)
    await update.message.reply_text("Type a title to search (TMDb).")


# English alias
async def search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await buscar(update, context)


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reset_flow_state(context)
    mgr = context.bot_data.get("dl_manager")
    cancelled = 0
    if mgr and hasattr(mgr, "cancel_running"):
        cancelled = await mgr.cancel_running(update.effective_chat.id)
    msg = "Flow cancelled."
    if cancelled:
        msg += f" Cancelled {cancelled} running download(s)."
    await update.message.reply_text(f"{msg} Use /search to start over.")


async def cancel_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reset_flow_state(context)
    mgr = context.bot_data.get("dl_manager")
    cancelled_running = 0
    cancelled_queue = 0
    if mgr and hasattr(mgr, "cancel_all"):
        cancelled_running, cancelled_queue = await mgr.cancel_all(update.effective_chat.id)
    msg = "Cancelled flow and all downloads."
    if cancelled_running or cancelled_queue:
        msg += f" Stopped {cancelled_running} running and cleared {cancelled_queue} queued."
    await update.message.reply_text(f"{msg} Use /search to start over.")


async def queue_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mgr = context.bot_data.get("dl_manager")
    if not mgr or not hasattr(mgr, "snapshot"):
        await update.message.reply_text("Queue is empty.")
        return
    running, queued = await mgr.snapshot(update.effective_chat.id)
    text, markup = _format_queue_view(running, queued)
    await update.message.reply_text(text, reply_markup=markup)


async def handle_queue_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    parts = (query.data or "").split("|")
    if len(parts) != 2:
        return
    try:
        task_id = int(parts[1])
    except ValueError:
        await query.answer("Invalid item.", show_alert=True)
        return
    mgr = context.bot_data.get("dl_manager")
    if not mgr or not hasattr(mgr, "cancel_task"):
        await query.message.edit_text("Queue is empty.")
        return
    running_cancelled, queued_cancelled = await mgr.cancel_task(update.effective_chat.id, task_id)
    note = "Cancelled download." if (running_cancelled or queued_cancelled) else "Item not found in your queue."
    running, queued = await mgr.snapshot(update.effective_chat.id)
    text, markup = _format_queue_view(running, queued, note=note)
    try:
        await query.message.edit_text(text, reply_markup=markup)
    except Exception:
        await query.message.reply_text(text, reply_markup=markup)



async def season(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args or []
    if not args:
        await update.message.reply_text("Usage: /season <number>")
        return
    try:
        season_num = int(args[0])
    except ValueError:
        await update.message.reply_text("Provide a valid season number, e.g. /season 2")
        return
    dest = set_season_for_selection(context, season_num)
    if not dest:
        await update.message.reply_text("No active series/docuseries selection. Use /search and pick a season first.")
        return
    await update.message.reply_text(f"Season set to {season_num}. Destination: {dest}")


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
        from app.telegram.handlers.db import db_stats  # type: ignore
        await db_stats(update, context)
    elif action == "scan":
        await scan_libraries(update, context, from_callback=True)
    else:
        await query.message.reply_text("Unknown action.")


async def clean_tmp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import shutil
    import tempfile
    import os

    tmp_root = tempfile.gettempdir()
    prefix = "plexbot_auto_"
    removed = 0
    failed = 0
    for entry in os.listdir(tmp_root):
        if not entry.startswith(prefix):
            continue
        path = os.path.join(tmp_root, entry)
        if not os.path.isdir(path):
            continue
        try:
            shutil.rmtree(path, ignore_errors=True)
            removed += 1
        except Exception:
            failed += 1
    msg = f"Cleaned {removed} temp folders."
    if failed:
        msg += f" Failed to remove {failed} folders."
    context.chat_data.pop("auto_tmp", None)
    context.user_data.pop("auto_pending_id", None)
    await update.message.reply_text(msg)


async def scan_libraries(update: Update, context: ContextTypes.DEFAULT_TYPE, from_callback: bool = False):
    # Run scan in background thread to avoid blocking
    from fs.scanner import scan_all_libraries
    from app.infra.db import get_session
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
