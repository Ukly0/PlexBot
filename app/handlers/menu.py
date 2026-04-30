"""Menu navigation: /start, /menu, dashboard, queue view, admin."""

import asyncio
import logging
import os
import shutil
import tempfile
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from app.config import load_settings
from app.state import reset_flow_state


def _home_button() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🏠 Main menu", callback_data="action|home")]]
    )


def _is_admin(update: Update) -> bool:
    admin = load_settings().admin_chat_id
    if not admin:
        return True
    chat = update.effective_chat
    return bool(chat and str(chat.id) == str(admin))


def _shorten(text: str, limit: int = 42) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


async def _reply_or_edit(update: Update, text: str, reply_markup=None):
    query = update.callback_query
    if query and query.message:
        try:
            if query.message.photo:
                await query.message.edit_caption(caption=text, reply_markup=reply_markup)
            else:
                await query.message.edit_text(text, reply_markup=reply_markup)
            return
        except Exception:
            await query.message.reply_text(text, reply_markup=reply_markup)
            return
    if update.message:
        await update.message.reply_text(text, reply_markup=reply_markup)


async def _edit_query_message(query, text: str, reply_markup=None):
    if query.message.photo:
        await query.message.edit_caption(caption=text, reply_markup=reply_markup)
    else:
        await query.message.edit_text(text, reply_markup=reply_markup)


def _main_menu_markup(is_admin: bool) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton("🔍 Search TMDb", callback_data="action|search")],
        [
            InlineKeyboardButton("⏳ Queue", callback_data="action|queue"),
        ],
    ]
    recent = [
        InlineKeyboardButton("🆕 Recent", callback_data="action|recent"),
    ]
    buttons.append(recent)
    if is_admin:
        buttons.append(
            [InlineKeyboardButton("⚙️ Admin", callback_data="action|admin")]
        )
    buttons.append(
        [InlineKeyboardButton("❌ Cancel flow", callback_data="cancel|flow")]
    )
    return InlineKeyboardMarkup(buttons)


# ── Commands ─────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "PlexBot\n\n"
        "Forward Telegram links or files to add them.\n"
        "The bot will search TMDb automatically."
    )
    await _reply_or_edit(
        update, text, _main_menu_markup(_is_admin(update))
    )


async def menu_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)


async def search_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from app.state import set_state, STATE_SEARCH

    set_state(context.user_data, STATE_SEARCH)
    await update.message.reply_text("Type a title to search TMDb.")


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reset_flow_state(context)
    mgr = context.bot_data.get("dl_manager")
    cancelled = 0
    if mgr and hasattr(mgr, "cancel_running"):
        cancelled = await mgr.cancel_running(update.effective_chat.id)
    msg = "Flow cancelled."
    if cancelled:
        msg += f" Cancelled {cancelled} running download(s)."
    await update.message.reply_text(msg, reply_markup=_home_button())


async def cancel_all_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reset_flow_state(context)
    mgr = context.bot_data.get("dl_manager")
    r, q = 0, 0
    if mgr and hasattr(mgr, "cancel_all"):
        r, q = await mgr.cancel_all(update.effective_chat.id)
    msg = "Cancelled flow and all downloads."
    if r or q:
        msg += f" Stopped {r} running and cleared {q} queued."
    await update.message.reply_text(msg, reply_markup=_home_button())


