import asyncio
import itertools
from dataclasses import dataclass
from typing import Callable, Awaitable, Dict, List

@dataclass
class TaskItem:
    id: int
    coro_factory: Callable[[], Awaitable[None]]

class DownloadManager:
    def __init__(self, max_concurrent: int = 3):
        self.max_concurrent = max_concurrent
        self.queues: Dict[int, List[TaskItem]] = {}
        self.running: Dict[int, int] = {}
        self._id_gen = itertools.count(1)

    def enqueue(self, chat_id: int, coro_factory: Callable[[], Awaitable[None]]) -> int:
        q = self.queues.setdefault(chat_id, [])
        task_id = next(self._id_gen)
        q.append(TaskItem(id=task_id, coro_factory=coro_factory))
        asyncio.create_task(self._maybe_start(chat_id))
        return len(q)

    async def _maybe_start(self, chat_id: int):
        running = self.running.get(chat_id, 0)
        queue = self.queues.get(chat_id) or []
        if running >= self.max_concurrent or not queue:
            return
        item = queue.pop(0)
        self.running[chat_id] = running + 1
        asyncio.create_task(self._run_item(chat_id, item))

    async def _run_item(self, chat_id: int, item: TaskItem):
        try:
            await item.coro_factory()
        finally:
            self.running[chat_id] = max(0, self.running.get(chat_id, 1) - 1)
            await self._maybe_start(chat_id)
