"""Link/file intake handler with auto-metadata extraction."""

import os
import re
import logging
import asyncio
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
    # Codecs
    "x264", "x265", "h264", "h265", "hevc", "avc", "vp9", "av1",
    # Sources
    "bluray", "webdl", "webrip", "hdrip", "dvdrip", "brrip", "bdrip",
    "remux", "hdtv", "pdtv", "satrip", "dvd",
    # Streaming groups
    "nf", "amzn", "dsnp", "hulu", "hmax", "atvp", "pmtp", "crav", "stan",
    # Audio
    "aac", "dts", "ac3", "eac3", "truehd", "atmos", "flac", "ddp", "dd",
    "5.1", "7.1", "2.0", "5.1ch", "7.1ch",
    # Quality
    "1080p", "720p", "480p", "576p", "360p", "2160p", "800p", "4k", "uhd",
    "hd", "fhd", "hdr", "dolby", "dovi", "hdr10",
    # Language codes (common 2-letter)
    "es", "en", "fr", "de", "it", "pt", "pl", "ru", "ja", "ko", "zh",
    # Language tags (longer)
    "espa", "latino", "castellano", "english", "eng", "esp", "ita",
    "fre", "ger", "jpn", "kor", "rus",
    # Containers/formats
    "mp4", "mkv", "avi", "webm", "flv", "wmv", "mov", "ts",
    # Scene tags
    "multi", "dual", "sub", "subs", "proper", "repack", "extended",
    "directors", "cut", "theatrical", "uncut", "unrated", "complete",
    "completos", "forzados", "quemados", "10bit", "6ch",
    # Framerates
    "24fps", "25fps", "30fps", "60fps",
    # Release groups (common)
    "scorpion", "xusman", "kowalski", "yts", "ettv", "rartv", "btn",
    "nzb", "rarbg", "tigole", "ptp", "hdbits",
    # Misc
    "web", "dl", "full", "ntsc", "pal",
}

# ── Pre-processing patterns (applied BEFORE tokenization) ───────────

# Strip audio configs like 5.1, 7.1, 2.0 before tokenization splits on "."
_RX_AUDIO_CONFIG = re.compile(r"\b\d\.\d\b", re.I)

