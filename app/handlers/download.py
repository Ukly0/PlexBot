"""Download queue, tdl subprocess, and post-process pipeline."""

from __future__ import annotations

import asyncio
import itertools
import logging
import os
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Awaitable, Optional

from app.services.downloader import run_download as _run_tdl
from app.services.namer import safe_title, parse_season_episode, VIDEO_EXT
from app.services.extractor import extract_archives
from app.state import SERIES_TYPES, MOVIE_TYPES, record_recent, title_with_year
from app.config import load_settings
from app.handlers.dashboard import (
    ensure_dashboard,
    update_dashboard,
    set_live,
    clear_live,
    push_recent,
)
from app.handlers.telegram_utils import edit_message_safely, safe_answer
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import RetryAfter, TimedOut


# tdl renders progress with go-pretty (verified against tdl/pkg/prog/prog.go +
# go-pretty StyleOptionsDefault). Speed uses binary units + "/s" suffix
# (e.g. "12.34MiB/s"); ETA uses the "~ETA" prefix at second precision
# (e.g. "~ETA 5s", "~ETA 1m30s"). Strip ANSI colour/cursor codes first —
# tdl colours done/fail strings, and stray escapes would break the match.
_RX_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_RX_SPEED = re.compile(r"(\d+(?:\.\d+)?\s?[KMGT]?i?B/s)", re.I)
# ETA value is a Go duration at second precision: "5s", "1m30s", "1h2m3s".
# Require the verified "~ETA" prefix (go-pretty's default ETAString) so a
# stray "eta" substring elsewhere on the line can't be mistaken for it.
_RX_ETA = re.compile(r"~ETA[:\s]*(\d[\dhms]*)", re.I)

# Substring → user-facing explanation for common tdl failures.
_ERROR_HINTS = (
    ("flood_wait", "Telegram is rate-limiting the account (FLOOD_WAIT). It clears in a few minutes — try again later."),
    ("flood wait", "Telegram is rate-limiting the account (FLOOD_WAIT). It clears in a few minutes — try again later."),
    ("channel_private", "The source channel is private — the tdl account must join it first."),
    ("username_not_occupied", "That channel or user no longer exists."),
    ("message_id_invalid", "The message was deleted or the link points to nothing."),
    ("msg not found", "The message was deleted or the link points to nothing."),
    ("not authorized", "tdl session expired — re-run `tdl login`."),
    ("stalled", "The download stalled (no output from tdl). Check connectivity and retry."),
)


def _parse_speed_eta(line: str) -> tuple[Optional[str], Optional[str]]:
    speed = eta = None
    if line:
        line = _RX_ANSI.sub("", line)
        m = _RX_SPEED.search(line)
        if m:
            speed = m.group(1).replace(" ", "")
        m = _RX_ETA.search(line)
        if m:
            eta = m.group(1)
    return speed, eta


def _friendly_error(tail: str) -> str:
    low = (tail or "").lower()
    for needle, hint in _ERROR_HINTS:
        if needle in low:
            return hint
    tail = (tail or "").strip()
    if tail:
        return f"tdl: …{tail[-160:]}" if len(tail) > 160 else f"tdl: {tail}"
    return "Check the link and try again."


def _next_batch_id(context) -> int:
    counter = context.bot_data.setdefault("_download_batch_counter", itertools.count(1))
    return next(counter)


