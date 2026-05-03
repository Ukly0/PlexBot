"""TMDb API v3 client — multi-search and season lookup."""

import logging
import os
from dataclasses import dataclass
from typing import Optional

import requests

TMDB_BASE = "https://api.themoviedb.org/3"
TMDB_IMG_BASE = "https://image.tmdb.org/t/p"
_tmdb_last_error: Optional[str] = None


@dataclass
class TMDbItem:
    id: int
    title: str
    year: Optional[int]
    kind: str  # "movie" or "tv"
    poster: Optional[str] = None
    popularity: float = 0.0
    rating: float = 0.0
    overview: Optional[str] = None


@dataclass
class TMDbSeason:
    season_number: int


def tmdb_last_error() -> Optional[str]:
    return _tmdb_last_error


def _headers() -> Optional[dict]:
    token = os.getenv("TMDB_API_KEY")
    if not token:
        global _tmdb_last_error
        _tmdb_last_error = "Missing TMDB_API_KEY"
        return None
    return {"Authorization": f"Bearer {token}"}


def _extract_year(date_str: Optional[str]) -> Optional[int]:
    if not date_str:
        return None
    try:
        return int(date_str.split("-")[0])
    except Exception:
        return None


def _poster_url(path: Optional[str], size: str = "w500") -> Optional[str]:
    if not path:
        return None
    return f"{TMDB_IMG_BASE}/{size}{path}"


def search(query: str, limit: int = 10) -> list[TMDbItem]:
    global _tmdb_last_error
    hdrs = _headers()
    if not hdrs:
        return []

    items: list[TMDbItem] = []
    try:
        for kind, endpoint in (("movie", "search/movie"), ("tv", "search/tv")):
            r = requests.get(
                f"{TMDB_BASE}/{endpoint}",
                params={"query": query, "language": "en-US"},
                headers=hdrs,
                timeout=10,
            )
            if r.status_code == 401:
                _tmdb_last_error = "Invalid API key"
                return []
            r.raise_for_status()
            for d in r.json().get("results", []) or []:
                title = d.get("title") if kind == "movie" else d.get("name")
                if not title:
                    continue
                year_field = (
                    d.get("release_date") if kind == "movie" else d.get("first_air_date")
                )
                year = _extract_year(year_field)
                popularity = float(d.get("popularity") or 0)
                rating = float(d.get("vote_average") or 0)
                overview = d.get("overview")
                poster = _poster_url(d.get("poster_path"))
                items.append(
                    TMDbItem(
                        id=int(d["id"]),
                        title=title,
                        year=year,
                        kind=kind,
                        poster=poster,
                        popularity=popularity,
                        rating=rating,
                        overview=overview,
                    )
                )
        _tmdb_last_error = None
    except Exception as e:
        _tmdb_last_error = f"search error: {e}"
        logging.error("TMDb search error: %s", e)

    items.sort(key=lambda x: x.popularity, reverse=True)
    return items[:limit]


def get_seasons(tv_id: int) -> list[TMDbSeason]:
    global _tmdb_last_error
    hdrs = _headers()
    if not hdrs:
        return []

    try:
        r = requests.get(
            f"{TMDB_BASE}/tv/{tv_id}",
            params={"language": "en-US"},
            headers=hdrs,
            timeout=10,
        )
        if r.status_code == 401:
            _tmdb_last_error = "Invalid API key"
            return []
        r.raise_for_status()
        seasons = r.json().get("seasons", []) or []
        result = [
            TMDbSeason(season_number=s["season_number"])
            for s in seasons
            if isinstance(s.get("season_number"), int) and s["season_number"] > 0
        ]
        _tmdb_last_error = None
        return sorted(result, key=lambda s: s.season_number)
    except Exception as e:
        _tmdb_last_error = f"seasons error: {e}"
        logging.error("TMDb seasons error: %s", e)
        return []
