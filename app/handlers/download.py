"""Download queue, tdl subprocess, and post-process pipeline."""

from __future__ import annotations

import asyncio
import itertools
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Awaitable, Optional

from app.services.downloader import run_download as _run_tdl
from app.services.namer import safe_title
from app.services.extractor import extract_archives
from app.state import SERIES_TYPES, MOVIE_TYPES, record_recent
from app.config import load_settings
from telegram.error import RetryAfter

TARGET_UID = 1000
TARGET_GID = 1000
DIR_MODE = 0o755
FILE_MODE = 0o644


# ── DownloadManager ─────────────────────────────────────────────

@dataclass
class TaskItem:
    id: int
    chat_id: int
    label: str
    destination: str
    content_id: str
    content_label: str
    content_destination: str
    coro_factory: Callable[[], Awaitable[None]]


@dataclass
class ContentSummary:
    content_id: str
    chat_id: int
    label: str
    destination: str
    total: int
    queued: int
    running: bool
    representative_task_id: int

    @property
    def pending(self) -> int:
        return max(0, self.total - (1 if self.running else 0))


class DownloadManager:
    """Global FIFO single-worker download queue."""

    def __init__(self, max_concurrent: int = 1):
        self.max_concurrent = max_concurrent
        self.queue: list[TaskItem] = []
        self._id_gen = itertools.count(1)
        self.child_pids: dict[int, list[int]] = {}
        self._lock = asyncio.Lock()
        self._worker: asyncio.Task | None = None
        self._current: TaskItem | None = None

    async def _ensure_worker(self):
        async with self._lock:
            if self._worker is None or self._worker.done():
                self._worker = asyncio.create_task(self._run_worker())

    async def _run_worker(self):
        while True:
            async with self._lock:
                if not self.queue:
                    self._current = None
                    self._worker = None
                    return
                item = self.queue.pop(0)
                self._current = item
                logging.info("Starting task %s (chat %s). Queued: %s", item.id, item.chat_id, len(self.queue))
            try:
                await item.coro_factory()
            except asyncio.CancelledError:
                raise
            finally:
                async with self._lock:
                    logging.info("Finished task %s (chat %s). Queued: %s", item.id, item.chat_id, len(self.queue))
                    self._current = None

    def enqueue(
        self,
        chat_id: int,
        label: str,
        destination: str,
        coro_factory: Callable[[], Awaitable[None]],
        *,
        content_id: str,
        content_label: str,
        content_destination: str,
    ) -> tuple[int, int]:
        task_id = next(self._id_gen)
        item = TaskItem(
            id=task_id,
            chat_id=chat_id,
            label=label,
            destination=destination,
            content_id=content_id,
            content_label=content_label,
            content_destination=content_destination,
            coro_factory=coro_factory,
        )
        self.queue.append(item)
        logging.info("Enqueued task %s (chat %s). Queue length: %s", task_id, chat_id, len(self.queue))
        asyncio.create_task(self._ensure_worker())
        return self.queue.index(item) + 1, task_id

    async def cancel_running(self, chat_id: int) -> int:
        cancelled = 0
        if self._current and self._current.chat_id == chat_id and self._worker:
            self._worker.cancel()
            cancelled = 1

        for pid in list(self.child_pids.get(chat_id, [])):
            try:
                os.kill(pid, 9)
            except Exception:
                pass
        self.child_pids[chat_id] = []

        if cancelled:
            try:
                await asyncio.wait_for(
                    asyncio.gather(self._worker, return_exceptions=True), timeout=5
                )
            except (asyncio.TimeoutError, Exception):
                pass
            finally:
                if self._worker and self._worker.done():
                    self._worker = None
                self._current = None

        if cancelled and self.queue:
            asyncio.create_task(self._ensure_worker())
        return cancelled

    async def cancel_all(self, chat_id: int) -> tuple[int, int]:
        running = await self.cancel_running(chat_id)
        before = len(self.queue)
        self.queue = [item for item in self.queue if item.chat_id != chat_id]
        await self._ensure_worker()
        return running, before - len(self.queue)

    async def cancel_task(self, chat_id: int, task_id: int) -> tuple[int, int]:
        target = None
        if self._current and self._current.chat_id == chat_id and self._current.id == task_id:
            target = self._current.content_id
        else:
            for item in self.queue:
                if item.chat_id == chat_id and item.id == task_id:
                    target = item.content_id
                    break
        if not target:
            return 0, 0

        running = 0
        if self._current and self._current.chat_id == chat_id and self._current.content_id == target:
            running = await self.cancel_running(chat_id)
        before = len(self.queue)
        self.queue = [
            item for item in self.queue
            if not (item.chat_id == chat_id and item.content_id == target)
        ]
        if running or before != len(self.queue):
            await self._ensure_worker()
        return running, before - len(self.queue)

    async def snapshot(self, chat_id: Optional[int] = None):
        async with self._lock:
            running = self._current if self._current and (chat_id is None or self._current.chat_id == chat_id) else None
            queued = [q for q in self.queue if chat_id is None or q.chat_id == chat_id] if chat_id is not None else list(self.queue)
        return running, queued

    async def snapshot_by_content(self, chat_id: Optional[int] = None):
        running_task, queued_tasks = await self.snapshot(chat_id)
        groups: dict[str, ContentSummary] = {}
        order: dict[str, int] = {}

        def add(item, is_running, pos):
            s = groups.get(item.content_id)
            if not s:
                s = ContentSummary(
                    content_id=item.content_id, chat_id=item.chat_id,
                    label=item.content_label, destination=item.content_destination,
                    total=0, queued=0, running=False, representative_task_id=item.id,
                )
                groups[item.content_id] = s
                order[item.content_id] = pos
            s.total += 1
            if not is_running:
                s.queued += 1
            if is_running:
                s.running = True
            if item.id < s.representative_task_id:
                s.representative_task_id = item.id

        pos = 0
        if running_task:
            add(running_task, True, pos)
            pos += 1
        for item in queued_tasks:
            add(item, False, pos)
            pos += 1

        running_summary = None
        queued_summaries = []
        for cid, summary in sorted(groups.items(), key=lambda kv: order[kv[0]]):
            if summary.running:
                running_summary = summary
            else:
                queued_summaries.append(summary)
        return running_summary, queued_summaries

    async def pending_for_content(self, chat_id: int, content_id: str) -> int:
        _, queued = await self.snapshot(chat_id)
        return sum(1 for item in queued if item.content_id == content_id)


