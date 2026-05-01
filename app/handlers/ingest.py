"""Link/file intake handler with auto-metadata extraction."""

import os
import re
import logging
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from app.services.tmdb import search as tmdb_search, tmdb_last_error
from app.handlers.search import build_results_keyboard
from app.handlers.download import queue_download
from app.state import (
    STATE_SEARCH,
    set_state,
    get_recent_for,
)


# Tokens that alone carry no meaningful title information.
_NOISE_TOKENS = {
    "1080p", "720p", "480p", "2160p", "4k", "bluray", "webdl", "webrip",
    "hdrip", "x264", "x265", "h264", "h265", "hdr", "dolby", "atmos",
    "dts", "aac", "ac3", "eac3", "truehd", "remux", "hevc", "avc",
    "web", "dl", "nf", "amzn", "dsnp", "hulu", "hmax", "atvp",
    "multi", "dual", "sub", "subs", "espa", "latino", "castellano",
    "english", "eng", "esp", "hd", "full", "proper", "repack", "extended",
    "directors", "cut", "theatrical", "uncut", "unrated",
}

# Patterns that indicate the string is *only* an episode/season marker, not a title.
_RX_ONLY_EPISODE_MARKER = re.compile(
    r"^(s\d{1,2}e\d{1,3}|\d{1,2}x\d{1,3}|e\d{1,3}|episodio\s*\d{1,3}|capitulo\s*\d{1,3}|"
    r"cap\s*\d{1,3}|ep\s*\d{1,3}|episode\s*\d{1,3}|chapter\s*\d{1,3}|temporada\s*\d{1,2}|"
    r"season\s*\d{1,2}|s\d{1,2})$",
    re.I,
)


def _is_meaningful(text: str) -> bool:
    """Return True if the string looks like a real title worth searching on TMDb."""
    if not text or not text.strip():
        return False

    stripped = text.strip()

    # Pure episode/season markers → garbage
    if _RX_ONLY_EPISODE_MARKER.match(stripped):
        return False

    # Remove noise tokens and check what's left
    meaningful = [t for t in stripped.split() if t.lower() not in _NOISE_TOKENS]
    joined = " ".join(meaningful).strip()

    # Nothing left after stripping noise
    if not joined:
        return False

    # Only digits left → garbage (e.g. "3", "01 02")
    if joined.isdigit():
        return False

    # Need at least 2 alphabetic characters (catches single letters, pure symbols)
    alpha_chars = sum(1 for c in joined if c.isalpha())
    if alpha_chars < 2:
        return False

    # After stripping episode patterns, is anything left?
    stripped_ep = _RX_ONLY_EPISODE_MARKER.sub("", joined).strip()
    if not stripped_ep or stripped_ep.isdigit():
        return False

    return True


def _guess_title(filename: str) -> str:
    stem = os.path.splitext(filename)[0]
    cleaned = stem.replace(".", " ").replace("_", " ").replace("-", " ")
    tokens = cleaned.split()
    filtered = [t for t in tokens if t.lower() not in _NOISE_TOKENS]
    title = " ".join(filtered) or cleaned
    return title.strip()


def _extract_season(filename: str) -> Optional[int]:
    patterns = [
        r"S(\d{1,2})",  # S01, S1
        r"Season\s*(\d{1,2})",  # Season 1
        r"(\d{1,2})x\d{1,3}",  # 1x02
    ]
    for pat in patterns:
        m = re.search(pat, filename, re.I)
        if m:
            return int(m.group(1))
    return None


def _get_message_link(message) -> str:
    chat = message.chat
    if chat.username:
        return f"https://t.me/{chat.username}/{message.message_id}"
    chat_id_str = str(chat.id)
    base = chat_id_str[4:] if chat_id_str.startswith("-100") else chat_id_str.lstrip("-")
    return f"https://t.me/c/{base}/{message.message_id}"


def _add_pending(context, link: str, filename: Optional[str]) -> bool:
    """Add to pending queue; returns False if this link is already queued."""
    items: list = context.chat_data.setdefault("pending_links", [])
    for item in items:
        if item.get("link") == link:
            return False
    items.append({"link": link, "filename": filename})
    return True


