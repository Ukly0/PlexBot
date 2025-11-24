from typing import List

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from bot.state import (
    STATE_MANUAL_SEASON,
    STATE_MANUAL_TITLE,
    STATE_SEARCH,
    set_state,
)
from core.tmdb import TMDbItem, TMDbSeason, tmdb_search, tmdb_seasons, tmdb_last_error
from .download import finalize_selection, handle_download_message

PAGE_SIZE = 5
SEASON_TYPES = {"series", "anime", "docuseries"}


def build_results_keyboard(results: List[TMDbItem], page: int = 0) -> InlineKeyboardMarkup:
    start = page * PAGE_SIZE
    slice_items = results[start : start + PAGE_SIZE]
    buttons: list[list[InlineKeyboardButton]] = []

    row: list[InlineKeyboardButton] = []
    for item in slice_items:
        label = f"{item.title} ({item.year})" if item.year else item.title
        row.append(InlineKeyboardButton(label[:64], callback_data=f"tmdb|{item.type}|{item.id}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    pagination = []
    total_pages = (len(results) - 1) // PAGE_SIZE if results else 0
    if page > 0:
        pagination.append(InlineKeyboardButton("⬅️", callback_data=f"page|{page-1}"))
    if page < total_pages:
        pagination.append(InlineKeyboardButton("➡️", callback_data=f"page|{page+1}"))
    if pagination:
        buttons.append(pagination)

    buttons.append([InlineKeyboardButton("✍️ Manual entry", callback_data="manual|start")])
    return InlineKeyboardMarkup(buttons)


def build_season_keyboard(seasons: List[TMDbSeason]) -> InlineKeyboardMarkup:
    buttons: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for s in seasons[:24]:
        row.append(InlineKeyboardButton(f"Season {s.season_number}", callback_data=f"season|{s.season_number}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton("🔢 Other season", callback_data="season|manual")])
    buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel|flow")])
    return InlineKeyboardMarkup(buttons)


def build_category_keyboard() -> InlineKeyboardMarkup:
    opts = [
        ("📺 Series", "series"),
        ("✨ Anime", "anime"),
        ("🎞️ Docuseries", "docuseries"),
        ("🎥 Documentary", "documentary"),
        ("🎬 Movie", "movies"),
    ]
    buttons: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for label, val in opts:
        row.append(InlineKeyboardButton(label, callback_data=f"cat|{val}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel|flow")])
    return InlineKeyboardMarkup(buttons)


async def _send_poster(query, item: TMDbItem):
    title = f"{item.title} ({item.year})" if item.year else item.title
    rating = f"⭐ {item.rating:.1f}/10" if item.rating else ""
    overview = (item.overview or "").strip()
    if len(overview) > 240:
        overview = overview[:237].rstrip() + "..."
    lines = [title]
    if rating:
        lines.append(rating)
    if overview:
        lines.append(overview)
    caption = "\n".join(lines)
    try:
        if item.poster:
            await query.message.reply_photo(item.poster, caption=caption)
            return
    except Exception:
        pass
    await query.message.reply_text(caption)


async def render_results(target, results: List[TMDbItem], page: int):
    if not results:
        await target.reply_text("No results. Type another title or use manual entry.")
        return
    await target.reply_text("Results:", reply_markup=build_results_keyboard(results, page))


async def handle_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
    results: List[TMDbItem] = context.user_data.get("results_list") or []
    if not results:
        await query.edit_message_text("No results loaded. Please run /buscar again.")
        return
    await query.edit_message_text("Results:", reply_markup=build_results_keyboard(results, page))
    context.user_data["results_page"] = page


async def handle_tmdb_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    parts = (query.data or "").split("|")
    if len(parts) != 3:
        await query.edit_message_text("Invalid selection.")
        return
    _, kind, id_raw = parts
    key = f"{kind}:{id_raw}"
    item = (context.user_data.get("results_map") or {}).get(key)
    if not item:
        await query.edit_message_text("Item not found. Please search again.")
        return

    context.user_data["pending_show"] = {
        "id": item.id,
        "kind": kind,
        "label": item.title,
        "year": item.year,
        "title_en": item.title,  # TMDb ya entrega en idioma original
    }
    await _send_poster(query, item)
    info = f"{item.title} ({item.year})" if item.year else item.title
    await query.message.reply_text(
        f"{info}\nSelect a category to classify.",
        reply_markup=build_category_keyboard(),
    )


async def handle_category_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    parts = (query.data or "").split("|")
    if len(parts) != 2:
        return
    _, lib_type = parts
    pending = context.user_data.get("pending_show") or {}
    label = pending.get("label")
    year = pending.get("year")
    title_en = pending.get("title_en")
    label_fmt = f"{label} ({year})" if year else label

    if lib_type in SEASON_TYPES:
        seasons = tmdb_seasons(pending.get("id")) if pending else []
        if seasons:
            await query.message.reply_text(
                f"{label_fmt}\nChoose a season:",
                reply_markup=build_season_keyboard(seasons),
            )
            context.user_data["pending_manual_type"] = lib_type
            return
        await query.message.reply_text(
            f"{label_fmt}\nCould not fetch seasons. Type the season number or cancel.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel|flow")]]),
        )
        context.user_data["pending_manual_type"] = lib_type
        set_state(context.user_data, STATE_MANUAL_SEASON)
        return

    await finalize_selection(update, context, lib_type=lib_type, season=None, label=label_fmt, original_title_en=title_en, year=year)


