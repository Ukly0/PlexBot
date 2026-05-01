"""Link/file intake handler with auto-metadata extraction."""

import os
import re
import logging
from typing import Optional
from dataclasses import dataclass

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


# ── Noise tokens (lowered for case-insensitive comparison) ─────────

_NOISE_TOKENS = {
    "1080p", "720p", "480p", "2160p", "4k", "bluray", "webdl", "webrip",
    "hdrip", "x264", "x265", "h264", "h265", "hdr", "dolby", "atmos",
    "dts", "aac", "ac3", "eac3", "truehd", "remux", "hevc", "avc",
    "web", "dl", "nf", "amzn", "dsnp", "hulu", "hmax", "atvp",
    "multi", "dual", "sub", "subs", "espa", "latino", "castellano",
    "english", "eng", "esp", "hd", "full", "proper", "repack", "extended",
    "directors", "cut", "theatrical", "uncut", "unrated",
    "10bit", "6ch", "ita", "fre", "ger", "jpn", "kor", "rus", "esp",
}

# ── Episode/season extraction patterns (applied BEFORE TMDb search) ──

# S01E02, S1E02, 1x02, S02.E05 — per-token matching
_RX_SE_TOKEN = re.compile(
    r"^S?(\d{1,2})[xEex](\d{1,3})(?:[Ee](\d{1,3}))*$", re.I
)
# S02 standalone as a token
_RX_SEASON_TOKEN = re.compile(r"^S(\d{1,2})$", re.I)
# Year token: (2024) or standalone 2024 (4 digits starting with 19 or 20)
_RX_YEAR_TOKEN = re.compile(r"^\(?((?:19|20)\d{2})\)?$")
# Resolution tokens: 1080p, 720p, etc. (matched case-insensitively later)
_RX_RES_TOKEN = re.compile(r"^(?:2160p|1080p|720p|480p|576p|360p|4k)$", re.I)

# ── Patterns that indicate the string is *only* an episode/season marker ──

_RX_ONLY_EPISODE_MARKER = re.compile(
    r"^(s\d{1,2}e\d{1,3}|\d{1,2}x\d{1,3}|e\d{1,3}|episodio\s*\d{1,3}|capitulo\s*\d{1,3}|"
    r"cap\s*\d{1,3}|ep\s*\d{1,3}|episode\s*\d{1,3}|chapter\s*\d{1,3}|temporada\s*\d{1,2}|"
    r"season\s*\d{1,2}|s\d{1,2})$",
    re.I,
)


# ── Parsed metadata from a filename ────────────────────────────────

@dataclass
class ParsedName:
    title: str
    season: Optional[int] = None
    episode: Optional[int] = None
    year: Optional[int] = None


def _parse_filename(filename: str) -> ParsedName:
    """Extract title, season, episode, and year from a filename.

    Tokenizes the filename, classifies each token, and builds a clean
    title for TMDb search while preserving extracted metadata.
    """
    stem = os.path.splitext(filename)[0]
    cleaned = stem.replace(".", " ").replace("_", " ").replace("-", " ")
    tokens = cleaned.split()

    year = None
    season = None
    episode = None
    title_tokens = []

    for tok in tokens:
        tok_lower = tok.lower()

        # Check year: (2024), 2024, etc.
        ym = _RX_YEAR_TOKEN.match(tok)
        if ym:
            year = int(ym.group(1))
            continue

        # Check SxxExx pattern
        sem = _RX_SE_TOKEN.match(tok)
        if sem:
            season = int(sem.group(1))
            episode = int(sem.group(2))
            continue

        # Check S02 standalone
        sm = _RX_SEASON_TOKEN.match(tok)
        if sm:
            season = int(sm.group(1))
            continue

        # Check resolution tokens
        if _RX_RES_TOKEN.match(tok_lower):
            continue

        # Check noise tokens
        if tok_lower in _NOISE_TOKENS:
            continue

        title_tokens.append(tok)

    title = " ".join(title_tokens).strip()
    title = re.sub(r"\s+", " ", title)

    # If all tokens were noise/markers, fall back to full cleaned stem
    # minus noise and markers
    if not title or len(title) < 2:
        fallback_tokens = []
        for tok in tokens:
            tok_lower = tok.lower()
            if _RX_YEAR_TOKEN.match(tok):
                continue
            if _RX_SE_TOKEN.match(tok):
                continue
            if _RX_SEASON_TOKEN.match(tok):
                continue
            if _RX_RES_TOKEN.match(tok_lower):
                continue
            if tok_lower in _NOISE_TOKENS:
                continue
            fallback_tokens.append(tok)
        title = " ".join(fallback_tokens).strip() or cleaned.strip()

    return ParsedName(title=title or "", season=season, episode=episode, year=year)


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
    """Backward-compatible: returns just the cleaned title string."""
    return _parse_filename(filename).title


