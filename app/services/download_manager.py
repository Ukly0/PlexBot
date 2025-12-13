import asyncio
import itertools
import os
from dataclasses import dataclass
from typing import Callable, Awaitable, Dict, List
import logging

@dataclass
class TaskItem:
    id: int
    chat_id: int
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

    def enqueue(self, chat_id: int, coro_factory: Callable[[], Awaitable[None]]) -> int:
        task_id = next(self._id_gen)
        item = TaskItem(id=task_id, chat_id=chat_id, coro_factory=coro_factory)
        self.queue.append(item)
        logging.info("Enqueued task %s (chat %s). Queue length: %s", task_id, chat_id, len(self.queue))
        asyncio.create_task(self._ensure_worker())
        # Position is 1-based in queue (ignores current running)
        return self.queue.index(item) + (1 if self._current else 1)

    async def cancel_running(self, chat_id: int) -> int:
        cancelled_running = 0
        # Cancel worker if current is from chat_id
        if self._current and self._current.chat_id == chat_id and self._worker:
            self._worker.cancel()
            cancelled_running = 1
            await asyncio.gather(self._worker, return_exceptions=True)
            self._worker = None
            self._current = None
        # Hard-kill child PIDs of this chat
        for pid in list(self.child_pids.get(chat_id, [])):
            try:
                os.kill(pid, 9)
            except Exception:
                pass
        self.child_pids[chat_id] = []
        return cancelled_running

    async def cancel_all(self, chat_id: int) -> tuple[int, int]:
        running_cancelled = await self.cancel_running(chat_id)
        queued_before = len(self.queue)
        self.queue = [item for item in self.queue if item.chat_id != chat_id]
        queued_removed = queued_before - len(self.queue)
        # Relanzar worker si quedan tareas de otros chats
        await self._ensure_worker()
        return running_cancelled, queued_removed