# S01E02, S1E2, 1x04, S02.E05, 1X04 — per-token matching
_RX_SE_TOKEN = re.compile(r"^S?(\d{1,2})[xEex](\d{1,3})", re.I)
# S02 standalone as a token (season-only marker)
_RX_SEASON_TOKEN = re.compile(r"^S(\d{1,2})$", re.I)
# Year in parentheses: (2024), (2024-text)  — matched BEFORE tokenization
_RX_PAREN_YEAR = re.compile(r"\((19\d{2}|20\d{2})(?:\s*[-–—]\s*[^)]*)?\)")
# Remaining parentheses (non-year, already stripped by pre-pass)
_RX_PAREN_CONTENT = re.compile(r"\([^)]*\)")
# Year as standalone token: (2024) after space normalization, or just 2024
_RX_YEAR_TOKEN = re.compile(r"^\(?((?:19|20)\d{2})\)?$")
# Resolution tokens: 1080p, 720p, etc.
_RX_RES_TOKEN = re.compile(r"^(?:2160p|1080p|720p|576p|480p|360p|800p|4k)$", re.I)
# Framerate token: 24fps, etc.
_RX_FPS_TOKEN = re.compile(r"^\d+fps$", re.I)

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

    PRE-PASS extracts years from parenthetical expressions and strips all
    parenthetical content. Then tokenizes, classifies each token, and builds
    a clean title. Tokens after the first SxxExx marker are treated as
    subtitle/noise and excluded from the title.
    """
    # Strip known video/archive extensions only
    # (os.path.splitext treats 5.1, 7.1 as extensions — too greedy)
    _VIDEO_EXTS = (".mkv", ".mp4", ".avi", ".mov", ".ts", ".m4v", ".webm",
                   ".flv", ".wmv", ".mpg", ".mpeg", ".m2ts", ".mts",
                   ".rar", ".zip", ".7z", ".tar", ".gz")
    stem = filename
    for ext in _VIDEO_EXTS:
        if stem.lower().endswith(ext):
            stem = stem[: -len(ext)]
            break

    # ── PRE-PASS: Strip audio configs like 5.1, 7.1, 2.0 ────────────
    # Must happen before "." → " " normalization splits them
    stem = _RX_AUDIO_CONFIG.sub(" ", stem)

    # ── PRE-PASS: Extract year from (YYYY) or (YYYY-text) ───────────
    year = None

    def _extract_year(m):
        nonlocal year
        if year is None:
            year = int(m.group(1))
        return " "

    stem = _RX_PAREN_YEAR.sub(_extract_year, stem)

    # ── PRE-PASS: Strip remaining parenthetical content ──────────────
    # E.g. "(Esta chica me pone)", "(1080p)", alternate titles
    stem = _RX_PAREN_CONTENT.sub(" ", stem)

    # ── Normalize all separators to spaces ───────────────────────────
    cleaned = stem.replace(".", " ").replace("_", " ").replace("-", " ")
    cleaned = cleaned.replace("+", " ").replace("&", " ")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()

    tokens = cleaned.split()

    season = None
    episode = None
    title_tokens = []
    se_position = None  # index where first SxxExx marker was found

    for i, tok in enumerate(tokens):
        tok_lower = tok.lower()

        # Check year token: (2024), 2024, etc.
        ym = _RX_YEAR_TOKEN.match(tok)
        if ym:
            if year is None:
                year = int(ym.group(1))
            continue

        # Check SxxExx pattern
        sem = _RX_SE_TOKEN.match(tok)
        if sem:
            if se_position is None:
                se_position = i
            season = int(sem.group(1))
            # Some tokens like "1X04Hulk" have trailing chars — only extract digits
            episode = int(sem.group(2))
            continue

        # Check S02 standalone
        sm = _RX_SEASON_TOKEN.match(tok)
        if sm:
            if se_position is None:
                se_position = i
            season = int(sm.group(1))
            continue

        # Check resolution tokens
        if _RX_RES_TOKEN.match(tok_lower):
            continue

        # Check fps tokens
        if _RX_FPS_TOKEN.match(tok_lower):
            continue

        # Check noise tokens (exact match)
        if tok_lower in _NOISE_TOKENS:
            continue

        # ── Tokens AFTER the SxxExx marker are subtitle/noise ───────
        # Only include title tokens that come BEFORE the marker.
        # Exception: allow a few post-marker tokens if no SE was found
        # (movie titles have no season marker).
        if se_position is not None:
            continue

# Skip tokens that are purely non-alphabetic (numbers, symbols, emoji)
        # Exception: keep small numbers that are part of a franchise name
        # (e.g. "2" in "Greenland 2") only if the preceding title word is
        # capitalized AND the next token is NOT a small digit (avoids "5 1" from 5.1)
        if tok and not any(c.isalpha() for c in tok):
            if tok.isdigit() and 1 <= int(tok) <= 99:
                prev_cap = title_tokens and title_tokens[-1][0].isupper()
                next_is_digit = (
                    i + 1 < len(tokens)
                    and tokens[i + 1].isdigit()
                    and len(tokens[i + 1]) <= 2
                )
                if prev_cap and not next_is_digit:
                    pass  # franchise number like "Greenland 2"
                else:
                    continue  # audio config like "5" in "5.1"
            else:
                continue

        # Skip single-character tokens that are likely noise (s, c, etc.)
        # but keep single-letter articles/prepositions common in titles
        # and single-digit franchise numbers (2 in "Greenland 2")
        _KEEP_SHORT = {"a", "e", "y", "o", "u", "el", "la", "lo", "le",
                       "de", "del", "al", "en", "un", "una", "the", "of",
                       "in", "on", "at", "to", "is", "an", "as", "by", "or"}
        if len(tok) == 1 and tok.lower() not in _KEEP_SHORT and not tok.isupper() and not tok.isdigit():
            continue

        title_tokens.append(tok)

# ── Post-processing: strip "by releasegroup" suffixes ────────────
    # Scene releases often end with "by groupname" or "by group1&group2"
    # Remove everything from last "by" if followed by lowercase/funky tokens
    by_index = None
    for j in range(len(title_tokens) - 1, -1, -1):
        if title_tokens[j].lower() == "by" and j < len(title_tokens) - 1:
            by_index = j
            break
    if by_index is not None:
        after_by = " ".join(title_tokens[by_index + 1:]).lower()
        group_indicators = any(
            c in after_by for c in "&0123456789"
        ) or len(after_by.split()) <= 3
        if group_indicators:
            title_tokens = title_tokens[:by_index]
    # Also strip trailing "by" that has no subsequent tokens (noise suffix)
    if title_tokens and title_tokens[-1].lower() == "by":
        title_tokens.pop()

    title = " ".join(title_tokens).strip()
    title = re.sub(r"\s+", " ", title)

    # ── Fallback: if title is too short, try including post-SE tokens ─
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
            if _RX_FPS_TOKEN.match(tok_lower):
                continue
            if tok_lower in _NOISE_TOKENS:
                continue
            if tok and not any(c.isalpha() for c in tok):
                if tok.isdigit() and 1 <= int(tok) <= 99:
                    pass
                else:
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


async def _safe_reply(message, text: str, reply_markup=None, max_retries: int = 3):
    """Send a reply with RetryAfter/TimedOut handling."""
    from telegram.error import RetryAfter, TimedOut
    for attempt in range(max_retries):
        try:
            return await message.reply_text(text, reply_markup=reply_markup)
        except RetryAfter as e:
            wait = getattr(e, "retry_after", 30) or 30
            logging.warning("Flood control: retrying reply in %ss (attempt %s/%s)", wait, attempt + 1, max_retries)
            await asyncio.sleep(wait)
        except TimedOut:
            logging.warning("Timed out sending reply (attempt %s/%s)", attempt + 1, max_retries)
            await asyncio.sleep(2)
        except Exception as e:
            err_str = str(e)
            if "not modified" in err_str.lower():
                return None
            logging.warning("Reply failed: %s", e)
            return None
    logging.error("Reply failed after %s retries", max_retries)
    return None


async def _safe_reply_photo(message, photo, caption: str, reply_markup=None, max_retries: int = 3):
    """Send a photo reply with RetryAfter/TimedOut handling."""
    from telegram.error import RetryAfter, TimedOut
    for attempt in range(max_retries):
        try:
            return await message.reply_photo(photo=photo, caption=caption, reply_markup=reply_markup)
        except RetryAfter as e:
            wait = getattr(e, "retry_after", 30) or 30
            logging.warning("Flood control: retrying photo in %ss (attempt %s/%s)", wait, attempt + 1, max_retries)
            await asyncio.sleep(wait)
        except TimedOut:
            logging.warning("Timed out sending photo (attempt %s/%s)", attempt + 1, max_retries)
            await asyncio.sleep(2)
        except Exception as e:
            logging.warning("Photo reply failed: %s", e)
            return None
    logging.error("Photo reply failed after %s retries", max_retries)
    return None


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
            await _safe_reply(message, f"✅ Added to batch ({pending_count} pending) → {lib_name}")
            return

        context.chat_data["batch_prompted"] = True
        await _safe_reply(
            message,
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
        return

    # No destination set — store link in pending queue
    _add_pending(context, link, filename, is_text=is_text_link)

    # If auto-detection is already in progress (user is picking metadata),
    # don't start another search — the new link is safely queued.
    if context.user_data.get("state") in (STATE_SEARCH, "pending_selection"):
        await _safe_reply(
            message,
            f"📥 Queued. Complete the current selection first.\n"
            f"Pending: {len(context.chat_data.get('pending_links', []))} item(s).",
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
            await _safe_reply(
                message,
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
                    await _safe_reply_photo(
                        message, first.poster, caption_text, reply_markup=markup,
                    )
                else:
                    await _safe_reply(message, caption_text, reply_markup=markup)
            else:
                err = tmdb_last_error() or ""
                note = f"\nTMDb: {err}" if err else ""
                context.user_data["state"] = STATE_SEARCH
                await _safe_reply(
                    message,
                    f"Detected: {guess}\nNo TMDb results.{note}\n"
                    "Type a title to search manually, or /search.",
                )
    else:
        # No meaningful guess — skip auto-search, ask user directly
        hint = f"(from: {guess})" if guess else ""
        context.user_data["state"] = STATE_SEARCH
        await _safe_reply(
            message,
            f"Content received {hint}\nType a title to search, or /search.",
        )