# ── Downloads ────────────────────────────────────────────────────

def _ensure_dir(path: str) -> None:
    try:
        os.makedirs(path, exist_ok=True)
    except Exception as e:
        logging.error("Could not create %s: %s", path, e)


def _snapshot_files(path: str) -> set[str]:
    snap: set[str] = set()
    for root, _, files in os.walk(path):
        for fname in files:
            full = os.path.join(root, fname)
            rel = os.path.relpath(full, path)
            snap.add(rel)
    return snap


def _apply_permissions(path: str) -> None:
    try:
        os.chown(path, TARGET_UID, TARGET_GID)
        os.chmod(path, DIR_MODE)
    except Exception as e:
        logging.warning("Could not set perms on %s: %s", path, e)
    for root, dirs, files in os.walk(path):
        for d in dirs:
            p = os.path.join(root, d)
            try:
                os.chown(p, TARGET_UID, TARGET_GID)
                os.chmod(p, DIR_MODE)
            except Exception as e:
                logging.debug("Perms skipped for %s: %s", p, e)
        for f in files:
            p = os.path.join(root, f)
            try:
                os.chown(p, TARGET_UID, TARGET_GID)
                os.chmod(p, FILE_MODE)
            except Exception as e:
                logging.debug("Perms skipped for %s: %s", p, e)