async def queue_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mgr = context.bot_data.get("dl_manager")
    if not mgr or not hasattr(mgr, "snapshot_by_content"):
        await update.message.reply_text("Queue is empty.", reply_markup=_home_button())
        return

    running, queued = await mgr.snapshot_by_content(update.effective_chat.id)
    lines: list[str] = []
    buttons: list[list[InlineKeyboardButton]] = []

    if running:
        dest = Path(running.destination).name or running.destination
        pending = f" (+{running.pending} pending)" if running.pending else ""
        lines.append(f"▶️ {running.label}{pending} → {dest}")
        buttons.append(
            [
                InlineKeyboardButton(
                    f"❌ Cancel {_shorten(running.label)}",
                    callback_data=f"cancel_task|{running.representative_task_id}",
                )
            ]
        )

    if queued:
        lines.append("⏳ In queue:")
        for idx, item in enumerate(queued, start=1):
            dest = Path(item.destination).name or item.destination
            count = f" ({item.total} pending)" if item.total > 1 else ""
            lines.append(f"{idx}. {item.label}{count} → {dest}")
            buttons.append(
                [
                    InlineKeyboardButton(
                        f"❌ Cancel {_shorten(item.label)}",
                        callback_data=f"cancel_task|{item.representative_task_id}",
                    )
                ]
            )

    if not running and not queued:
        lines.append("Queue is empty.")
        markup = _home_button()
    else:
        buttons.append(
            [InlineKeyboardButton("🏠 Main menu", callback_data="action|home")]
        )
        markup = InlineKeyboardMarkup(buttons)

    await update.message.reply_text("\n".join(lines), reply_markup=markup)


async def queue_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    parts = (query.data or "").split("|")
    if len(parts) != 2:
        return
    try:
        task_id = int(parts[1])
    except ValueError:
        return

    mgr = context.bot_data.get("dl_manager")
    if not mgr:
        return
    r, q = await mgr.cancel_task(update.effective_chat.id, task_id)
    note = f"Cancelled {r + q} download(s)." if r + q else "Item not found."
    await query.message.reply_text(note, reply_markup=_home_button())


async def show_recent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    entries = (
        context.bot_data.get("recent_destinations", {}).get(
            update.effective_chat.id, []
        )
        or []
    )
    if not entries:
        await _reply_or_edit(
            update,
            "No recently added content yet. Completed downloads will appear here.",
            _home_button(),
        )
        return

    lines = ["🆕 Recently added", ""]
    for e in entries:
        lib_name = (e.get("library") or {}).get("name", "")
        season = e.get("season")
        detail = f" - Season {season}" if season else ""
        dest = f" · {lib_name}" if lib_name else ""
        lines.append(f"- {e.get('title', '?')}{detail}{dest}")

    await _reply_or_edit(update, "\n".join(lines), _home_button())


async def clean_tmp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        await _reply_or_edit(update, "Admins only.", _home_button())
        return

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
        msg += f" Failed to remove {failed}."
    context.chat_data.pop("auto_tmp", None)
    context.user_data.pop("auto_pending_id", None)
    await _reply_or_edit(update, msg, _home_button())


async def show_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        await _reply_or_edit(update, "Admins only.", _home_button())
        return
    buttons = [
        [
            InlineKeyboardButton(
                "🧹 Clean temp folders", callback_data="action|clean_tmp"
            )
        ],
        [InlineKeyboardButton("🏠 Main menu", callback_data="action|home")],
    ]
    await _reply_or_edit(update, "⚙️ Admin", InlineKeyboardMarkup(buttons))


# ── Inline action router ─────────────────────────────────────────

