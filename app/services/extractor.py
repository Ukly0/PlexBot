"""Multipart RAR/ZIP/7z detection and extraction."""

from __future__ import annotations

import logging
import os
import re
import subprocess
import zipfile
from pathlib import Path

VIDEO_EXT = {
    ".mkv", ".mp4", ".avi", ".mov", ".ts", ".m4v",
    ".webm", ".flv", ".wmv", ".mpg", ".mpeg", ".m2ts", ".mts",
}

ARCHIVE_SUFFIXES = {
    ".zip", ".rar", ".7z",
    ".001", ".002", ".003",
    ".r00", ".r01", ".r02",
}


def _is_archive_part(path: Path) -> bool:
    suffixes = [s.lower() for s in path.suffixes]
    if not suffixes:
        return False
    for s in suffixes:
        if s in ARCHIVE_SUFFIXES:
            return True
        if s.startswith(".r") and len(s) >= 3 and s[2:].isdigit():
            return True
        if s.startswith(".part") and s[5:].rstrip(".").isdigit():
            return True
    # compound suffixes like .tar.gz
    name_lower = path.name.lower()
    if name_lower.endswith(".tar.gz") or name_lower.endswith(".tar.bz2") or name_lower.endswith(".tar.xz"):
        return True
    return False


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
        if magic.startswith(b"\x37\x7a\xbc\xaf\x27\x1c"):
            return "7z"
    except Exception:
        pass

    suffixes = [s.lower() for s in path.suffixes]
    name_lower = path.name.lower()

    if name_lower.endswith(".tar.gz") or name_lower.endswith(".tgz"):
        return "tar"
    if name_lower.endswith(".tar.bz2") or name_lower.endswith(".tbz2"):
        return "tar"
    if name_lower.endswith(".tar.xz"):
        return "tar"

    if any(s == ".zip" for s in suffixes):
        return "zip"
    if any(
        s in {".rar", ".r00", ".r01", ".r02"}
        or (s.startswith(".r") and len(s) >= 3 and s[2:].isdigit())
        for s in suffixes
    ):
        return "rar"
    if any(s == ".7z" for s in suffixes):
        return "7z"
    return None


def _archive_key(path: Path) -> str:
    stem = path.stem.lower()
    stem = re.sub(r"\.part\d+$", "", stem)
    ext = path.suffix.lower()
    if ext in {".rar", ".zip", ".7z"}:
        return f"{stem}__{ext.lstrip('.')}"
    return stem


def _volume_index(path: Path) -> int:
    suffixes = [s.lower() for s in path.suffixes]
    name_lower = path.name.lower()

    if name_lower.endswith(".tar.gz") or name_lower.endswith(".tar.bz2") or name_lower.endswith(".tar.xz"):
        return 0

    for suf in reversed(suffixes):
        match = re.match(r"\.part(\d+)", suf)
        if match:
            return int(match.group(1))
    for suf in suffixes:
        if suf.startswith(".r") and len(suf) >= 3 and suf[2:].isdigit():
            return int(suf[2:]) + 1
        if suf == ".rar":
            return 0
        if suf.startswith(".") and suf[1:].isdigit():
            return int(suf[1:])
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


def _safe_extract_zip(path: Path, dest: Path):
    dest_resolved = dest.resolve()
    with zipfile.ZipFile(path, "r") as zf:
        for member in zf.infolist():
            member_path = (dest_resolved / member.filename).resolve()
            if not str(member_path).startswith(str(dest_resolved) + os.sep) and member_path != dest_resolved:
                logging.warning("Zip-slip blocked: %s tries to escape %s", member.filename, dest)
                continue
        zf.extractall(dest)


def _extract_zip(path: Path, dest: Path):
    _safe_extract_zip(path, dest)


def _extract_rar(path: Path, dest: Path):
    cmd = ["unrar", "x", "-o+", "-p-", str(path), str(dest)]
    result = subprocess.run(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=900
    )
    stdout = result.stdout.decode(errors="ignore") if result.stdout else ""
    stderr = result.stderr.decode(errors="ignore") if result.stderr else ""
    if result.returncode != 0:
        raise RuntimeError(f"unrar exit {result.returncode}: {stderr.strip() or stdout.strip()}")


def _extract_7z(path: Path, dest: Path):
    cmd = ["7z", "x", "-y", f"-o{dest}", str(path)]
    result = subprocess.run(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=900
    )
    if result.returncode != 0:
        stderr = result.stderr.decode(errors="ignore") if result.stderr else ""
        raise RuntimeError(f"7z exit {result.returncode}: {stderr.strip()}")


def _extract_tar(path: Path, dest: Path):
    import tarfile
    try:
        with tarfile.open(path, "r:*") as tf:
            tf.extractall(dest, filter="data")
    except TypeError:
        with tarfile.open(path, "r:*") as tf:
            tf.extractall(dest)


_EXTRACTORS = {
    "zip": _extract_zip,
    "rar": _extract_rar,
    "7z": _extract_7z,
    "tar": _extract_tar,
}


def _cleanup_archives(root: Path, key: str) -> None:
    removed = 0
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if _archive_key(p) != key:
            continue
        if not _is_archive_part(p):
            continue
        try:
            p.unlink(missing_ok=True)
            removed += 1
        except Exception as e:
            logging.debug("Could not delete archive part %s: %s", p, e)
    logging.info("Cleaned up %d archive parts for key=%s", removed, key)


def _has_video_files(directory: Path) -> bool:
    for p in directory.rglob("*"):
        if p.is_file() and p.suffix.lower() in VIDEO_EXT:
            return True
    return False


def extract_archives(root: Path, max_passes: int = 3) -> None:
    processed: set[str] = set()
    for pass_num in range(1, max_passes + 1):
        archives = _pick_archive_roots(list(_iter_archives(root)))
        new_archives = [a for a in archives if _archive_key(a) not in processed]
        if not new_archives:
            logging.info("extract_archives pass %d: no new archives found, done", pass_num)
            break

        logging.info("extract_archives pass %d: found %d new archives", pass_num, len(new_archives))
        for arc in new_archives:
            archive_type = _detect_archive_type(arc)
            key = _archive_key(arc)
            if key in processed:
                continue
            if not archive_type:
                logging.warning("Skipping unknown archive type: %s", arc)
                processed.add(key)
                continue
            processed.add(key)
            extractor = _EXTRACTORS.get(archive_type)
            if not extractor:
                logging.warning("No extractor for type=%s: %s", archive_type, arc)
                continue
            dest = arc.parent
            try:
                logging.info("Extracting %s (%s)", arc, archive_type)
                extractor(arc, dest)
                # Only cleanup if we got video files out
                if _has_video_files(dest):
                    _cleanup_archives(root, key)
                    logging.info("Extraction finished for %s", arc)
                else:
                    logging.warning("No video files after extracting %s; keeping archives", arc)
            except subprocess.TimeoutExpired:
                logging.error("Extraction timeout for %s", arc)
            except Exception as e:
                logging.error("Error extracting %s (%s): %s", arc, archive_type, e)