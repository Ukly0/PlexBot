from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes
from sqlalchemy import func

from bot.db import get_session
from store.models import Show, Season

PAGE_SIZE = 5


def _format_show(show):
    year = f" ({show.year})" if getattr(show, "year", None) else ""
    return f"{show.title}{year} [{show.kind}]"


def _build_results(shows, page: int = 0):
    start = page * PAGE_SIZE
    items = shows[start : start + PAGE_SIZE]
    buttons = []
    row = []
    for s in items:
        label = _format_show(s)
        row.append(InlineKeyboardButton(label[:64], callback_data="noop"))
        if len(row) == 1:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    total_pages = (len(shows) - 1) // PAGE_SIZE if shows else 0
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️", callback_data=f"dbpage|{page-1}"))
    if page < total_pages:
        nav.append(InlineKeyboardButton("➡️", callback_data=f"dbpage|{page+1}"))
    if nav:
        buttons.append(nav)
    return InlineKeyboardMarkup(buttons) if buttons else None


async def db_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = (update.message.text or "").split(maxsplit=1)
    if len(args) < 2:
        await update.message.reply_text("Usage: /dbsearch <title fragment>")
        return
    q = args[1].strip()
    with get_session() as s:
        rows = (
            s.query(Show)
            .filter(func.lower(Show.title).like(f"%{q.lower()}%"))
            .order_by(Show.title)
            .all()
        )
    if not rows:
        await update.message.reply_text("No matches in DB.")
        return
    context.user_data["db_results"] = rows
    context.user_data["db_page"] = 0
    kb = _build_results(rows, 0)
    await update.message.reply_text("DB results:", reply_markup=kb)


async def db_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    parts = (query.data or "").split("|")
    if len(parts) != 2:
        return
    _, page_raw = parts
    try:
        page = int(page_raw)
    except ValueError:
        return
    shows = context.user_data.get("db_results") or []
    if not shows:
        await query.edit_message_text("No results loaded. Run /dbsearch first.")
        return
    kb = _build_results(shows, page)
    context.user_data["db_page"] = page
    await query.edit_message_text("DB results:", reply_markup=kb)


async def db_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with get_session() as s:
        totals = s.query(Show.kind, func.count()).group_by(Show.kind).all()
        seasons = s.scalar(s.query(func.count()).select_from(Season)) or 0
        shows_total = s.scalar(s.query(func.count()).select_from(Show)) or 0
    lines = ["DB stats:", f"Shows: {shows_total}", f"Seasons: {seasons}"]
    for kind, count in totals:
        lines.append(f"- {kind}: {count}")
    await update.message.reply_text("\n".join(lines))
