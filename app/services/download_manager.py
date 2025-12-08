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
        self.running_tasks: Dict[int, List[asyncio.Task]] = {}
        self._id_gen = itertools.count(1)
        # Track spawned subprocess PIDs per chat for hard-kill on cancel.
        self.child_pids: Dict[int, List[int]] = {}

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
        task = asyncio.create_task(self._run_item(chat_id, item))
        self.running_tasks.setdefault(chat_id, []).append(task)

    async def _run_item(self, chat_id: int, item: TaskItem):
        try:
            await item.coro_factory()
        except asyncio.CancelledError:
            # Propagate cancellation so awaiting callers know it was cancelled.
            raise
        finally:
            self.running[chat_id] = max(0, self.running.get(chat_id, 1) - 1)
            tasks = self.running_tasks.get(chat_id)
            if tasks:
                try:
                    tasks.remove(asyncio.current_task())  # type: ignore[arg-type]
                except ValueError:
                    pass
            await self._maybe_start(chat_id)

    async def cancel_running(self, chat_id: int) -> int:
        tasks = list(self.running_tasks.get(chat_id, []))
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        # Hard-kill any child PIDs tracked for this chat.
        for pid in list(self.child_pids.get(chat_id, [])):
            try:
                os.kill(pid, 9)
            except Exception:
                pass
        self.child_pids[chat_id] = []
        return len(tasks)

    async def cancel_all(self, chat_id: int) -> tuple[int, int]:
        running_cancelled = await self.cancel_running(chat_id)
        queued = len(self.queues.get(chat_id, []))
        self.queues[chat_id] = []
        return running_cancelled, queued
