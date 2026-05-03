"""Conversation state constants and helpers."""

import re
from typing import Optional

# State machine states
STATE_SEARCH = "search_query"
STATE_MANUAL_TITLE = "manual_title"
STATE_MANUAL_SEASON = "manual_season"

# Behavioural library types
SERIES_TYPES = {"series", "anime"}
MOVIE_TYPES = {"movie", "movies", "film", "films"}


def title_without_year(title: str, year: Optional[int]) -> str:
    if not title:
        return "Content"
    if year:
        pattern = rf"\s*\({re.escape(str(year))}\)$"
        cleaned = title.strip()
        while re.search(pattern, cleaned):
            cleaned = re.sub(pattern, "", cleaned).strip()
        return cleaned or "Content"
    return title.strip() or "Content"


def title_with_year(title: str, year: Optional[int]) -> str:
    base = title_without_year(title, year)
    return f"{base} ({year})" if year else base


def set_state(user_data: dict, state: Optional[str]) -> None:
    if state:
        user_data["state"] = state
    else:
        user_data.pop("state", None)


def reset_flow_state(context) -> None:
    for key in [
        "state",
        "pending_title",
        "pending_year",
        "pending_season",
        "tmdb_results",
        "tmdb_page",
        "selected_tmdb",
        "auto_library",
    ]:
        context.user_data.pop(key, None)
    for key in [
        "download_dir",
        "season_hint",
        "active_library",
        "selected_type",
        "pending_links",
        "batch_prompted",
        "_batch_notices",
    ]:
        context.chat_data.pop(key, None)


def record_recent(context, chat_id: int, title: str, library: dict, season: Optional[int], year: Optional[int] = None) -> None:
    clean_title = title_with_year(title, year)
    key = clean_title.lower()
    entries: list = context.bot_data.setdefault("recent_destinations", {}).setdefault(
        chat_id, []
    )
    entries[:] = [e for e in entries if e.get("key") != key]
    entries.insert(0, {"key": key, "title": clean_title, "library": library, "season": season, "year": year})
    if len(entries) > 5:
        entries.pop()


def get_recent_for(
    context, chat_id: int, title: str
) -> Optional[dict]:
    key = title.strip().lower()
    entries: list = context.bot_data.get("recent_destinations", {}).get(
        chat_id, []
    )
    for entry in entries:
        entry_key = entry.get("key")
        entry_title = entry.get("title", "")
        entry_year = entry.get("year")
        entry_base_key = title_without_year(entry_title, entry_year).lower()
        if entry_key == key or entry_base_key == key:
            return entry
    return None
