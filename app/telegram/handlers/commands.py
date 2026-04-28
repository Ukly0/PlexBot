import asyncio
import logging
from pathlib import Path
from typing import Optional
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from sqlalchemy import func
from sqlalchemy.exc import OperationalError

from app.services.download_manager import ContentSummary

from app.telegram.state import set_state, reset_flow_state, STATE_DB_SEARCH, STATE_SEARCH
from app.telegram.handlers.download import set_season_for_selection
from app.infra.db import get_session
from config.settings import load_settings
from store.models import Show, Season
from store.repos import recent_content

CART_PAGE_SIZE = 8


def _shorten(text: str, limit: int = 42) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _format_queue_view(running: Optional[ContentSummary], queued: list[ContentSummary], note: Optional[str] = None):
    lines: list[str] = []
    if note:
        lines.append(note)

    buttons: list[list[InlineKeyboardButton]] = []
    items_for_buttons = []

    if running:
        dest = Path(running.destination).name or running.destination
        pending_note = f" (+{running.pending} pending)" if running.pending else ""
        lines.append(f"▶️ {running.label}{pending_note} → {dest}")
        items_for_buttons.append(running)

    if queued:
        lines.append("⏳ In queue:")
        for idx, item in enumerate(queued, start=1):
            dest = Path(item.destination).name or item.destination
            count_note = f" ({item.total} pending)" if item.total > 1 else ""
            lines.append(f"{idx}. {item.label}{count_note} → {dest}")
            items_for_buttons.append(item)

    if not running and not queued:
        lines.append(note or "Queue is empty.")
        return "\n".join(lines), None

    for item in items_for_buttons:
        label = _shorten(item.label)
        buttons.append([InlineKeyboardButton(f"❌ Cancel {label}", callback_data=f"queue_cancel|{item.representative_task_id}")])

    markup = InlineKeyboardMarkup(buttons) if buttons else None
    return "\n".join(lines), markup


def _is_admin(update: Update) -> bool:
    admin_chat_id = load_settings().admin_chat_id
    if not admin_chat_id:
        return True
    chat = update.effective_chat
    return bool(chat and str(chat.id) == str(admin_chat_id))


def _main_menu_markup(is_admin: bool, cart_count: int = 0) -> InlineKeyboardMarkup:
    cart_label = f"🛒 Inbox ({cart_count})" if cart_count else "🛒 Inbox"
    buttons = [
        [InlineKeyboardButton("➕ Add content", callback_data="action|search")],
        [InlineKeyboardButton(cart_label, callback_data="action|cart"), InlineKeyboardButton("⏳ Queue", callback_data="action|queue")],
        [InlineKeyboardButton("🆕 Recently added", callback_data="action|recent")],
    ]
    if is_admin:
        buttons.append([InlineKeyboardButton("⚙️ Admin", callback_data="action|admin")])
    buttons.append([InlineKeyboardButton("❌ Cancel current flow", callback_data="cancel|flow")])
    return InlineKeyboardMarkup(buttons)


def _home_button() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Main menu", callback_data="action|home")]])


def _add_content_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🔍 Search TMDb", callback_data="action|search_tmdb")],
            [InlineKeyboardButton("✍️ Manual entry", callback_data="manual|start")],
            [InlineKeyboardButton("⬅️ Back", callback_data="action|home")],
        ]
    )


def _cart_items(context: ContextTypes.DEFAULT_TYPE) -> list[dict]:
    return context.chat_data.setdefault("cart", [])


def _cart_markup(has_items: bool, page: int = 0, total: int = 0) -> InlineKeyboardMarkup:
    buttons = []
    if has_items:
        start = page * CART_PAGE_SIZE
        end = min(start + CART_PAGE_SIZE, total)
        for idx in range(start, end):
            buttons.append([InlineKeyboardButton(f"❌ Remove {idx + 1}", callback_data=f"cart|remove|{idx}")])
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("⬅️", callback_data=f"cart|page|{page - 1}"))
        if end < total:
            nav.append(InlineKeyboardButton("➡️", callback_data=f"cart|page|{page + 1}"))
        if nav:
            buttons.append(nav)
        buttons.append([InlineKeyboardButton("🎯 Choose destination", callback_data="cart|assign")])
        buttons.append([InlineKeyboardButton("🗑️ Clear inbox", callback_data="cart|clear")])
    buttons.append([InlineKeyboardButton("🏠 Main menu", callback_data="action|home")])
    return InlineKeyboardMarkup(buttons)


