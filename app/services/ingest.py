import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Optional
import zipfile

from app.services.naming import bulk_rename

SERIES_TYPES = {"series", "anime", "docuseries"}

VIDEO_EXT = {".mkv", ".mp4", ".avi", ".mov"}
ARCHIVE_EXT = {".rar", ".zip", ".r00", ".001"}


def _find_archives(root: Path):
    for p in root.rglob("*"):
        if p.is_file() and p.suffix.lower() in ARCHIVE_EXT:
            yield p


def _extract_zip(path: Path, dest: Path):
    with zipfile.ZipFile(path, "r") as zf:
        zf.extractall(dest)


def _extract_rar(path: Path, dest: Path):
    # unrar must be in PATH
    cmd = ["unrar", "x", "-o+", str(path), str(dest)]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def extract_archives(root: Path) -> None:
    for arc in _find_archives(root):
        try:
            dest = arc.parent
            if arc.suffix.lower() == ".zip":
                _extract_zip(arc, dest)
            else:
                _extract_rar(arc, dest)
            arc.unlink(missing_ok=True)
        except Exception as e:
            logging.error("Error extracting %s: %s", arc, e)


def process_directory(directory: str, title: str, season_hint: Optional[int], lib_type: Optional[str] = None) -> None:
    root = Path(directory)
    if not root.exists():
        return
    extract_archives(root)
    if lib_type in SERIES_TYPES:
        bulk_rename(root, title, season_hint)
