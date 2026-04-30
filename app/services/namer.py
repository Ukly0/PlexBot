"""Plex-safe filename sanitization, SxxExx parsing, and bulk renaming.

CRITICAL: Every filename/folder written to disk MUST go through these helpers.
Plex requires ASCII-only names: no accents, no ñ, no special chars.
"""

import re
import unicodedata
from pathlib import Path
from typing import Optional

RX_SE = re.compile(r"S?(\d{1,2})[xEex](\d{1,3})", re.I)
RX_SE_ALT = re.compile(r"S(\d{1,2})E(\d{1,3})", re.I)
RX_E_ONLY = re.compile(r"E(\d{1,3})", re.I)
RX_THREE = re.compile(r"(?<!\d)(\d)(\d{2})(?!\d)")

VIDEO_EXT = {
    ".mkv", ".mp4", ".avi", ".mov", ".ts", ".m4v",
    ".webm", ".flv", ".wmv", ".mpg", ".mpeg", ".m2ts", ".mts",
}
INVALID_FS_CHARS = set('<>:"/\\|?*')


def _ascii_safe(text: str) -> str:
    """Normalize to closest ASCII: ñ→n, é→e, etc."""
    nfkd = unicodedata.normalize("NFKD", text)
    return nfkd.encode("ascii", "ignore").decode("ascii")


def safe_title(title: str) -> str:
    """
    Sanitize a title for Plex filesystem use:
    - Normalize unicode (accents, ñ → ASCII equivalents)
    - Remove invalid characters
    - Collapse whitespace
    """
    if not title:
        return "Content"
    cleaned = _ascii_safe(title)
    cleaned = "".join(" " if ch in INVALID_FS_CHARS else ch for ch in cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip().strip(".")
    return cleaned or "Content"


def parse_season_episode(
    name: str, season_hint: Optional[int] = None
) -> tuple[Optional[int], Optional[int]]:
    m = RX_SE.search(name) or RX_SE_ALT.search(name)
    if m:
        s, e = m.groups()
        return int(s), int(e)
    m = RX_E_ONLY.search(name)
    if m and season_hint is not None:
        return season_hint, int(m.group(1))
    m = RX_THREE.search(name)
    if m:
        s, e = m.groups()
        return int(s), int(e)
    return None, None


def target_name(
    title: str, season: Optional[int], episode: Optional[int], ext: str
) -> Optional[str]:
    if season is None or episode is None:
        return None
    return f"S{season:02d}E{episode:02d} - {title}{ext}"


def rename_video(path: Path, title: str, season_hint: Optional[int]) -> Path:
    season, episode = parse_season_episode(path.name, season_hint)
    ext = path.suffix.lower()
    if ext not in VIDEO_EXT:
        return path
    new_name = target_name(title, season, episode, ext)
    if not new_name:
        return path
    target = path.with_name(new_name)
    if target == path:
        return path
    if target.exists():
        base = target.stem
        suffix = target.suffix
        n = 1
        while target.exists():
            target = target.with_name(f"{base}-dup{n}{suffix}")
            n += 1
    path.rename(target)
    return target


def bulk_rename(root: Path, title: str, season_hint: Optional[int]) -> None:
    for p in root.rglob("*"):
        if p.is_file() and p.suffix.lower() in VIDEO_EXT:
            try:
                rename_video(p, title, season_hint)
            except Exception:
                continue


def _movie_title_with_year(title: str, year: Optional[int]) -> str:
    sanitized = safe_title(title)
    sanitized = re.sub(r"\s*\(\d{4}\)$", "", sanitized).strip()
    if year:
        sanitized = f"{sanitized} ({year})"
    return sanitized or "Content"


def rename_movie_files(root: Path, title: str, year: Optional[int]) -> None:
    target_base = _movie_title_with_year(title, year)
    for p in root.rglob("*"):
        if p.is_file() and p.suffix.lower() in VIDEO_EXT:
            try:
                target = p.with_name(f"{target_base}{p.suffix.lower()}")
                if target == p:
                    continue
                if target.exists():
                    base = target.stem
                    suffix = target.suffix
                    n = 1
                    while target.exists():
                        target = target.with_name(f"{base}-dup{n}{suffix}")
                        n += 1
                p.rename(target)
            except Exception:
                continue
