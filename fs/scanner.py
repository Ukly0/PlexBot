# cinebot/services/scanner.py
"""
Filesystem scanner (Phase 2)
- Read-only: does NOT move or create files. Only indexes what exists.
- Idempotent: re-running does not duplicate records.
- Multi-disk: run per Library root; can chain for all.

Key rules:
- Show = first-level folder: <root>/<Show>/...
- Season = second-level folder "Season N" | "Temporada N" | "SNN" | "TNN"
           or inferred from filename (SxxEyy, 1x02, Eyy)
           (fallback to Season 1 if none detected).
- Episode = valid video file; number optional if it can’t be inferred.

Typical usage:
    stats = scan_library(session, library, verbose=True)
    # or
    scan_all_libraries(session, verbose=True)
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional, Tuple
import re
import unicodedata
import os

from sqlalchemy.orm import Session

from store.models import Library, LibraryType
from store.repos import LibraryRepo, ShowRepo, SeasonRepo, EpisodeRepo
from app.services.naming import VIDEO_EXT as NAMING_VIDEO_EXT


# ----------------------------
# Naming/parse config
# ----------------------------

VIDEO_EXT = NAMING_VIDEO_EXT
SUB_EXT = {".srt", ".ass"}  # remove from VALID_EXT if you don’t want subtitles indexed
VALID_EXT = VIDEO_EXT 

IGNORED_EXT = {".rar", ".zip", ".7z", ".nfo"}
IGNORED_DIRS = {".git", ".idea", "__pycache__", ".DS_Store"}
IGNORED_FILE_PATTERNS = re.compile(r"(^|[._-])sample($|[._-])", re.I)

RX_SE = re.compile(r"(?:S?(\d{1,2})[xEex](\d{1,3}))|(?:S(\d{1,2})E(\d{1,3}))", re.I)
RX_EONLY = re.compile(r"(?<!\w)E(\d{1,3})(?!\w)", re.I)
RX_THREE = re.compile(r"(?<!\d)(\d)(\d{2})(?!\d)")  # 101 → S01E01 (opcional)


# ----------------------------
# Naming helpers
# ----------------------------

def safe_unicode(s: str) -> str:
    # Replace invalid chars with '?'
    return s.encode("utf-8", "replace").decode("utf-8")

def _strip_diacritics(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")

def slugify(title: str) -> str:
    """
    Simple slug: lowercase, no diacritics, separators -> '-', strip common noise.
    """
    title = re.sub(r"\[(?:.+?)\]|\((?:1080p|720p|x264|x265|WEB[-.]DL|BluRay|HDTV).*?\)", "", title, flags=re.I)
    title = title.replace(".", " ")
    s = _strip_diacritics(title).lower().strip()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return s or "untitled"

def season_from_dirname(name: str) -> Optional[int]:
    name = name.strip()
    # Season 1 / Temporada 1
    m = re.match(r"^(?:season|temporada)\s*(\d{1,2})$", name, re.I)
    if m:
        return int(m.group(1))
    # S01 / T01
    m = re.match(r"^[st](\d{1,2})$", name, re.I)
    if m:
        return int(m.group(1))
    return None

def parse_season_episode_from_filename(fname: str) -> Tuple[Optional[int], Optional[int]]:
    """
    Return (season, episode) if inferred, otherwise (None, None) or (S,None)/(None,E).
    """
    m = RX_SE.search(fname)
    if m:
        g = m.groups()
        s = g[0] or g[2]
        e = g[1] or g[3]
        return (int(s) if s else None, int(e) if e else None)

    # Eyy only if season known by context (caller decides)
    m = RX_EONLY.search(fname)
    if m:
        return (None, int(m.group(1)))

    # 101 → S01E01 (optional; may cause collisions)
    m = RX_THREE.search(fname)
    if m:
        s, e = int(m.group(1)), int(m.group(2))
        return (s, e)

    return (None, None)


# ----------------------------
# Stats
# ----------------------------

@dataclass
class ScanStats:
    shows_new: int = 0
    seasons_new: int = 0
    episodes_new: int = 0
    files_seen: int = 0

    def __iadd__(self, other: "ScanStats"):
        self.shows_new += other.shows_new
        self.seasons_new += other.seasons_new
        self.episodes_new += other.episodes_new
        self.files_seen += other.files_seen
        return self


# ----------------------------
# Scan core
# ----------------------------

def _iter_valid_files(root: Path) -> Iterable[Path]:
    """
    Walk recursively under root and yield ONLY valid files (allowed extensions),
    skipping hidden entries, 'sample' patterns, and ignored extensions.
    """
    if not root.exists():
        return
    for p in root.rglob("*"):
        # Skip hidden/ignored dirs
        if p.is_dir():
            if p.name in IGNORED_DIRS or p.name.startswith("."):
                continue
            else:
                continue  # files will be yielded when p is a file
        # Files
        if p.name.startswith("."):
            continue
        if IGNORED_FILE_PATTERNS.search(p.name):
            continue

        ext = p.suffix.lower()
        if ext in IGNORED_EXT:
            continue
        if ext not in VALID_EXT:
            continue
        yield p


def _show_and_season_from_path(file_path: Path, lib_root: Path) -> Tuple[str, Optional[int], Optional[int]]:
    """
    Derive show_name, season_number, and year from a path:
    - show_name = first segment under root
    - season_number = from season folder or filename
    - year = if show name looks like 'Name(YYYY)'
    """
    rel = file_path.relative_to(lib_root)
    parts = rel.parts
    if len(parts) < 1:
        raise ValueError(f"Ruta inesperada bajo {lib_root}: {file_path}")

    raw_show_name = safe_unicode(parts[0])
    m = re.match(r"^(.*)\((\d{4})\)$", raw_show_name)
    if m:
        show_name = m.group(1).strip()
        year = int(m.group(2))
    else:
        show_name = raw_show_name
        year = None

    season_number = None
    if len(parts) >= 2:
        season_number = season_from_dirname(parts[1])
    if season_number is None:
        s_from_name, _ = parse_season_episode_from_filename(file_path.name)
        if s_from_name is not None:
            season_number = s_from_name
    if season_number is None:
        season_number = 1

    return show_name, season_number, year


def _episode_number_from_filename(file_path: Path, season_number_hint: Optional[int]) -> Optional[int]:
    s, e = parse_season_episode_from_filename(file_path.name)
    if e is not None:
        return e
    # if only Eyy and season hint provided, respect hint and return episode
    m = RX_EONLY.search(file_path.name)
    if m and season_number_hint:
        return int(m.group(1))
    return None


def scan_library(
    s: Session,
    library: Library,
    *,
    verbose: bool = False,
) -> ScanStats:
    """
    Scan a given Library root and persist Show/Season/Episode.

    - Unique Show per (slug, kind).
    - Unique Season per (show, library, number).
    - Unique Episode per (season, file_path).

    Returns counters of new elements.
    """
    stats = ScanStats()
    root = Path(library.root)

    show_repo = ShowRepo(s)
    season_repo = SeasonRepo(s)
    episode_repo = EpisodeRepo(s)

    # Cache ligera por rendimiento
    show_cache: dict[tuple[str, LibraryType], int] = {}            # (slug, kind) -> show_id
    season_cache: dict[tuple[int, int, int], int] = {}             # (show_id, lib_id, number) -> season_id

    if verbose:
        print(f"[scan] Library {library.name} ({library.type.value}) → {root}")

    for file in _iter_valid_files(root):
        stats.files_seen += 1

        try:
            show_name, season_num, show_year = _show_and_season_from_path(file, root)
        except Exception as e:
            if verbose:
                print(f"[scan] Skipping {file}: {e}")
            continue

        show_slug = slugify(show_name)
        kind = library.type

        # SHOW (get_or_create + cache)
        key = (show_slug, kind)
        show_id = show_cache.get(key)
        if show_id is None:
            show = show_repo.get_or_create(title=show_name, slug=show_slug, kind=kind, year=show_year)
            if show.id not in show_cache.values():
                stats.shows_new += 1 if s.is_modified(show, include_collections=False) else 0  # approximate
            show_id = show.id
            show_cache[key] = show_id

        # SEASON (upsert + cache)
        skey = (show_id, library.id, season_num)
        season_id = season_cache.get(skey)
        if season_id is None:
            default_season_dir_name = f"Season {season_num}"
            season_dir = Path(root / show_name / default_season_dir_name)
            # Ensure path is sanitized
            safe_season_dir = safe_unicode(str(season_dir))
            season = season_repo.upsert(
                show_id=show_id,
                library_id=library.id,
                number=season_num,
                path=safe_season_dir,
            )
            if season.id not in season_cache.values():
                # repo doesn't tell if it was newly inserted; rely on cache heuristic
                stats.seasons_new += 1 if s.is_modified(season, include_collections=False) else 0
            season_id = season.id
            season_cache[skey] = season_id

        # EPISODE (upsert)
        ep_num = _episode_number_from_filename(file, season_num)
        size = None
        try:
            size = file.stat().st_size
        except OSError:
            pass

        before = s.new.copy()
        # Sanitize file_path before storing
        safe_file_path = safe_unicode(str(file))
        episode = episode_repo.upsert(
            season_id=season_id,
            file_path=safe_file_path,
            number=ep_num,
            size=size,
        )
        # Heuristic for "new" count
        stats.episodes_new += 1 if episode in s.new or len(before) != len(s.new) else 0

    s.commit()
    if verbose:
        print(f"[scan] Done {library.name}: +{stats.shows_new} shows, +{stats.seasons_new} seasons, +{stats.episodes_new} episodes (files seen: {stats.files_seen})")
    return stats


def scan_libraries_by_type(
    s: Session,
    lib_type: LibraryType,
    *,
    verbose: bool = False,
) -> ScanStats:
    """
    Scan all libraries of a given type (series, anime, docuseries, documentary, movie).
    """
    stats = ScanStats()
    libs = LibraryRepo(s).list_by_type(lib_type)
    for lib in libs:
        stats += scan_library(s, lib, verbose=verbose)
    return stats


def scan_all_libraries(
    s: Session,
    *,
    verbose: bool = False,
) -> ScanStats:
    """
    Scan ALL registered libraries.
    """
    stats = ScanStats()
    for t in (
        LibraryType.series,
        LibraryType.anime,
        LibraryType.docuseries,
        LibraryType.documentary,
        getattr(LibraryType, "movies", None) or LibraryType.movie,
    ):
        if t is None:
            continue
        stats += scan_libraries_by_type(s, t, verbose=verbose)
    return stats