def _build_tdl_args(template: str, link: str, download_dir: str, use_group: bool) -> list[str]:
    """Render the configured tdl command as argv, not shell text."""
    args = [
        part.replace("{url}", link).replace("{dir}", download_dir)
        for part in shlex.split(template)
    ]
    if use_group and "--group" not in args:
        args.append("--group")
    return args


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
    batch_id: Optional[int]
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
            except Exception:
                logging.exception("Task %s (chat %s) failed unexpectedly", item.id, item.chat_id)
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
        batch_id: Optional[int] = None,
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
            batch_id=batch_id,
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

    async def batch_ids_for_task(self, chat_id: int, task_id: int) -> set[int]:
        target = None
        batch_ids: set[int] = set()
        if self._current and self._current.chat_id == chat_id and self._current.id == task_id:
            target = self._current.content_id
        else:
            for item in self.queue:
                if item.chat_id == chat_id and item.id == task_id:
                    target = item.content_id
                    break
        if not target:
            return batch_ids
        if (
            self._current
            and self._current.chat_id == chat_id
            and self._current.content_id == target
            and self._current.batch_id is not None
        ):
            batch_ids.add(self._current.batch_id)
        for item in self.queue:
            if item.chat_id == chat_id and item.content_id == target and item.batch_id is not None:
                batch_ids.add(item.batch_id)
        return batch_ids

    async def batch_ids_for_chat(self, chat_id: int) -> set[int]:
        batch_ids: set[int] = set()
        if self._current and self._current.chat_id == chat_id and self._current.batch_id is not None:
            batch_ids.add(self._current.batch_id)
        for item in self.queue:
            if item.chat_id == chat_id and item.batch_id is not None:
                batch_ids.add(item.batch_id)
        return batch_ids

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


def _apply_permissions(path: str, puid: int, pgid: int, dir_mode: int, file_mode: int) -> None:
    try:
        os.chown(path, puid, pgid)
        os.chmod(path, dir_mode)
    except Exception as e:
        logging.warning("Could not set perms on %s: %s", path, e)
    for root, dirs, files in os.walk(path):
        for d in dirs:
            p = os.path.join(root, d)
            try:
                os.chown(p, puid, pgid)
                os.chmod(p, dir_mode)
            except Exception as e:
                logging.debug("Perms skipped for %s: %s", p, e)
        for f in files:
            p = os.path.join(root, f)
            try:
                os.chown(p, puid, pgid)
                os.chmod(p, file_mode)
            except Exception as e:
                logging.debug("Perms skipped for %s: %s", p, e)


