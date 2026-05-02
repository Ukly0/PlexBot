"""TMDb search flow: result selection, pagination, season pick, library choice."""

import asyncio

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from app.services.tmdb import (
    TMDbItem,
    search as tmdb_search,
    get_seasons,
    tmdb_last_error,
)
from app.state import (
    SERIES_TYPES,
    STATE_MANUAL_SEASON,
    STATE_MANUAL_TITLE,
    STATE_SEARCH,
    set_state,
)
from app.config import load_settings
from app.handlers.telegram_utils import (
    delete_safely,
    edit_message_safely,
    safe_answer,
)

PAGE_SIZE = 5


def _home_button() -> InlineKeyboardButton:
    return InlineKeyboardButton("🏠 Main menu", callback_data="action|home")


# ── Keyboards ────────────────────────────────────────────────────

def build_results_keyboard(
    results: list[TMDbItem], page: int = 0
) -> InlineKeyboardMarkup:
    start = page * PAGE_SIZE
    chunk = results[start : start + PAGE_SIZE]
    buttons: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for item in chunk:
        label = f"{item.title} ({item.year})" if item.year else item.title
        row.append(
            InlineKeyboardButton(
                label[:64], callback_data=f"tmdb|{item.kind}|{item.id}"
            )
        )
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    pagination = []
    total_pages = (len(results) - 1) // PAGE_SIZE if results else 0
    if page > 0:
        pagination.append(
            InlineKeyboardButton("⬅️", callback_data=f"page|{page - 1}")
        )
    if page < total_pages:
        pagination.append(
            InlineKeyboardButton("➡️", callback_data=f"page|{page + 1}")
        )
    if pagination:
        buttons.append(pagination)

    buttons.append(
        [
            InlineKeyboardButton("✍️ Manual entry", callback_data="manual|start"),
        ]
    )
    buttons.append(
        [
            InlineKeyboardButton("⬅️ Back", callback_data="action|search"),
            _home_button(),
        ]
    )
    return InlineKeyboardMarkup(buttons)


def build_season_keyboard(seasons: list) -> InlineKeyboardMarkup:
    buttons: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for s in seasons[:24]:
        num = s.season_number if hasattr(s, "season_number") else s.get("season_number")
        row.append(
            InlineKeyboardButton(f"Season {num}", callback_data=f"season|{num}")
        )
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append(
        [InlineKeyboardButton("🔢 Other season", callback_data="season|manual")]
    )
    buttons.append(
        [
            InlineKeyboardButton("⬅️ Back", callback_data="action|search"),
            _home_button(),
        ]
    )
    buttons.append(
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel|flow")]
    )
    return InlineKeyboardMarkup(buttons)


def build_library_keyboard() -> InlineKeyboardMarkup:
    st = load_settings()
    buttons: list[list[InlineKeyboardButton]] = []
    for lib in st.libraries:
        buttons.append(
            [
                InlineKeyboardButton(
                    lib.name, callback_data=f"lib|{lib.name}"
                )
            ]
        )
    buttons.append(
        [
            InlineKeyboardButton("⬅️ Back", callback_data="action|search"),
            _home_button(),
        ]
    )
    buttons.append(
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel|flow")]
    )
    return InlineKeyboardMarkup(buttons)


async def _edit_message(query, text: str, reply_markup=None):
    """Edit text or caption depending on whether the message is a photo."""
    await edit_message_safely(query.message, text, reply_markup=reply_markup)


# ── Formatting ───────────────────────────────────────────────────

def _format_item_preview(item: TMDbItem) -> str:
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
    return "\n".join(lines)


# ── Handlers ─────────────────────────────────────────────────────

async def handle_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await safe_answer(query)
    parts = (query.data or "").split("|")
    if len(parts) != 2:
        return
    try:
        page = int(parts[1])
    except ValueError:
        return
    results = context.user_data.get("tmdb_results") or []
    context.user_data["tmdb_page"] = page
    if not results:
        await _edit_message(query, "No results. Search again.")
        return
    await _edit_message(query, 
        "Results:", reply_markup=build_results_keyboard(results, page)
    )