def _format_cart(context: ContextTypes.DEFAULT_TYPE, page: int | None = None) -> tuple[str, InlineKeyboardMarkup]:
    items = _cart_items(context)
    if not items:
        return "🛒 The inbox is empty.\n\nForward Telegram links or files to add them here.", _cart_markup(False)
    if page is None:
        page = int(context.user_data.get("cart_page") or 0)
    total_pages = max(1, (len(items) + CART_PAGE_SIZE - 1) // CART_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    context.user_data["cart_page"] = page
    start = page * CART_PAGE_SIZE
    end = min(start + CART_PAGE_SIZE, len(items))
    lines = [f"🛒 Temporary inbox: {len(items)} item(s)", ""]
    for idx, item in enumerate(items[start:end], start=start + 1):
        label = item.get("filename") or item.get("link") or "Item"
        lines.append(f"{idx}. {_shorten(str(label), 54)}")
    if total_pages > 1:
        lines.append(f"\nPage {page + 1}/{total_pages}")
    lines.append("\nChoose a destination to enqueue everything together.")
    return "\n".join(lines), _cart_markup(True, page, len(items))


async def _reply_or_edit(update: Update, text: str, reply_markup=None):
    query = update.callback_query
    if query and query.message:
        try:
            await query.message.edit_text(text, reply_markup=reply_markup)
            return
        except Exception:
            await query.message.reply_text(text, reply_markup=reply_markup)
            return
    if update.message:
        await update.message.reply_text(text, reply_markup=reply_markup)


async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cart_count = len(context.chat_data.get("cart") or [])
    text = (
        "PlexBot dashboard\n\n"
        "Forward Telegram links or files to add them to the inbox, or use the buttons to search and manage the queue."
    )
    await _reply_or_edit(update, text, _main_menu_markup(_is_admin(update), cart_count))


async def show_cart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text, markup = _format_cart(context)
    await _reply_or_edit(update, text, markup)


async def show_recent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        with get_session() as s:
            items = recent_content(s, 10)
    except OperationalError:
        await _reply_or_edit(update, "The recent-content database is not initialized yet. Add content first.", _home_button())
        return
    except Exception as e:
        await _reply_or_edit(update, f"Could not load recently added content: {e}", _home_button())
        return
    if not items:
        await _reply_or_edit(update, "No recently added content yet. Completed downloads will appear here.", _home_button())
        return
    lines = ["🆕 Recently added", ""]
    for item in items:
        year = f" ({item['year']})" if item.get("year") else ""
        kind = str(item.get("kind") or "")
        season = item.get("season")
        detail = ""
        if kind in {"series", "anime", "docuseries"} and season is not None:
            detail = f" - Season {season}"
        destination = Path(item.get("destination") or "").name
        dest_note = f" · {destination}" if destination else ""
        lines.append(f"- {item['title']}{year}{detail}{dest_note}")
    await _reply_or_edit(update, "\n".join(lines), _home_button())


async def show_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        await _reply_or_edit(update, "This section is only available to admins.", _home_button())
        return
    buttons = [
        [InlineKeyboardButton("🧹 Clean temp folders", callback_data="action|clean_tmp")],
        [InlineKeyboardButton("🏠 Main menu", callback_data="action|home")],
    ]
    await _reply_or_edit(update, "⚙️ Admin", InlineKeyboardMarkup(buttons))


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_main_menu(update, context)


async def start_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    set_state(context.user_data, STATE_SEARCH)
    await update.message.reply_text("Type a title to search (TMDb).")


# English alias
async def search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start_search(update, context)


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reset_flow_state(context)
    mgr = context.bot_data.get("dl_manager")
    cancelled = 0
    if mgr and hasattr(mgr, "cancel_running"):
        cancelled = await mgr.cancel_running(update.effective_chat.id)
    msg = "Flow cancelled."
    if cancelled:
        msg += f" Cancelled {cancelled} running download(s)."
    await update.message.reply_text(msg, reply_markup=_home_button())


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
    await update.message.reply_text(msg, reply_markup=_home_button())


async def queue_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mgr = context.bot_data.get("dl_manager")
    if not mgr or not hasattr(mgr, "snapshot_by_content"):
        await update.message.reply_text("Queue is empty.", reply_markup=_home_button())
        return
    running, queued = await mgr.snapshot_by_content(update.effective_chat.id)
    text, markup = _format_queue_view(running, queued)
    if markup:
        buttons = list(markup.inline_keyboard)
        buttons.append([InlineKeyboardButton("🏠 Main menu", callback_data="action|home")])
        markup = InlineKeyboardMarkup(buttons)
    else:
        markup = _home_button()
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
    cancelled_total = running_cancelled + queued_cancelled
    note = (
        f"Cancelled {cancelled_total} download(s) for that title."
        if cancelled_total
        else "Item not found in your queue."
    )
    running, queued = await mgr.snapshot_by_content(update.effective_chat.id)
    text, markup = _format_queue_view(running, queued, note=note)
    if markup:
        buttons = list(markup.inline_keyboard)
        buttons.append([InlineKeyboardButton("🏠 Main menu", callback_data="action|home")])
        markup = InlineKeyboardMarkup(buttons)
    else:
        markup = _home_button()
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
    await show_main_menu(update, context)


async def handle_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    action = (query.data or "").split("|")[1] if "|" in (query.data or "") else ""
    if action == "home":
        await show_main_menu(update, context)
    elif action == "search":
        await query.message.edit_text("Add content\n\nChoose how you want to select the destination metadata.", reply_markup=_add_content_markup())
    elif action == "search_tmdb":
        set_state(context.user_data, STATE_SEARCH)
        await query.message.edit_text(
            "Type a title to search TMDb.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="action|search"), InlineKeyboardButton("🏠 Main menu", callback_data="action|home")]]),
        )
    elif action == "results":
        from app.telegram.handlers.search import render_results_message

        results = context.user_data.get("results_list") or []
        page = int(context.user_data.get("results_page") or 0)
        await render_results_message(query.message, results, page)
    elif action == "categories":
        from app.telegram.handlers.search import _format_item_preview, build_category_keyboard

        pending = context.user_data.get("pending_show") or {}
        results = context.user_data.get("results_map") or {}
        item = results.get(f"{pending.get('kind')}:{pending.get('id')}")
        if item:
            text = f"{_format_item_preview(item)}\n\nSelect a library type."
        else:
            title = pending.get("label") or "Selected title"
            year = f" ({pending.get('year')})" if pending.get("year") else ""
            text = f"{title}{year}\n\nSelect a library type."
        await query.message.edit_text(text, reply_markup=build_category_keyboard())
    elif action == "cart":
        await show_cart(update, context)
    elif action == "queue":
        mgr = context.bot_data.get("dl_manager")
        if not mgr or not hasattr(mgr, "snapshot_by_content"):
            await query.message.edit_text("The queue is empty.", reply_markup=_home_button())
            return
        running, queued = await mgr.snapshot_by_content(update.effective_chat.id)
        text, markup = _format_queue_view(running, queued)
        if markup:
            buttons = list(markup.inline_keyboard)
            buttons.append([InlineKeyboardButton("🏠 Main menu", callback_data="action|home")])
            markup = InlineKeyboardMarkup(buttons)
        else:
            markup = _home_button()
        await query.message.edit_text(text, reply_markup=markup)
    elif action == "recent":
        await show_recent(update, context)
    elif action == "dbsearch":
        set_state(context.user_data, STATE_DB_SEARCH)
        await query.message.edit_text("Type a title fragment to search the local library.", reply_markup=_home_button())
    elif action == "dbstats":
        if not _is_admin(update):
            await query.message.edit_text("This action is only available to admins.", reply_markup=_home_button())
            return
        await db_stats_view(update, context)
    elif action == "admin":
        await show_admin(update, context)
    elif action == "scan":
        await query.message.edit_text("Library scanning is disabled for now. Recently added content is tracked from completed downloads.", reply_markup=_home_button())
    elif action == "clean_tmp":
        if not _is_admin(update):
            await query.message.edit_text("This action is only available to admins.", reply_markup=_home_button())
            return
        await clean_tmp(update, context)
    else:
        await query.message.reply_text("Unknown action.")


async def handle_cart_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    parts = (query.data or "").split("|")
    action = parts[1] if len(parts) > 1 else ""
    if action == "assign":
        cart = context.chat_data.get("cart") or []
        if not cart:
            await query.message.edit_text("The inbox is empty.", reply_markup=_home_button())
            return
        first_name = next((item.get("filename") for item in cart if item.get("filename")), None)
        if first_name:
            from app.services.tmdb_client import tmdb_last_error, tmdb_search
            from app.telegram.handlers.download import _guess_title_from_filename
            from app.telegram.handlers.search import build_results_keyboard

            guess = _guess_title_from_filename(first_name)
            results = tmdb_search(guess)
            context.user_data["results_list"] = results
            context.user_data["results_map"] = {f"{r.type}:{r.id}": r for r in results}
            context.user_data["results_page"] = 0
            if results:
                await query.message.edit_text(
                    f"Suggestion from inbox: {first_name}\nSelect the title, or type another search.",
                    reply_markup=build_results_keyboard(results, 0),
                )
                return
            err = tmdb_last_error()
            note = f"\nTMDb: {err}" if err else ""
            set_state(context.user_data, STATE_SEARCH)
            await query.message.edit_text(
                f"No results found for '{guess}'.{note}\nType a title to search manually.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="action|cart"), InlineKeyboardButton("🏠 Main menu", callback_data="action|home")]]),
            )
            return
        set_state(context.user_data, STATE_SEARCH)
        await query.message.edit_text(
            "Type a title to choose a destination for the whole inbox.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="action|cart"), InlineKeyboardButton("🏠 Main menu", callback_data="action|home")]]),
        )
    elif action == "page" and len(parts) == 3:
        try:
            page = int(parts[2])
        except ValueError:
            page = 0
        text, markup = _format_cart(context, page)
        await query.message.edit_text(text, reply_markup=markup)
    elif action == "remove" and len(parts) == 3:
        try:
            idx = int(parts[2])
        except ValueError:
            await query.answer("Invalid item.", show_alert=True)
            return
        cart = context.chat_data.get("cart") or []
        if 0 <= idx < len(cart):
            cart.pop(idx)
            if cart:
                context.chat_data["cart"] = cart
                text, markup = _format_cart(context)
            else:
                context.chat_data.pop("cart", None)
                text, markup = _format_cart(context, 0)
            await query.message.edit_text(text, reply_markup=markup)
        else:
            await query.answer("That item no longer exists.", show_alert=True)
    elif action == "clear":
        context.chat_data.pop("cart", None)
        await query.message.edit_text("Inbox cleared.", reply_markup=_home_button())
    else:
        await show_cart(update, context)


async def db_stats_view(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        with get_session() as s:
            totals = s.query(Show.kind, func.count()).group_by(Show.kind).all()
            seasons = s.scalar(s.query(func.count()).select_from(Season)) or 0
            shows_total = s.scalar(s.query(func.count()).select_from(Show)) or 0
    except OperationalError:
        await _reply_or_edit(update, "DB is not initialized. Run init-db, seed-libs, and then a library scan.", _home_button())
        return
    except Exception as e:
        await _reply_or_edit(update, f"DB stats failed: {e}", _home_button())
        return
    lines = ["📊 DB stats", f"Titles: {shows_total}", f"Seasons: {seasons}"]
    for kind, count in totals:
        lines.append(f"- {kind}: {count}")
    await _reply_or_edit(update, "\n".join(lines), _home_button())


async def clean_tmp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        await _reply_or_edit(update, "This action is only available to admins.", _home_button())
        return

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
    await _reply_or_edit(update, msg, _home_button())


async def scan_libraries(update: Update, context: ContextTypes.DEFAULT_TYPE, from_callback: bool = False):
    if not _is_admin(update):
        await _reply_or_edit(update, "This action is only available to admins.", _home_button())
        return

    await _reply_or_edit(update, "Library scanning is disabled for now. Recently added content is tracked from completed downloads.", _home_button())
    return

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

    try:
        stats = await asyncio.to_thread(_run_scan)
        await msg.edit_text(
            f"Scan done: +{stats.shows_new} shows, +{stats.seasons_new} seasons, +{stats.episodes_new} episodes "
            f"(files seen: {stats.files_seen})",
            reply_markup=_home_button(),
        )
    except Exception as e:
        logging.exception("Scan failed")
        err_text = f"Scan failed: {e}"
        try:
            await msg.edit_text(err_text, reply_markup=_home_button())
        except Exception:
            await msg.reply_text(err_text, reply_markup=_home_button())
