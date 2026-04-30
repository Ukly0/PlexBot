"""Multipart RAR/ZIP detection and extraction."""

from __future__ import annotations

import logging
import re
import subprocess
import zipfile
from pathlib import Path

ARCHIVE_CANDIDATES = {".zip", ".rar", ".r00", ".r01", ".r02", ".001"}


def _is_archive_part(path: Path) -> bool:
    suffixes = [s.lower() for s in path.suffixes]
    if not suffixes:
        return False
    return any(s in ARCHIVE_CANDIDATES for s in suffixes) or any(
        s.startswith(".r") and s[1:].isdigit() for s in suffixes
    )


def _iter_archives(root: Path):
    for p in sorted(root.rglob("*")):
        if p.is_file() and _is_archive_part(p):
            yield p


def _detect_archive_type(path: Path) -> str | None:
    try:
        with open(path, "rb") as f:
            magic = f.read(8)
        if magic.startswith(b"PK"):
            return "zip"
        if magic.startswith(b"Rar!"):
            return "rar"
    except Exception:
        pass

    suffixes = [s.lower() for s in path.suffixes]
    if any(s == ".zip" for s in suffixes):
        return "zip"
    if any(
        s in {".rar", ".r00", ".r01", ".r02"}
        or (s.startswith(".r") and s[1:].isdigit())
        for s in suffixes
    ):
        return "rar"
    return None


def _archive_key(path: Path) -> str:
    stem = path.stem.lower()
    stem = re.sub(r"\.part\d+$", "", stem)
    return stem


def _volume_index(path: Path) -> int:
    suffixes = [s.lower() for s in path.suffixes]
    for suf in reversed(suffixes):
        match = re.match(r"\.part(\d+)", suf)
        if match:
            return int(match.group(1))
    for suf in suffixes:
        if suf.startswith(".r") and suf[2:].isdigit():
            return int(suf[2:]) + 1
        if suf == ".001":
            return 1
    return 0


def _pick_archive_roots(paths: list[Path]) -> list[Path]:
    best: dict[str, tuple[int, Path]] = {}
    for p in paths:
        key = _archive_key(p)
        idx = _volume_index(p)
        prev = best.get(key)
        if prev is None or idx < prev[0]:
            best[key] = (idx, p)
    return [entry[1] for entry in best.values()]


def _extract_zip(path: Path, dest: Path):
    with zipfile.ZipFile(path, "r") as zf:
        zf.extractall(dest)


def _extract_rar(path: Path, dest: Path):
    cmd = ["unrar", "x", "-o+", "-p-", str(path), str(dest)]
    result = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=900)
    if result.returncode != 0:
        stderr = result.stderr.decode(errors="ignore") if result.stderr else ""
        raise RuntimeError(f"unrar exit {result.returncode}: {stderr.strip()}")


def _cleanup_archives(root: Path, key: str) -> None:
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if _archive_key(p) != key:
            continue
        if not _is_archive_part(p):
            continue
        try:
            p.unlink(missing_ok=True)
        except Exception as e:
            logging.debug("Could not delete archive part %s: %s", p, e)


def extract_archives(root: Path) -> None:
    processed: set[str] = set()
    archives = _pick_archive_roots(list(_iter_archives(root)))
    for arc in archives:
        archive_type = _detect_archive_type(arc)
        if not archive_type:
            continue
        key = _archive_key(arc)
        if key in processed:
            continue
        processed.add(key)
        try:
            dest = arc.parent
            logging.info("Extracting %s (%s)", arc, archive_type)
            if archive_type == "zip":
                _extract_zip(arc, dest)
            else:
                _extract_rar(arc, dest)
            _cleanup_archives(root, key)
            logging.info("Extraction finished for %s", arc)
        except subprocess.TimeoutExpired:
            logging.error("Extraction timeout for %s", arc)
        except Exception as e:
            logging.error("Error extracting %s (%s): %s", arc, archive_type, e)