async def handle_download_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message:
        return

    # Determine link and filename
    link = None
    filename = None

    if message.text and "https://t.me" in message.text:
        link = message.text.strip()
    elif any([message.document, message.video, message.audio, message.photo]):
        link = _get_message_link(message)
        if message.document:
            filename = message.document.file_name
        elif message.video:
            filename = message.video.file_name
        elif message.audio:
            filename = message.audio.file_name

    if not link:
        return

    # If destination is already set
    download_dir = context.chat_data.get("download_dir")
    if download_dir:
        title = context.user_data.get("pending_title") or "Content"
        season = context.chat_data.get("season_hint")
        year = context.user_data.get("pending_year")
        active_lib = context.chat_data.get("active_library") or {}
        lib_type = active_lib.get("type", "movie")

        # For movies: auto-queue and clear destination (each movie is independent)
        if lib_type not in ("series", "anime"):
            from app.handlers.download import queue_download

            display_name = filename or link
            await queue_download(
                message, context, link, download_dir,
                title, season, year, display_name,
            )
            context.chat_data.pop("download_dir", None)
            context.chat_data.pop("active_library", None)
            context.chat_data.pop("season_hint", None)
            context.chat_data.pop("selected_type", None)
            context.user_data.pop("pending_title", None)
            context.user_data.pop("pending_year", None)
            context.user_data.pop("pending_season", None)
            context.user_data.pop("selected_tmdb", None)
            return

        # For series: add to pending and ask user whether to continue batch or start new
        _add_pending(context, link, filename)
        lib_name = active_lib.get("name", "")
        season_label = f" S{season:02d}" if season else ""
        await message.reply_text(
            f"📥 Added to batch: {title}{season_label} → {lib_name}\n"
            f"Pending: {len(context.chat_data.get('pending_links', []))} item(s).\n\n"
            f"Continue with this series or start a new search?",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "✅ Continue batch", callback_data="action|continue_batch"
                    ),
                    InlineKeyboardButton(
                        "🔍 New search", callback_data="action|new_search"
                    ),
                ]
            ]),
        )
        return

    # No destination set — store link in pending queue
    _add_pending(context, link, filename)

    # If auto-detection is already in progress (user is picking metadata),
    # don't start another search — the new link is safely queued.
    if context.user_data.get("state") in (STATE_SEARCH, "pending_selection"):
        await message.reply_text(
            f"📥 Queued. Complete the current selection first.\n"
            f"Pending: {len(context.chat_data.get('pending_links', []))} item(s)."
        )
        return

    # Mark that we're now doing auto-detection
    context.user_data["state"] = "pending_selection"

    # Try to guess title
    if filename:
        guess = _guess_title(filename)
    elif message.text:
        guess = _guess_title(message.text.split("https://")[0])
    elif message.caption:
        guess = _guess_title(message.caption.split("https://")[0])
    else:
        guess = ""

    if guess and _is_meaningful(guess):
        # Check recent destinations for quick-add shortcut
        recent = get_recent_for(context, update.effective_chat.id, guess)
        if recent:
            from app.handlers.search import build_library_keyboard

            # Pre-fill title/season so user just picks a library
            context.user_data["pending_title"] = recent["title"]
            context.user_data["pending_season"] = recent.get("season")
            context.user_data["pending_year"] = recent.get("year")
            recent_lib = recent.get("library") or {}
            recent_type = recent_lib.get("type", "movie")
            context.user_data["selected_tmdb"] = {
                "id": 0,
                "kind": "tv" if recent_type in ("series", "anime") else "movie",
                "title": recent["title"],
                "year": recent.get("year"),
            }
            await message.reply_text(
                f"Detected: {guess}\n\n"
                f"Continue '{recent['title']}' "
                + (f"Season {recent['season']}? " if recent.get("season") else "")
                + "Pick a library:",
                reply_markup=build_library_keyboard(),
            )
        else:
            # Auto-search TMDb
            results = tmdb_search(guess)
            context.user_data["tmdb_results"] = results
            context.user_data["tmdb_page"] = 0

            if results:
                first = results[0]
                markup = build_results_keyboard(results, 0)
                if first.poster:
                    await message.reply_photo(
                        photo=first.poster,
                        caption=f"Detected: {guess}\nSelect the matching title:",
                        reply_markup=markup,
                    )
                else:
                    await message.reply_text(
                        f"Detected: {guess}\nSelect the matching title:",
                        reply_markup=markup,
                    )
            else:
                err = tmdb_last_error() or ""
                note = f"\nTMDb: {err}" if err else ""
                context.user_data["state"] = STATE_SEARCH
                await message.reply_text(
                    f"Detected: {guess}\nNo TMDb results.{note}\n"
                    "Type a title to search manually, or /search.",
                )
    else:
        # No meaningful guess — skip auto-search, ask user directly
        hint = f"(from: {guess})" if guess else ""
        context.user_data["state"] = STATE_SEARCH
        await message.reply_text(
            f"Content received {hint}\nType a title to search, or /search.",
        )
