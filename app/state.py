"""Conversation state constants and helpers."""

from typing import Optional

# State machine states
STATE_SEARCH = "search_query"
STATE_MANUAL_TITLE = "manual_title"
STATE_MANUAL_SEASON = "manual_season"

# Behavioural library types
SERIES_TYPES = {"series", "anime"}
MOVIE_TYPES = {"movie", "movies", "film", "films"}


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
    ]:
        context.chat_data.pop(key, None)


def record_recent(context, chat_id: int, title: str, library: dict, season: Optional[int], year: Optional[int] = None) -> None:
    key = title.strip().lower()
    entries: list = context.bot_data.setdefault("recent_destinations", {}).setdefault(
        chat_id, []
    )
    entries[:] = [e for e in entries if e.get("key") != key]
    entries.insert(0, {"key": key, "title": title, "library": library, "season": season, "year": year})
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
        if entry.get("key") == key:
            return entry
    return None