def _process_directory(
    directory: str,
    title: str,
    season_hint: Optional[int],
    lib_type: Optional[str],
    year: Optional[int],
) -> None:
    from pathlib import Path as _Path
    from app.services.namer import bulk_rename, rename_movie_files

    root = _Path(directory)
    if not root.exists():
        logging.warning("_process_directory: path does not exist: %s", directory)
        return
    files_before = [str(p) for p in root.rglob("*") if p.is_file()]
    logging.info(
        "_process_directory: dir=%s title=%s season=%s lib_type=%s year=%s files=%s",
        directory, title, season_hint, lib_type, year, len(files_before),
    )
    extract_archives(root)
    if lib_type in SERIES_TYPES:
        logging.info("_process_directory: calling bulk_rename (series) for %s", directory)
        bulk_rename(root, title, season_hint)
    elif lib_type in MOVIE_TYPES:
        logging.info("_process_directory: calling rename_movie_files (movie) for %s", directory)
        rename_movie_files(root, title, year)
    elif lib_type is None:
        logging.warning("_process_directory: lib_type is None, inferring from season_hint (season=%s). Treating as series.", season_hint)
        bulk_rename(root, title, season_hint)
    else:
        logging.warning("_process_directory: unknown lib_type=%s, treating as series", lib_type)
        bulk_rename(root, title, season_hint)
    files_after = [str(p) for p in root.rglob("*") if p.is_file()]
    renamed = set(files_after) - set(files_before)
    logging.info("_process_directory: done. files_before=%d files_after=%d renamed=%d", len(files_before), len(files_after), len(renamed))


def _should_reset_after_enqueue(context, lib_type: str) -> bool:
    return lib_type not in SERIES_TYPES


async def queue_download(
    message,
    context,
    link: str,
    download_dir: str,
    title: str,
    season_hint: Optional[int],
    year: Optional[int] = None,
    display_name: Optional[str] = None,
    use_group: bool = False,
):
    st = load_settings()
    tdl_template = st.download.tdl_template
    cmd = tdl_template.replace("{url}", link).replace("{dir}", download_dir)
    tdl_home = st.download.tdl_home
    if use_group and "--group" not in cmd:
        cmd = f"{cmd} --group"
    env = os.environ.copy()
    if tdl_home:
        env["TDL_HOME"] = tdl_home
        try:
            os.makedirs(tdl_home, exist_ok=True)
        except Exception as e:
            logging.warning("Could not create TDL_HOME %s: %s", tdl_home, e)

    mgr: DownloadManager = context.bot_data.setdefault("dl_manager", DownloadManager())
    path_clean = download_dir
    active_lib = context.chat_data.get("active_library") or {}
    lib_type = active_lib.get("type") or context.chat_data.get("selected_type")
    human_label = display_name or title or link

    lib_type_snapshot = lib_type
    year_snapshot = year
    title_snapshot = title
    season_hint_snapshot = season_hint

    status_holder: dict = {"msg": None}

    async def _safe_send(text: str, max_retries: int = 5):
        for attempt in range(max_retries):
            try:
                return await message.reply_text(text)
            except RetryAfter as e:
                wait = getattr(e, "retry_after", 30) or 30
                logging.warning("Flood control: retrying send in %ss (attempt %s/%s)", wait, attempt + 1, max_retries)
                await asyncio.sleep(wait)
            except Exception as e:
                logging.warning("Status send failed: %s", e)
                return None
        logging.error("Status send failed after %s retries", max_retries)
        return None

    async def _safe_edit(msg, text: str, max_retries: int = 5):
        if msg is None:
            return True
        for attempt in range(max_retries):
            try:
                await msg.edit_text(text)
                return True
            except RetryAfter as e:
                wait = getattr(e, "retry_after", 30) or 30
                logging.warning("Flood control: retrying edit in %ss (attempt %s/%s)", wait, attempt + 1, max_retries)
                await asyncio.sleep(wait)
            except Exception as e:
                logging.warning("Status edit failed: %s", e)
                return False
        logging.error("Status edit failed after %s retries", max_retries)
        return False

    async def _run():
        status_msg = await _safe_send(f"▶️ Starting: {human_label}")
        status_holder["msg"] = status_msg

        before_files = _snapshot_files(path_clean)
        last_progress = {"pct": -1, "ts": 0.0}

        async def report_progress(pct: int, _line: str):
            now = time.time()
            if pct < last_progress["pct"] and last_progress["pct"] >= 0:
                return
            if (
                pct != last_progress["pct"]
                and (pct - last_progress["pct"] < 2)
                and (now - last_progress["ts"] < 5.0)
            ):
                return
            last_progress["pct"] = pct
            last_progress["ts"] = now
            bar_len = 20
            filled = int(bar_len * pct / 100)
            bar = "█" * filled + "░" * (bar_len - filled)
            await _safe_edit(status_msg, f"⬇️ Downloading: {human_label}\n[{bar}] {pct}%")

        try:
            register_pid = lambda pid: mgr.child_pids.setdefault(message.chat_id, []).append(pid)
            unregister_pid = lambda pid: mgr.child_pids.get(message.chat_id, []).remove(pid) if pid in mgr.child_pids.get(message.chat_id, []) else None
            ok = await _run_tdl(cmd, env=env, on_progress=report_progress, register_pid=register_pid, unregister_pid=unregister_pid)
        except asyncio.CancelledError:
            try:
                await _safe_edit(status_msg, f"⛔️ Cancelled: {human_label}") or await _safe_send(f"⛔️ Cancelled: {human_label}")
            except Exception:
                pass
            return

        after_files = _snapshot_files(path_clean)
        new_files = after_files - before_files

        if not ok:
            for rel in sorted(new_files):
                try:
                    os.remove(os.path.join(path_clean, rel))
                except (FileNotFoundError, Exception):
                    pass
            logging.error("Download failed; skipped post-processing for %s", path_clean)
        else:
            pending_same = await mgr.pending_for_content(message.chat_id, path_clean)
            if pending_same > 0:
                logging.info("Skipping post-process for %s (pending tasks: %s)", path_clean, pending_same)
            else:
                try:
                    logging.info(
                        "Post-processing download at %s (%s new files) lib_type=%s title=%s season=%s year=%s",
                        path_clean, len(new_files), lib_type_snapshot, title_snapshot, season_hint_snapshot, year_snapshot,
                    )
                    await asyncio.to_thread(
                        _process_directory, path_clean, title_snapshot, season_hint_snapshot, lib_type_snapshot, year_snapshot
                    )
                except Exception as e:
                    logging.error("Post-process failed: %s", e)
            try:
                await asyncio.to_thread(_apply_permissions, path_clean)
            except Exception as e:
                logging.warning("Permission fix failed for %s: %s", path_clean, e)

        if ok:
            record_recent(context, message.chat_id, title_snapshot, active_lib, season_hint_snapshot, year_snapshot)
            done_text = f"✅ Done: {human_label}\n{path_clean}"
            try:
                if not await _safe_edit(status_msg, done_text):
                    await _safe_send(done_text)
            except Exception:
                await _safe_send(done_text)
        else:
            fail_text = f"❌ Download failed: {human_label}. Check the link and try again."
            try:
                if not await _safe_edit(status_msg, fail_text):
                    await _safe_send(fail_text)
            except Exception:
                await _safe_send(fail_text)

    content_id = path_clean
    pos, _ = mgr.enqueue(
        message.chat_id,
        human_label,
        path_clean,
        _run,
        content_id=content_id,
        content_label=title,
        content_destination=path_clean,
    )
    if pos > 1:
        try:
            await message.reply_text(f"⏳ Added to queue (position {pos}).")
        except Exception:
            pass