async def handle_tmdb_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await safe_answer(query)
    parts = (query.data or "").split("|")
    if len(parts) != 3:
        return
    _, kind, id_raw = parts
    item = None
    results: list[TMDbItem] = context.user_data.get("tmdb_results") or []
    try:
        item_id = int(id_raw)
    except ValueError:
        return
    for r in results:
        if r.kind == kind and r.id == item_id:
            item = r
            break
    if not item:
        await _edit_message(query, "Item not found. Search again.")
        return

    context.user_data["selected_tmdb"] = {
        "id": item.id,
        "kind": kind,
        "title": item.title,
        "year": item.year,
    }
    context.user_data["pending_title"] = item.title
    context.user_data["pending_year"] = item.year

    if kind == "tv":
        # Auto-detect if this show already has a folder in a series library
        from app.config import load_settings
        from app.handlers.download import find_existing_library

        st = load_settings()
        existing_lib = find_existing_library(item.title, item.year, st.libraries)

        seasons = await asyncio.to_thread(get_seasons, item.id)
        season_markup_buttons = []

        if existing_lib:
            # Store auto-detected library for handle_season to use
            context.user_data["auto_library"] = existing_lib
            # Show which library was auto-detected, with a "Change" option
            season_markup_buttons.append(
                [InlineKeyboardButton(
                    f"📁 {existing_lib['name']}",
                    callback_data=f"autolib|{existing_lib['name']}",
                )]
            )
        else:
            context.user_data.pop("auto_library", None)

        if seasons:
            row: list[InlineKeyboardButton] = []
            for s in seasons[:24]:
                row.append(
                    InlineKeyboardButton(f"Season {s.season_number}", callback_data=f"season|{s.season_number}")
                )
                if len(row) == 2:
                    season_markup_buttons.append(row)
                    row = []
            if row:
                season_markup_buttons.append(row)

        season_markup_buttons.append(
            [InlineKeyboardButton("🔢 Other season", callback_data="season|manual")]
        )
        if existing_lib:
            season_markup_buttons.append(
                [InlineKeyboardButton("📂 Change library", callback_data="action|search")]
            )
        season_markup_buttons.append(
            [
                InlineKeyboardButton("⬅️ Back", callback_data="action|search"),
                _home_button(),
            ]
        )
        season_markup_buttons.append(
            [InlineKeyboardButton("❌ Cancel", callback_data="cancel|flow")]
        )
        markup = InlineKeyboardMarkup(season_markup_buttons)

        text = f"{_format_item_preview(item)}\n\nChoose a season:"
        if existing_lib:
            text += f"\n📁 Auto-detected: {existing_lib['name']}"
        if not seasons:
            set_state(context.user_data, STATE_MANUAL_SEASON)
            text = f"{_format_item_preview(item)}\n\nType the season number:"
            if existing_lib:
                text += f"\n📁 Auto-detected: {existing_lib['name']}"

        if item.poster:
            await delete_safely(query.message)
            await context.bot.send_photo(
                chat_id=update.effective_chat.id,
                photo=item.poster,
                caption=text,
                reply_markup=markup,
            )
        else:
            await _edit_message(query, text, reply_markup=markup)
        return

    # Movie → auto-detect library or go to library selection
    from app.config import load_settings
    from app.handlers.download import find_existing_library
    from app.state import MOVIE_TYPES

    st = load_settings()
    existing_lib = find_existing_library(item.title, item.year, st.libraries, MOVIE_TYPES)

    if existing_lib:
        from app.handlers.download import set_destination, queue_download_batch

        lib_dict = {"name": existing_lib["name"], "root": existing_lib["root"], "type": existing_lib["type"]}
        context.user_data.pop("state", None)
        full_title = item.title
        if item.year:
            full_title = f"{item.title} ({item.year})"
        context.user_data["pending_title"] = full_title

        download_dir = await set_destination(update, context, lib_dict, item.title, item.year, None)

        pending_items: list = context.chat_data.pop("pending_links", [])
        if pending_items:
            await _edit_message(query, f"📁 Auto: {existing_lib['name']}\nQueuing {len(pending_items)} item(s)...")
            await queue_download_batch(
                query.message, context, pending_items,
                download_dir, full_title, None, item.year,
            )
            context.chat_data.pop("download_dir", None)
            context.chat_data.pop("active_library", None)
            context.chat_data.pop("season_hint", None)
            context.chat_data.pop("selected_type", None)
            context.user_data.pop("pending_title", None)
            context.user_data.pop("pending_year", None)
            context.user_data.pop("pending_season", None)
            context.user_data.pop("selected_tmdb", None)
        else:
            markup = InlineKeyboardMarkup([[_home_button()]])
            await _edit_message(query, f"📁 Auto: {existing_lib['name']}\n{download_dir}\n\nSend a link or file.", reply_markup=markup)
        return

    markup = build_library_keyboard()
    text = f"{_format_item_preview(item)}\n\nSelect destination library:"
    if item.poster:
        await delete_safely(query.message)
        await context.bot.send_photo(
            chat_id=update.effective_chat.id,
            photo=item.poster,
            caption=text,
            reply_markup=markup,
        )
    else:
        await _edit_message(query, text, reply_markup=markup)


