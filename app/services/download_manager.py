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
    coro_factory: Callable[[], Awaitable[None]]


class DownloadManager:
    """
    Global FIFO with un solo worker: procesa uno por uno en orden de llegada.
    Cancelaciones por chat eliminan pendientes y detienen la tarea actual de ese chat.
    """

    def __init__(self, max_concurrent: int = 1):
        # max_concurrent se mantiene para compat, pero el worker es único para evitar solapes.
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

    def enqueue(self, chat_id: int, label: str, destination: str, coro_factory: Callable[[], Awaitable[None]]) -> tuple[int, int]:
        task_id = next(self._id_gen)
        item = TaskItem(id=task_id, chat_id=chat_id, label=label, destination=destination, coro_factory=coro_factory)
        self.queue.append(item)
        logging.info("Enqueued task %s (chat %s). Queue length: %s", task_id, chat_id, len(self.queue))
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
        # Relanzar worker si quedan tareas de otros chats
        await self._ensure_worker()
        return running_cancelled, queued_removed

    async def cancel_task(self, chat_id: int, task_id: int) -> tuple[int, int]:
        """
        Cancel a specific task by id for a chat.
        Returns (running_cancelled, queued_cancelled).
        """
        if self._current and self._current.chat_id == chat_id and self._current.id == task_id:
            cancelled_running = await self.cancel_running(chat_id)
            await self._ensure_worker()
            return cancelled_running, 0

        queued_before = len(self.queue)
        self.queue = [item for item in self.queue if not (item.chat_id == chat_id and item.id == task_id)]
        queued_cancelled = 1 if len(self.queue) < queued_before else 0
        if queued_cancelled:
            await self._ensure_worker()
        return 0, queued_cancelled

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
