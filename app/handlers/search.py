"""TMDb search flow: result selection, pagination, season pick, library choice."""

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
    msg = query.message
    if msg.photo:
        await msg.edit_caption(caption=text, reply_markup=reply_markup)
    else:
        await msg.edit_text(text, reply_markup=reply_markup)


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
    await query.answer()
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
    await query.answer()
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
        seasons = get_seasons(item.id)
        markup = (
            build_season_keyboard(seasons)
            if seasons
            else InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅️ Back", callback_data="action|search"), _home_button()]]
            )
        )
        text = f"{_format_item_preview(item)}\n\nChoose a season:"
        if not seasons:
            set_state(context.user_data, STATE_MANUAL_SEASON)
            text = f"{_format_item_preview(item)}\n\nType the season number:"

        if item.poster:
            await query.message.delete()
            await context.bot.send_photo(
                chat_id=update.effective_chat.id,
                photo=item.poster,
                caption=text,
                reply_markup=markup,
            )
        else:
            await _edit_message(query, text, reply_markup=markup)
        return

    # Movie → go to library selection
    markup = build_library_keyboard()
    text = f"{_format_item_preview(item)}\n\nSelect destination library:"
    if item.poster:
        await query.message.delete()
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
    await query.answer()
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
    title = sel.get("title", "Content")

    await _edit_message(query, 
        f"{title} — Season {season_num}\n\nSelect destination library:",
        reply_markup=build_library_keyboard(),
    )


async def handle_library(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
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
    season = context.user_data.get("pending_season")

    from app.handlers.download import set_destination, queue_download

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
        for item in pending_items:
            await queue_download(
                query.message,
                context,
                item["link"],
                download_dir,
                title,
                season,
                year,
                item.get("filename") or item["link"],
            )
    else:
        await _edit_message(
            query,
            f"Destination set: {download_dir}\nReady. Send a link or file to download.",
            reply_markup=InlineKeyboardMarkup([[_home_button()]]),
        )


async def handle_manual_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
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


async def handle_cancel_flow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    from app.state import reset_flow_state as reset

    reset(context)
    await _edit_message(query, 
        "Flow cancelled.",
        reply_markup=InlineKeyboardMarkup([[_home_button()]]),
    )