async def handle_season(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await safe_answer(query)
    parts = (query.data or "").split("|")
    if len(parts) != 2:
        return

    val = parts[1]
    if val == "manual":
        await _edit_message(query, 
            "Type the season number.",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "⬅️ Back", callback_data="action|search"
                        ),
                        _home_button(),
                    ]
                ]
            ),
        )
        set_state(context.user_data, STATE_MANUAL_SEASON)
        return

    try:
        season_num = int(val)
    except ValueError:
        return

    context.user_data["pending_season"] = season_num
    sel = context.user_data.get("selected_tmdb") or {}
    title = sel.get("title") or context.user_data.get("pending_title", "Content")
    year = sel.get("year") or context.user_data.get("pending_year")
    kind = sel.get("kind", "movie")

    # If a series auto-library was detected, skip library selection
    auto_lib = context.user_data.pop("auto_library", None)
    if auto_lib:
        from app.handlers.download import set_destination, queue_download_batch

        full_title = title
        if year:
            full_title = f"{title} ({year})"
        context.user_data["pending_title"] = full_title
        if kind != "tv":
            context.user_data.pop("pending_season", None)

        download_dir = await set_destination(update, context, auto_lib, title, year, season_num)

        pending_items: list = context.chat_data.pop("pending_links", [])
        context.user_data.pop("state", None)

        if pending_items:
            await _edit_message(query, f"📁 {auto_lib['name']} — Season {season_num:02d}\nQueuing {len(pending_items)} item(s)...")
            await queue_download_batch(
                query.message, context, pending_items,
                download_dir, full_title, season_num, year,
            )
        else:
            markup = InlineKeyboardMarkup([[_home_button()]])
            await _edit_message(query, f"📁 {auto_lib['name']} — Season {season_num:02d}\n{download_dir}\n\nSend a link or file.", reply_markup=markup)
        return

    await _edit_message(query, 
        f"{title} — Season {season_num}\n\nSelect destination library:",
        reply_markup=build_library_keyboard(),
    )


async def handle_library(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await safe_answer(query)
    parts = (query.data or "").split("|")
    if len(parts) != 2:
        return
    lib_name = parts[1]

    st = load_settings()
    library = None
    for lib in st.libraries:
        if lib.name == lib_name:
            library = {"name": lib.name, "root": lib.root, "type": lib.type}
            break

    if not library:
        await _edit_message(query, f"Library '{lib_name}' not found.")
        return

    sel = context.user_data.get("selected_tmdb") or {}
    title = sel.get("title") or context.user_data.get("pending_title", "Content")
    year = sel.get("year") or context.user_data.get("pending_year")
    kind = sel.get("kind", "movie")
    season = context.user_data.get("pending_season") if kind == "tv" else None

    # Clear stale season for movies
    if kind != "tv":
        context.user_data.pop("pending_season", None)

    from app.handlers.download import set_destination, queue_download_batch

    # Build full title with year for display and naming consistency
    full_title = title
    if year:
        full_title = f"{title} ({year})"
    context.user_data["pending_title"] = full_title

    download_dir = await set_destination(
        update, context, library, title, year, season
    )

    pending_items: list = context.chat_data.pop("pending_links", [])
    context.user_data.pop("state", None)

    if pending_items:
        count = len(pending_items)
        await _edit_message(
            query,
            f"Destination: {download_dir}\nQueuing {count} item(s)..."
        )
        await queue_download_batch(
            query.message, context, pending_items,
            download_dir, full_title, season, year,
        )
        # For movies, clear destination after queuing so next file starts fresh
        if library.get("type") not in ("series", "anime"):
            context.chat_data.pop("download_dir", None)
            context.chat_data.pop("active_library", None)
            context.chat_data.pop("season_hint", None)
            context.chat_data.pop("selected_type", None)
            context.user_data.pop("pending_title", None)
            context.user_data.pop("pending_year", None)
            context.user_data.pop("pending_season", None)
            context.user_data.pop("selected_tmdb", None)
    else:
        await _edit_message(
            query,
            f"Destination set: {download_dir}\nReady. Send a link or file to download.",
            reply_markup=InlineKeyboardMarkup([[_home_button()]]),
        )


async def handle_manual_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await safe_answer(query)
    await _edit_message(query, 
        "Type the title:",
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "⬅️ Back", callback_data="action|search"
                    ),
                    _home_button(),
                ]
            ]
        ),
    )
    set_state(context.user_data, STATE_MANUAL_TITLE)


async def handle_autolib(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the auto-detected library button — clears it so user picks manually."""
    query = update.callback_query
    await safe_answer(query)
    context.user_data.pop("auto_library", None)
    sel = context.user_data.get("selected_tmdb") or {}
    title = sel.get("title", "Content")
    markup = build_library_keyboard()
    text = f"{title}\n\nSelect destination library:"
    await _edit_message(query, text, reply_markup=markup)


async def handle_cancel_flow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await safe_answer(query)
    from app.state import reset_flow_state as reset

    reset(context)
    await _edit_message(query, 
        "Flow cancelled.",
        reply_markup=InlineKeyboardMarkup([[_home_button()]]),
    )