async def handle_season_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    parts = (query.data or "").split("|")
    if len(parts) != 2:
        return
    _, val = parts
    if val == "manual":
        await query.message.reply_text("Type the season number.")
        set_state(context.user_data, STATE_MANUAL_SEASON)
        context.user_data["pending_manual_type"] = context.user_data.get("pending_manual_type", "series")
        return
    try:
        season_num = int(val)
    except ValueError:
        await query.message.reply_text("Invalid season.")
        return
    pending = context.user_data.get("pending_show") or {}
    label = pending.get("label")
    year = pending.get("year")
    title_en = pending.get("title_en")
    label_fmt = f"{label} ({year})" if year else label
    lib_type = context.user_data.get("pending_manual_type", "series")
    await finalize_selection(update, context, lib_type=lib_type, season=season_num, label=label_fmt, original_title_en=title_en, year=year)


async def handle_manual_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("No match. Type the title manually.")
    set_state(context.user_data, STATE_MANUAL_TITLE)


async def handle_manual_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    parts = (query.data or "").split("|")
    if len(parts) != 2:
        return
    _, lib_type = parts
    title = context.user_data.get("manual_title") or "Title"
    if lib_type in SEASON_TYPES:
        context.user_data["pending_manual_type"] = lib_type
        await query.edit_message_text(f"{title}\nType the season number.")
        set_state(context.user_data, STATE_MANUAL_SEASON)
    else:
        await finalize_selection(update, context, lib_type=lib_type, season=None, label=title, original_title_en=title)


async def handle_cancel_flow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data.clear()
    context.chat_data.pop("tdl_extra_flags", None)
    context.chat_data.pop("download_dir", None)
    await query.message.reply_text("Flow cancelled. Use /buscar to start over.")


async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    awaiting = context.user_data.get("awaiting")

    if awaiting == STATE_SEARCH:
        set_state(context.user_data, None)
        results = tmdb_search(text)
        context.user_data["results_list"] = results
        context.user_data["results_map"] = {f"{r.type}:{r.id}": r for r in results}
        context.user_data["results_page"] = 0
        if results:
            await render_results(update.message, results, 0)
        else:
            err = tmdb_last_error()
            if err:
                await update.message.reply_text(f"TMDb query failed: {err}")
            await update.message.reply_text(
                "No results. Type another title or use manual entry.", reply_markup=build_category_keyboard()
            )
            context.user_data["manual_title"] = text
        return

    if awaiting == STATE_MANUAL_TITLE:
        context.user_data["manual_title"] = text
        set_state(context.user_data, None)
        await update.message.reply_text("Choose library type:", reply_markup=build_category_keyboard())
        return

    if awaiting == STATE_MANUAL_SEASON:
        try:
            season_num = int(text)
        except ValueError:
            await update.message.reply_text("Enter a valid season number.")
            return
        lib_type = context.user_data.get("pending_manual_type", "series")
        title = context.user_data.get("manual_title") or context.user_data.get("pending_show", {}).get("label", "Title")
        set_state(context.user_data, None)
        await finalize_selection(update, context, lib_type=lib_type, season=season_num, label=title, original_title_en=title)
        return

    await handle_download_message(update, context)
