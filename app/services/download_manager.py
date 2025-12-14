import asyncio
import itertools
import os
from dataclasses import dataclass
from typing import Callable, Awaitable, Dict, List, Optional
import logging

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
    """
    Global FIFO with a single worker: processes one task at a time in arrival order.
    Per-chat cancellations remove queued items and stop the current task for that chat.
    """

    def __init__(self, max_concurrent: int = 1):
        # max_concurrent kept for compatibility; worker remains single to avoid overlap.
        self.max_concurrent = max_concurrent
        self.queue: List[TaskItem] = []
        self._id_gen = itertools.count(1)
        self.child_pids: Dict[int, List[int]] = {}
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
        logging.info(
            "Enqueued task %s (chat %s, content %s). Queue length: %s",
            task_id,
            chat_id,
            content_id,
            len(self.queue),
        )
        asyncio.create_task(self._ensure_worker())
        position = self.queue.index(item) + 1  # 1-based position among queued items
        return position, task_id

    async def cancel_running(self, chat_id: int) -> int:
        cancelled_running = 0
        restart_needed = False
        # Cancel worker if current is from chat_id
        if self._current and self._current.chat_id == chat_id and self._worker:
            self._worker.cancel()
            cancelled_running = 1
            await asyncio.gather(self._worker, return_exceptions=True)
            self._worker = None
            self._current = None
            restart_needed = True
        # Hard-kill child PIDs of this chat
        for pid in list(self.child_pids.get(chat_id, [])):
            try:
                os.kill(pid, 9)
            except Exception:
                pass
        self.child_pids[chat_id] = []
        if restart_needed and self.queue:
            asyncio.create_task(self._ensure_worker())
        return cancelled_running

    async def cancel_all(self, chat_id: int) -> tuple[int, int]:
        running_cancelled = await self.cancel_running(chat_id)
        queued_before = len(self.queue)
        self.queue = [item for item in self.queue if item.chat_id != chat_id]
        queued_removed = queued_before - len(self.queue)
        # Relaunch worker if tasks remain for other chats
        await self._ensure_worker()
        return running_cancelled, queued_removed

    async def cancel_task(self, chat_id: int, task_id: int) -> tuple[int, int]:
        """
        Cancel a content by referencing any of its task ids for a chat.
        Returns (running_cancelled, queued_cancelled) where queued_cancelled is the number of queued tasks removed.
        """
        target_content_id = None

        if self._current and self._current.chat_id == chat_id and self._current.id == task_id:
            target_content_id = self._current.content_id
        else:
            for item in self.queue:
                if item.chat_id == chat_id and item.id == task_id:
                    target_content_id = item.content_id
                    break

        if not target_content_id:
            return 0, 0

        cancelled_running = 0
        if self._current and self._current.chat_id == chat_id and self._current.content_id == target_content_id:
            cancelled_running = await self.cancel_running(chat_id)

        queued_before = len(self.queue)
        self.queue = [
            item for item in self.queue if not (item.chat_id == chat_id and item.content_id == target_content_id)
        ]
        queued_cancelled = queued_before - len(self.queue)
        if cancelled_running or queued_cancelled:
            await self._ensure_worker()
        return cancelled_running, queued_cancelled

    async def snapshot(self, chat_id: Optional[int] = None) -> tuple[Optional[TaskItem], list[TaskItem]]:
        """
        Return (running, queued) for the given chat_id (or all if None).
        Copies are shallow; do not mutate returned items.
        """
        async with self._lock:
            running = None
            queued: list[TaskItem] = []
            if self._current and (chat_id is None or self._current.chat_id == chat_id):
                running = self._current
            if chat_id is None:
                queued = list(self.queue)
            else:
                queued = [q for q in self.queue if q.chat_id == chat_id]
        return running, queued

    async def snapshot_by_content(self, chat_id: Optional[int] = None) -> tuple[Optional[ContentSummary], list[ContentSummary]]:
        """
        Return running content (if any) and queued contents grouped by content_id for the given chat.
        """
        running_task, queued_tasks = await self.snapshot(chat_id)
        groups: dict[str, ContentSummary] = {}
        order: dict[str, int] = {}

        def add_item(item: TaskItem, is_running: bool, position: int):
            summary = groups.get(item.content_id)
            if not summary:
                summary = ContentSummary(
                    content_id=item.content_id,
                    chat_id=item.chat_id,
                    label=item.content_label,
                    destination=item.content_destination,
                    total=0,
                    queued=0,
                    running=False,
                    representative_task_id=item.id,
                )
                groups[item.content_id] = summary
                order[item.content_id] = position

            summary.total += 1
            if not is_running:
                summary.queued += 1
            if is_running:
                summary.running = True
            if item.id < summary.representative_task_id:
                summary.representative_task_id = item.id
            if not summary.label:
                summary.label = item.content_label
            if not summary.destination:
                summary.destination = item.content_destination

        position = 0
        if running_task:
            add_item(running_task, True, position)
            position += 1
        for item in queued_tasks:
            add_item(item, False, position)
            position += 1

        running_summary: Optional[ContentSummary] = None
        queued_summaries: list[ContentSummary] = []
        for content_id, summary in sorted(groups.items(), key=lambda kv: order[kv[0]]):
            if summary.running:
                running_summary = summary
            else:
                queued_summaries.append(summary)

        return running_summary, queued_summaries