def _extract_season(filename: str) -> Optional[int]:
    """Backward-compatible: returns just the season number from a filename."""
    return _parse_filename(filename).season


def _get_message_link(message) -> str:
    chat = message.chat
    if chat.username:
        return f"https://t.me/{chat.username}/{message.message_id}"
    chat_id_str = str(chat.id)
    base = chat_id_str[4:] if chat_id_str.startswith("-100") else chat_id_str.lstrip("-")
    return f"https://t.me/c/{base}/{message.message_id}"


def _add_pending(context, link: str, filename: Optional[str], is_text: bool = False) -> bool:
    """Add to pending queue; returns False if this link is already queued."""
    items: list = context.chat_data.setdefault("pending_links", [])
    for item in items:
        if item.get("link") == link:
            return False
    items.append({"link": link, "filename": filename, "is_text": is_text})
    return True


async def handle_download_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message:
        return

    # Determine link and filename
    link = None
    filename = None
    is_text_link = False

    if message.text and "https://t.me" in message.text:
        link = message.text.strip()
        is_text_link = True
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
                title, season, year, display_name, use_group=is_text_link,
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

        # For series: add to pending
        _add_pending(context, link, filename, is_text=is_text_link)
        pending_count = len(context.chat_data.get("pending_links", []))
        lib_name = active_lib.get("name", "")
        season_label = f" S{season:02d}" if season else ""

        # If we already showed the batch prompt, just confirm silently
        if context.chat_data.get("batch_prompted"):
            try:
                await message.reply_text(
                    f"✅ Added to batch ({pending_count} pending) → {lib_name}",
                )
            except Exception as e:
                logging.warning("Could not send batch confirm: %s", e)
            return

        context.chat_data["batch_prompted"] = True
        try:
            await message.reply_text(
                f"📥 Added to batch: {title}{season_label} → {lib_name}\n"
                f"Pending: {pending_count} item(s).\n\n"
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
        except Exception as e:
            logging.warning("Could not send batch message: %s", e)
        return

    # No destination set — store link in pending queue
    _add_pending(context, link, filename, is_text=is_text_link)

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

    # Parse filename to extract title, season, episode, year
    source_text = filename or (message.text.split("https://")[0] if message.text else "") or (message.caption.split("https://")[0] if message.caption else "")
    parsed = _parse_filename(source_text) if source_text else ParsedName(title="")
    guess = parsed.title

    # Store extracted season/year as hints (user can override)
    if parsed.season is not None:
        context.user_data["pending_season"] = parsed.season
    if parsed.year is not None:
        context.user_data["pending_year"] = parsed.year

    # Also set season_hint in chat_data for use during download pipeline
    if parsed.season is not None:
        context.chat_data["season_hint"] = parsed.season

    if guess and _is_meaningful(guess):
        # Check recent destinations for quick-add shortcut
        recent = get_recent_for(context, update.effective_chat.id, guess)
        if recent:
            from app.handlers.search import build_library_keyboard

            context.user_data["pending_title"] = recent["title"]
            context.user_data["pending_season"] = recent.get("season") or context.user_data.get("pending_season")
            context.user_data["pending_year"] = recent.get("year") or context.user_data.get("pending_year")
            recent_lib = recent.get("library") or {}
            recent_type = recent_lib.get("type", "movie")
            context.user_data["selected_tmdb"] = {
                "id": 0,
                "kind": "tv" if recent_type in ("series", "anime") else "movie",
                "title": recent["title"],
                "year": context.user_data.get("pending_year"),
            }
            lib_kb = build_library_keyboard()
            rows = lib_kb.inline_keyboard + [
                [InlineKeyboardButton("✍️ Search another title", callback_data="action|search")]
            ]
            await message.reply_text(
                f"Detected: {guess}\n\n"
                f"Continue '{recent['title']}' "
                + (f"Season {recent['season']}? " if recent.get("season") else "")
                + "Pick a library:",
                reply_markup=InlineKeyboardMarkup(rows),
            )
        else:
            # Auto-search TMDb with the CLEANED title (no SxxExx, no year)
            results = tmdb_search(guess)
            context.user_data["tmdb_results"] = results
            context.user_data["tmdb_page"] = 0

            if results:
                first = results[0]
                markup = build_results_keyboard(results, 0)
                # Show what we detected for transparency
                detected_info = guess
                hints = []
                if parsed.season is not None:
                    hints.append(f"S{parsed.season:02d}")
                if parsed.year is not None:
                    hints.append(str(parsed.year))
                if hints:
                    detected_info += f" [{', '.join(hints)}]"
                caption_text = f"Detected: {detected_info}\nSelect the matching title:"

                if first.poster:
                    await message.reply_photo(
                        photo=first.poster,
                        caption=caption_text,
                        reply_markup=markup,
                    )
                else:
                    await message.reply_text(
                        caption_text,
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