def _process_directory(
    directory: str,
    title: str,
    season_hint: Optional[int],
    lib_type: Optional[str],
    year: Optional[int],
    replace_existing: bool = False,
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
        bulk_rename(root, title, season_hint, replace_existing)
    elif lib_type in MOVIE_TYPES:
        logging.info("_process_directory: calling rename_movie_files (movie) for %s", directory)
        rename_movie_files(root, title, year, replace_existing)
    elif lib_type is None:
        logging.warning("_process_directory: lib_type is None, inferring from season_hint (season=%s). Treating as series.", season_hint)
        bulk_rename(root, title, season_hint, replace_existing)
    else:
        logging.warning("_process_directory: unknown lib_type=%s, treating as series", lib_type)
        bulk_rename(root, title, season_hint, replace_existing)
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
    notify_queued: bool = True,
    batch_id: Optional[int] = None,
    batch_index: Optional[int] = None,
    batch_total: Optional[int] = None,
    direct_file_id: Optional[str] = None,
    direct_filename: Optional[str] = None,
    replace_existing: bool = False,
):
    st = load_settings()
    perm = st.permissions
    is_direct = direct_file_id is not None
    if not is_direct:
        tdl_template = st.download.tdl_template
        cmd = _build_tdl_args(tdl_template, link, download_dir, use_group)
    tdl_home = st.download.tdl_home
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
    batch_mode = batch_id is not None and (batch_total or 0) > 1

    async def _safe_send(text: str, max_retries: int = 3):
        for attempt in range(max_retries):
            try:
                return await message.reply_text(text)
            except RetryAfter as e:
                wait = getattr(e, "retry_after", 30) or 30
                logging.warning("Flood control: retrying send in %ss (attempt %s/%s)", wait, attempt + 1, max_retries)
                await asyncio.sleep(wait)
            except TimedOut:
                logging.warning("Timed out sending message (attempt %s/%s)", attempt + 1, max_retries)
                await asyncio.sleep(2)
            except Exception as e:
                err_str = str(e)
                if "not modified" in err_str.lower():
                    return None
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
            except TimedOut:
                logging.warning("Timed out editing message (attempt %s/%s)", attempt + 1, max_retries)
                await asyncio.sleep(2)
            except Exception as e:
                err_str = str(e)
                if "not modified" in err_str.lower():
                    return True
                logging.warning("Status edit failed: %s", e)
                return False
        logging.error("Status edit failed after %s retries", max_retries)
        return False

    async def _safe_batch_edit(text: str):
        if not batch_mode:
            return False
        batches = context.bot_data.get("download_batches", {})
        batch = batches.get(batch_id)
        if not batch:
            return False
        msg = batch.get("msg")
        if msg is None:
            msg = await _safe_send(text)
            batch["msg"] = msg
            return msg is not None
        ok = await _safe_edit(msg, text)
        return ok

    async def _mark_batch_cancelled():
        if not batch_mode:
            return
        batches = context.bot_data.get("download_batches", {})
        batch = batches.get(batch_id)
        if not batch:
            return
        label = batch.get("label", title_snapshot)
        await _safe_batch_edit(f"⛔️ Batch cancelled: {label}\n{path_clean}")
        batches.pop(batch_id, None)

    async def _run():
        status_msg = None
        verb = "Processing" if is_direct else "Downloading"
        if batch_mode:
            await _safe_batch_edit(
                f"⬇️ Batch: {context.bot_data.get('download_batches', {}).get(batch_id, {}).get('label', title_snapshot)}\n"
                f"{verb} {batch_index}/{batch_total}: {human_label}"
            )
        else:
            status_msg = await _safe_send(f"▶️ Starting: {human_label}")
        status_holder["msg"] = status_msg

        # Live progress is rendered on the pinned dashboard, not as chat spam.
        set_live(
            context, message.chat_id,
            label=human_label, pct=None, speed=None, eta=None,
            batch_index=batch_index if batch_mode else None,
            batch_total=batch_total if batch_mode else None,
        )
        await update_dashboard(context, message.chat_id, force=True)

        before_files = _snapshot_files(path_clean)

        async def report_progress(pct: int, line: str):
            speed, eta = _parse_speed_eta(line)
            set_live(context, message.chat_id, pct=pct, speed=speed, eta=eta)
            await update_dashboard(context, message.chat_id)

        err_tail = ""
        if is_direct:
            from app.services.telegram_download import download_telegram_file
            dl_filename = direct_filename or "file"
            dl_path = await download_telegram_file(
                context.bot, direct_file_id, download_dir, dl_filename
            )
            ok = dl_path is not None
            if not ok:
                err_tail = "direct Telegram download failed"
                logging.error("Direct download failed for file_id=%s", direct_file_id)
        else:
            ok = False
            try:
                register_pid = lambda pid: mgr.child_pids.setdefault(message.chat_id, []).append(pid)
                unregister_pid = lambda pid: mgr.child_pids.get(message.chat_id, []).remove(pid) if pid in mgr.child_pids.get(message.chat_id, []) else None
                ok, err_tail = await _run_tdl(cmd, env=env, on_progress=report_progress, register_pid=register_pid, unregister_pid=unregister_pid)
            except asyncio.CancelledError:
                clear_live(context, message.chat_id)
                try:
                    if batch_mode:
                        await _mark_batch_cancelled()
                    else:
                        await _safe_edit(status_msg, f"⛔️ Cancelled: {human_label}") or await _safe_send(f"⛔️ Cancelled: {human_label}")
                    await update_dashboard(context, message.chat_id, force=True)
                except Exception:
                    pass
                return
            except Exception as e:
                logging.error("Download execution failed for %s: %s", human_label, e)
                err_tail = str(e)
                ok = False

        after_files = _snapshot_files(path_clean)
        new_files = after_files - before_files
        did_postprocess = False

        if not ok:
            for rel in sorted(new_files):
                try:
                    os.remove(os.path.join(path_clean, rel))
                except (FileNotFoundError, Exception):
                    pass
            logging.error("Download failed; skipped post-processing for %s", path_clean)
            pending_same = await mgr.pending_for_content(message.chat_id, path_clean)
            if pending_same == 0:
                try:
                    logging.info(
                        "Post-processing remaining files after final failed item at %s lib_type=%s title=%s season=%s year=%s",
                        path_clean, lib_type_snapshot, title_snapshot, season_hint_snapshot, year_snapshot,
                    )
                    await asyncio.to_thread(
                        _process_directory, path_clean, title_snapshot, season_hint_snapshot, lib_type_snapshot, year_snapshot, replace_existing
                    )
                    did_postprocess = True
                except Exception as e:
                    logging.error("Post-process after failure failed: %s", e)
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
                        _process_directory, path_clean, title_snapshot, season_hint_snapshot, lib_type_snapshot, year_snapshot, replace_existing
                    )
                    did_postprocess = True
                except Exception as e:
                    logging.error("Post-process failed: %s", e)

        try:
            await asyncio.to_thread(_apply_permissions, path_clean, perm.puid, perm.pgid, perm.dir_mode, perm.file_mode)
        except Exception as e:
            logging.warning("Permission fix failed for %s: %s", path_clean, e)

        # Files that landed (or got renamed) in this run, with their final
        # Plex names — shown in the completion summary.
        renamed_files: list[str] = []
        if did_postprocess:
            final_snapshot = await asyncio.to_thread(_snapshot_files, path_clean)
            renamed_files = sorted(
                rel for rel in final_snapshot - before_files
                if os.path.splitext(rel)[1].lower() in VIDEO_EXT
            )

        clear_live(context, message.chat_id)

        def _file_listing(files: list[str], limit: int = 8) -> str:
            shown = [f"  • {Path(f).name}" for f in files[:limit]]
            if len(files) > limit:
                shown.append(f"  …+{len(files) - limit} more")
            return "\n".join(shown)

        if batch_mode:
            if ok:
                record_recent(context, message.chat_id, title_snapshot, active_lib, season_hint_snapshot, year_snapshot)
            batches = context.bot_data.get("download_batches", {})
            batch = batches.get(batch_id)
            if batch is not None:
                if ok:
                    batch["done"] = batch.get("done", 0) + 1
                else:
                    batch["failed"] = batch.get("failed", 0) + 1
                if renamed_files:
                    batch.setdefault("files", []).extend(renamed_files)
                completed = batch.get("done", 0) + batch.get("failed", 0)
                total = batch.get("total", batch_total or completed)
                label = batch.get("label", title_snapshot)
                if completed >= total:
                    files = batch.get("files") or []
                    listing = f"\n{_file_listing(files)}" if files else ""
                    if batch.get("failed", 0):
                        reason = f"\nLast error: {_friendly_error(err_tail)}" if err_tail else ""
                        text = (
                            f"⚠️ Batch finished: {label}\n"
                            f"Done: {batch.get('done', 0)} · Failed: {batch.get('failed', 0)}{reason}\n"
                            f"{path_clean}{listing}"
                        )
                    else:
                        text = f"✅ Batch complete: {label}\n{total} item(s) → {path_clean}{listing}"
                    await _safe_batch_edit(text)
                    batches.pop(batch_id, None)
                    if ok or batch.get("done", 0):
                        push_recent(context, message.chat_id, f"{label} ({batch.get('done', 0)} item(s))")
                else:
                    await _safe_batch_edit(
                        f"⬇️ Batch: {label}\n"
                        f"Completed {completed}/{total}. Next items remain queued."
                    )
            await update_dashboard(context, message.chat_id, force=True)
            return

        if ok:
            record_recent(context, message.chat_id, title_snapshot, active_lib, season_hint_snapshot, year_snapshot)
            listing = f"\n{_file_listing(renamed_files)}" if renamed_files else ""
            done_text = f"✅ Done: {human_label}\n{path_clean}{listing}"
            push_recent(
                context, message.chat_id,
                Path(renamed_files[0]).name if renamed_files else human_label,
            )
            try:
                if not await _safe_edit(status_msg, done_text):
                    await _safe_send(done_text)
            except Exception:
                await _safe_send(done_text)
        else:
            fail_text = f"❌ Download failed: {human_label}\n{_friendly_error(err_tail)}"
            try:
                if not await _safe_edit(status_msg, fail_text):
                    await _safe_send(fail_text)
            except Exception:
                await _safe_send(fail_text)
        await update_dashboard(context, message.chat_id, force=True)

    content_id = path_clean
    pos, _ = mgr.enqueue(
        message.chat_id,
        human_label,
        path_clean,
        _run,
        content_id=content_id,
        content_label=title,
        content_destination=path_clean,
        batch_id=batch_id,
    )
    if notify_queued and pos > 1:
        try:
            await message.reply_text(f"⏳ Added to queue (position {pos}).")
        except Exception:
            pass
    await ensure_dashboard(context, message.chat_id)
    await update_dashboard(context, message.chat_id, force=True)
    return pos, _


