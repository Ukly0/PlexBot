import logging
import re
import subprocess
from pathlib import Path
from typing import Optional

import zipfile

from app.services.naming import bulk_rename, rename_movie_files
SERIES_TYPES = {"series", "anime", "docuseries"}
MOVIE_TYPES = {"movies", "documentary"}

ARCHIVE_CANDIDATES = {".zip", ".rar", ".r00", ".r01", ".r02", ".001"}


def _iter_archives(root: Path):
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        suffixes = [s.lower() for s in p.suffixes]
        if not suffixes:
            continue
        if any(s in ARCHIVE_CANDIDATES for s in suffixes) or any(s.startswith(".r") and s[1:].isdigit() for s in suffixes):
            yield p


def _detect_archive_type(path: Path) -> Optional[str]:
    """Return 'zip' or 'rar' if the file looks like an archive."""
    try:
        with open(path, "rb") as f:
            magic = f.read(8)
        if magic.startswith(b"PK"):
            return "zip"
        if magic.startswith(b"Rar!"):
            return "rar"
    except Exception as e:
        logging.debug("Could not read archive header for %s: %s", path, e)

    suffixes = [s.lower() for s in path.suffixes]
    if any(s == ".zip" for s in suffixes):
        return "zip"
    if any(s in {".rar", ".r00", ".r01", ".r02"} or (s.startswith(".r") and s[1:].isdigit()) for s in suffixes):
        return "rar"
    return None


def _archive_key(path: Path) -> str:
    """Normalize archive name to avoid extracting all volumes of the same set."""
    stem = path.stem.lower()
    stem = re.sub(r"\.part\d+$", "", stem)
    return stem


def _extract_zip(path: Path, dest: Path):
    with zipfile.ZipFile(path, "r") as zf:
        zf.extractall(dest)


def _extract_rar(path: Path, dest: Path):
    # unrar must be in PATH
    cmd = ["unrar", "x", "-o+", str(path), str(dest)]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def extract_archives(root: Path) -> None:
    processed: set[str] = set()
    for arc in _iter_archives(root):
        archive_type = _detect_archive_type(arc)
        if not archive_type:
            continue
        key = _archive_key(arc)
        if key in processed:
            continue
        processed.add(key)
        try:
            dest = arc.parent
            if archive_type == "zip":
                _extract_zip(arc, dest)
            else:
                _extract_rar(arc, dest)
            arc.unlink(missing_ok=True)
        except Exception as e:
            logging.error("Error extracting %s (%s): %s", arc, archive_type, e)


def process_directory(
    directory: str,
    title: str,
    season_hint: Optional[int],
    lib_type: Optional[str] = None,
    year: Optional[int] = None,
) -> None:
    root = Path(directory)
    if not root.exists():
        return
    extract_archives(root)
    if lib_type in SERIES_TYPES:
        bulk_rename(root, title, season_hint)
    elif lib_type in MOVIE_TYPES:
        rename_movie_files(root, title, year)
