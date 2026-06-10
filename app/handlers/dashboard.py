"""Live pinned dashboard — one status message per chat, edited in place.

Download progress no longer spams the chat: a single pinned message shows
the running download (with bar, speed, ETA), a queue summary, and the last
completed item. Created on /start or lazily on the first enqueue.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from app.handlers.telegram_utils import edit_message_safely, safe_answer

# Telegram allows ~1 edit/sec per chat; progress arrives faster than that.
MIN_EDIT_INTERVAL = 4.0
BAR_LEN = 16
RECENT_MAX = 3


def _dash_store(context) -> dict:
    return context.bot_data.setdefault("dashboards", {})


def _live_store(context) -> dict:
    return context.bot_data.setdefault("dash_live", {})


def set_live(context, chat_id: int, **fields) -> None:
    """Update the live-progress fields shown on the dashboard."""
    _live_store(context).setdefault(chat_id, {}).update(fields)


def clear_live(context, chat_id: int) -> None:
    _live_store(context).pop(chat_id, None)


def push_recent(context, chat_id: int, line: str) -> None:
    items: list = context.bot_data.setdefault("dash_recent", {}).setdefault(chat_id, [])
    items.insert(0, line)
    del items[RECENT_MAX:]


def _bar(pct: int) -> str:
    filled = int(BAR_LEN * pct / 100)
    return "█" * filled + "░" * (BAR_LEN - filled)


async def _render(context, chat_id: int) -> tuple[str, InlineKeyboardMarkup]:
    mgr = context.bot_data.get("dl_manager")
    running, queued = (None, [])
    if mgr and hasattr(mgr, "snapshot_by_content"):
        running, queued = await mgr.snapshot_by_content(chat_id)
    live = _live_store(context).get(chat_id) or {}

    lines = ["🎬 PlexBot — Status"]
    if running:
        label = live.get("label") or running.label
        batch = ""
        if live.get("batch_total"):
            batch = f" ({live.get('batch_index', '?')}/{live['batch_total']})"
        lines.append(f"▶️ {label}{batch}")
        pct = live.get("pct")
        if pct is not None:
            extras = " · ".join(
                part for part in (
                    live.get("speed"),
                    f"ETA {live['eta']}" if live.get("eta") else None,
                ) if part
            )
            lines.append(f"[{_bar(pct)}] {pct}%" + (f" · {extras}" if extras else ""))
        else:
            lines.append("Preparing…")
        dest = Path(running.destination).name
        if dest:
            lines.append(f"→ {dest}")
    else:
        lines.append("▫️ Idle — send a link or file.")

    if queued:
        parts = []
        for item in queued[:3]:
            count = f" ({item.total})" if item.total > 1 else ""
            parts.append(f"{item.label}{count}")
        more = f" +{len(queued) - 3} more" if len(queued) > 3 else ""
        lines.append("⏳ Queue: " + " · ".join(parts) + more)

    recents = context.bot_data.get("dash_recent", {}).get(chat_id) or []
    if recents:
        lines.append(f"✅ Last: {recents[0]}")

    buttons = [
        [
            InlineKeyboardButton("🔍 Search", callback_data="dash|search"),
            InlineKeyboardButton("📋 Queue", callback_data="dash|queue"),
        ]
    ]
    if running:
        buttons.append(
            [InlineKeyboardButton("❌ Cancel current", callback_data="dash|cancel")]
        )
    return "\n".join(lines), InlineKeyboardMarkup(buttons)


async def ensure_dashboard(context, chat_id: int) -> None:
    """Create and pin the dashboard message if this chat has none yet."""
    store = _dash_store(context)
    if chat_id in store:
        return
    text, markup = await _render(context, chat_id)
    try:
        msg = await context.bot.send_message(chat_id, text, reply_markup=markup)
    except Exception as e:
        logging.warning("Could not create dashboard for %s: %s", chat_id, e)
        return
    store[chat_id] = {
        "message_id": msg.message_id,
        "last_edit": time.monotonic(),
        "last_text": text,
    }
    try:
        await context.bot.pin_chat_message(
            chat_id, msg.message_id, disable_notification=True
        )
    except Exception as e:
        # Pinning needs admin rights in groups; the dashboard works unpinned.
        logging.debug("Could not pin dashboard in %s: %s", chat_id, e)


async def update_dashboard(context, chat_id: int, *, force: bool = False) -> None:
    store = _dash_store(context)
    entry = store.get(chat_id)
    if not entry:
        return
    now = time.monotonic()
    if not force and now - entry.get("last_edit", 0.0) < MIN_EDIT_INTERVAL:
        return
    text, markup = await _render(context, chat_id)
    if text == entry.get("last_text"):
        return
    entry["last_edit"] = now
    try:
        await context.bot.edit_message_text(
            text,
            chat_id=chat_id,
            message_id=entry["message_id"],
            reply_markup=markup,
        )
        entry["last_text"] = text
    except Exception as e:
        err = str(e).lower()
        if "not modified" in err:
            entry["last_text"] = text
            return
        if "message to edit not found" in err or "message_id_invalid" in err:
            # Dashboard was deleted by a user; recreate on next ensure.
            store.pop(chat_id, None)
            return
        logging.debug("Dashboard edit failed for %s: %s", chat_id, e)


async def handle_dash(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await safe_answer(query)
    data = query.data or ""
    action = data.split("|", 1)[1] if "|" in data else ""
    chat_id = update.effective_chat.id

    if action == "search":
        from app.state import set_state, STATE_SEARCH

        set_state(context.user_data, STATE_SEARCH)
        await context.bot.send_message(chat_id, "Type a title to search TMDb.")
    elif action == "queue":
        from app.handlers.menu import build_queue_view

        mgr = context.bot_data.get("dl_manager")
        text, markup = await build_queue_view(mgr, chat_id)
        await context.bot.send_message(chat_id, text, reply_markup=markup)
    elif action == "cancel":
        markup = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("✅ Yes, cancel", callback_data="dash|cancel_yes"),
                    InlineKeyboardButton("↩️ Keep going", callback_data="dash|cancel_no"),
                ]
            ]
        )
        await context.bot.send_message(
            chat_id, "Cancel the current download?", reply_markup=markup
        )
    elif action == "cancel_yes":
        mgr = context.bot_data.get("dl_manager")
        cancelled = 0
        if mgr and hasattr(mgr, "cancel_running"):
            cancelled = await mgr.cancel_running(chat_id)
        note = "Cancelled the running download." if cancelled else "Nothing is downloading."
        await edit_message_safely(query.message, note)
        clear_live(context, chat_id)
        await update_dashboard(context, chat_id, force=True)
    elif action == "cancel_no":
        await edit_message_safely(query.message, "Download continues.")