def _partition_duplicates(
    items: list[dict],
    download_dir: str,
    season_hint: Optional[int],
    lib_type: Optional[str],
) -> tuple[list[dict], list[dict], list[str]]:
    """Split items into (fresh, duplicates, duplicate_labels) by checking the
    destination folder for episodes/movies that already exist."""
    if not os.path.isdir(download_dir):
        return items, [], []
    existing_videos = [
        name for name in os.listdir(download_dir)
        if os.path.splitext(name)[1].lower() in VIDEO_EXT
    ]
    if not existing_videos:
        return items, [], []

    if lib_type in SERIES_TYPES or season_hint is not None:
        have: set[tuple[int, int]] = set()
        for name in existing_videos:
            s, e = parse_season_episode(name, season_hint)
            if s is not None and e is not None:
                have.add((s, e))
        fresh, dups, labels = [], [], []
        for item in items:
            fname = item.get("filename")
            s, e = parse_season_episode(fname, season_hint) if fname else (None, None)
            if s is not None and e is not None and (s, e) in have:
                dups.append(item)
                labels.append(f"S{s:02d}E{e:02d}")
            else:
                fresh.append(item)
        return fresh, dups, labels

    # Movie folder already holds a video → everything incoming is a duplicate.
    return [], list(items), existing_videos[:3]