async def set_destination(
    update,
    context,
    library: dict,
    title: str,
    year: Optional[int],
    season: Optional[int],
) -> str:
    root = library["root"]
    base_title = title or "Content"
    if year:
        base_title = f"{base_title} ({year})"
    folder_name = safe_title(base_title)
    base_dir = os.path.join(root, folder_name)
    download_dir = base_dir

    if season is not None:
        download_dir = os.path.join(base_dir, f"Season {season:02d}")
        context.chat_data["season_hint"] = season
    else:
        context.chat_data.pop("season_hint", None)

    _ensure_dir(download_dir)
    context.chat_data["download_dir"] = download_dir
    context.chat_data["active_library"] = library
    context.chat_data["selected_type"] = library.get("type", "movie")
    return download_dir


def find_existing_library(
    title: str,
    year: Optional[int],
    libraries: list,
    lib_types: set = SERIES_TYPES,
) -> Optional[dict]:
    """Check if a title+year folder already exists under a series-type library root.
    Returns the library dict if found, None otherwise."""
    base_title = title or "Content"
    if year:
        base_title = f"{base_title} ({year})"
    folder_name = safe_title(base_title)
    for lib in libraries:
        if lib.type not in lib_types:
            continue
        candidate = os.path.join(lib.root, folder_name)
        if os.path.isdir(candidate):
            return {"name": lib.name, "root": lib.root, "type": lib.type}
    return None
