import re
import os
from pathlib import Path
from typing import Optional, Tuple

RX_SE = re.compile(r"S?(\d{1,2})[xEex](\d{1,3})", re.I)
RX_SE_ALT = re.compile(r"S(\d{1,2})E(\d{1,3})", re.I)
RX_E_ONLY = re.compile(r"E(\d{1,3})", re.I)
RX_THREE = re.compile(r"(?<!\d)(\d)(\d{2})(?!\d)")  # 101 -> S01E01

VIDEO_EXT = {".mkv", ".mp4", ".avi", ".mov"}


def parse_season_episode(name: str, season_hint: Optional[int] = None) -> Tuple[Optional[int], Optional[int]]:
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


def target_name(title: str, season: Optional[int], episode: Optional[int], ext: str) -> Optional[str]:
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
    # evitar colisión
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