async def queue_download_batch(
    message,
    context,
    items: list[dict],
    download_dir: str,
    title: str,
    season_hint: Optional[int],
    year: Optional[int] = None,
    replace_existing: bool = False,
):
    if not items:
        return

    if not replace_existing:
        active_lib = context.chat_data.get("active_library") or {}
        lib_type = active_lib.get("type") or context.chat_data.get("selected_type")
        items, dups, dup_labels = await asyncio.to_thread(
            _partition_duplicates, items, download_dir, season_hint, lib_type
        )
        if dups:
            context.chat_data["dup_pending"] = {
                "items": dups,
                "download_dir": download_dir,
                "title": title,
                "season": season_hint,
                "year": year,
                "active_library": active_lib or None,
                "selected_type": lib_type,
            }
            shown = ", ".join(dup_labels[:6])
            if len(dup_labels) > 6:
                shown += f" +{len(dup_labels) - 6} more"
            try:
                await message.reply_text(
                    f"⚠️ Already in the library: {shown}\n"
                    f"{len(dups)} item(s) skipped for now — replace them?",
                    reply_markup=InlineKeyboardMarkup([
                        [
                            InlineKeyboardButton("⏭ Skip", callback_data="dup|skip"),
                            InlineKeyboardButton("🔁 Replace", callback_data="dup|replace"),
                        ]
                    ]),
                )
            except Exception as e:
                logging.warning("Could not send duplicate prompt: %s", e)
        if not items:
            return

    if len(items) == 1:
        item = items[0]
        direct_kwargs = {}
        if item.get("direct_file_id"):
            direct_kwargs["direct_file_id"] = item["direct_file_id"]
            direct_kwargs["direct_filename"] = item.get("filename")
        await queue_download(
            message, context, item["link"], download_dir,
            title, season_hint, year,
            item.get("filename") or item["link"],
            use_group=item.get("is_text", False),
            replace_existing=replace_existing,
            **direct_kwargs,
        )
        return

    batch_id = _next_batch_id(context)
    label = title or "Content"
    initial = (
        f"⏳ Queued batch: {label}\n"
        f"{len(items)} item(s)\n"
        f"{download_dir}\n\n"
        "Use /queue to view or cancel."
    )
    status_msg = None
    try:
        status_msg = await message.reply_text(initial)
    except Exception as e:
        logging.warning("Could not send batch status message: %s", e)

    context.bot_data.setdefault("download_batches", {})[batch_id] = {
        "msg": status_msg,
        "label": label,
        "total": len(items),
        "done": 0,
        "failed": 0,
    }

    for idx, item in enumerate(items, start=1):
        direct_kwargs = {}
        if item.get("direct_file_id"):
            direct_kwargs["direct_file_id"] = item["direct_file_id"]
            direct_kwargs["direct_filename"] = item.get("filename")
        await queue_download(
            message, context, item["link"], download_dir,
            title, season_hint, year,
            item.get("filename") or item["link"],
            use_group=item.get("is_text", False),
            notify_queued=False,
            batch_id=batch_id,
            batch_index=idx,
            batch_total=len(items),
            replace_existing=replace_existing,
            **direct_kwargs,
        )