async def handle_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    action = (
        (query.data or "").split("|")[1] if "|" in (query.data or "") else ""
    )

    if action == "home":
        await start(update, context)
    elif action == "search":
        from app.state import set_state, STATE_SEARCH

        set_state(context.user_data, STATE_SEARCH)
        await _edit_query_message(
            query,
            "Type a title to search TMDb.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🏠 Main menu", callback_data="action|home")]]
            ),
        )
    elif action == "queue":
        mgr = context.bot_data.get("dl_manager")
        if not mgr or not hasattr(mgr, "snapshot_by_content"):
            await _edit_query_message(
                query, "Queue is empty.", reply_markup=_home_button()
            )
            return
        running, queued = await mgr.snapshot_by_content(update.effective_chat.id)
        lines = []
        buttons = []
        if running:
            lines.append(f"▶️ {running.label} → {Path(running.destination).name}")
            buttons.append(
                [
                    InlineKeyboardButton(
                        f"❌ Cancel {_shorten(running.label)}",
                        callback_data=f"cancel_task|{running.representative_task_id}",
                    )
                ]
            )
        if queued:
            lines.append("⏳ Queued:")
            for idx, item in enumerate(queued, start=1):
                lines.append(
                    f"{idx}. {item.label} → {Path(item.destination).name}"
                )
                buttons.append(
                    [
                        InlineKeyboardButton(
                            f"❌ Cancel {_shorten(item.label)}",
                            callback_data=f"cancel_task|{item.representative_task_id}",
                        )
                    ]
                )
        if not running and not queued:
            lines.append("Queue is empty.")
            markup = _home_button()
        else:
            buttons.append(
                [InlineKeyboardButton("🏠 Main menu", callback_data="action|home")]
            )
            markup = InlineKeyboardMarkup(buttons)
        await _edit_query_message(query, "\n".join(lines), reply_markup=markup)
    elif action == "recent":
        await show_recent(update, context)
    elif action == "admin":
        await show_admin(update, context)
    elif action == "clean_tmp":
        await clean_tmp(update, context)
    elif action == "continue_batch":
        # Process all pending links to the current destination
        pending: list = context.chat_data.get("pending_links", [])
        download_dir = context.chat_data.get("download_dir")
        title = context.user_data.get("pending_title") or "Content"
        season = context.chat_data.get("season_hint")
        year = context.user_data.get("pending_year")
        if not pending or not download_dir:
            await query.message.reply_text("Nothing to queue.", reply_markup=_home_button())
            return
        from app.handlers.download import queue_download

        await query.message.edit_text(f"Queuing {len(pending)} item(s) for '{title}'...")
        for item in pending:
            await queue_download(
                query.message, context, item["link"],
                download_dir, title, season, year,
                item.get("filename") or item["link"],
            )
        context.chat_data["pending_links"] = []
        active_lib = context.chat_data.get("active_library") or {}
        if active_lib.get("type") not in {"series", "anime"}:
            context.chat_data.pop("download_dir", None)
            context.chat_data.pop("active_library", None)
            context.chat_data.pop("season_hint", None)
            context.user_data.pop("pending_title", None)
            context.user_data.pop("pending_year", None)
    elif action == "new_search":
        # Keep the pending links, clear destination, trigger auto-detect on them
        pending: list = context.chat_data.get("pending_links", [])
        context.chat_data.pop("download_dir", None)
        context.chat_data.pop("active_library", None)
        context.chat_data.pop("season_hint", None)
        context.user_data.pop("pending_title", None)
        context.user_data.pop("pending_year", None)
        context.user_data.pop("state", None)

        if not pending:
            await query.message.edit_text(
                "Destination cleared. Forward a link or file to start a new search.",
                reply_markup=_home_button(),
            )
            return

        # Use the first pending item's filename to trigger auto-detection
        first = pending[0]
        guess = None
        fname = first.get("filename")
        if fname:
            from app.handlers.ingest import _guess_title, _is_meaningful
            guess = _guess_title(fname)

        if guess and _is_meaningful(guess):
            from app.services.tmdb import search as tmdb_search, tmdb_last_error
            from app.handlers.search import build_results_keyboard

            results = tmdb_search(guess)
            context.user_data["tmdb_results"] = results
            context.user_data["tmdb_page"] = 0
            context.user_data["state"] = "pending_selection"

            if results:
                first_result = results[0]
                markup = build_results_keyboard(results, 0)
                if first_result.poster:
                    await query.message.delete()
                    await context.bot.send_photo(
                        chat_id=update.effective_chat.id,
                        photo=first_result.poster,
                        caption=f"Detected: {guess}\nSelect the matching title:",
                        reply_markup=markup,
                    )
                else:
                    await query.message.edit_text(
                        f"Detected: {guess}\nSelect the matching title:",
                        reply_markup=markup,
                    )
            else:
                from app.state import STATE_SEARCH, set_state
                set_state(context.user_data, STATE_SEARCH)
                await query.message.edit_text(
                    f"Detected: {guess}\nNo TMDb results. Type a title to search.",
                )
        else:
            from app.state import STATE_SEARCH, set_state
            set_state(context.user_data, STATE_SEARCH)
            hint = f"(from: {fname})" if fname else ""
            await query.message.edit_text(
                f"New search {hint}\nType a title to search.",
            )
    else:
        await query.message.reply_text("Unknown action.")