async def handle_dup_choice(update, context):
    """User decided what to do with duplicate items: skip or replace."""
    query = update.callback_query
    await safe_answer(query)
    choice = (query.data or "").split("|")[1] if "|" in (query.data or "") else ""
    pending = context.chat_data.pop("dup_pending", None)
    if not pending:
        await edit_message_safely(query.message, "Nothing pending.")
        return

    if choice == "skip":
        await edit_message_safely(
            query.message, f"⏭ Skipped {len(pending['items'])} duplicate(s)."
        )
        return

    if choice == "replace":
        # Restore library context: the flow may have cleared it (movies do).
        if pending.get("active_library"):
            context.chat_data["active_library"] = pending["active_library"]
        if pending.get("selected_type"):
            context.chat_data["selected_type"] = pending["selected_type"]
        await edit_message_safely(
            query.message,
            f"🔁 Re-downloading {len(pending['items'])} item(s) — existing files will be replaced.",
        )
        await queue_download_batch(
            query.message, context, pending["items"], pending["download_dir"],
            pending["title"], pending["season"], pending["year"],
            replace_existing=True,
        )
        lib_type = (pending.get("active_library") or {}).get("type") or pending.get("selected_type")
        if lib_type not in SERIES_TYPES:
            from app.state import clear_destination

            clear_destination(context)


async def set_destination(
    update,
    context,
    library: dict,
    title: str,
    year: Optional[int],
    season: Optional[int],
) -> str:
    root = library["root"]
    base_title = title_with_year(title, year)
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
    # Chat-scoped: in groups, any member's file must inherit this title —
    # user_data["pending_title"] only exists for the user who set it.
    context.chat_data["dest_title"] = base_title
    context.chat_data["dest_year"] = year
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
    base_title = title_with_year(base_title, year)
    folder_name = safe_title(base_title)
    for lib in libraries:
        if lib.type not in lib_types:
            continue
        candidate = os.path.join(lib.root, folder_name)
        if os.path.isdir(candidate):
            return {"name": lib.name, "root": lib.root, "type": lib.type}
    return None
