"""tdl subprocess wrapper — async download with progress, retries, locking."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from typing import Callable, Awaitable, Optional

ProgressCb = Callable[[int, str], Awaitable[None]]

# Rate-limit progress updates to avoid Telegram 429 storms.
PROGRESS_MIN_STEP = 2
PROGRESS_MIN_INTERVAL = 5.0


async def kill_stale_tdl() -> None:
    try:
        proc = await asyncio.create_subprocess_exec(
            "pkill",
            "-u",
            str(os.getuid()),
            "-f",
            "tdl dl",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
    except Exception as e:
        logging.debug("kill_stale_tdl failed: %s", e)


async def run_download(
    cmd: str,
    *,
    env: Optional[dict[str, str]] = None,
    retries: int = 3,
    delay: int = 5,
    idle_timeout: int = 300,
    on_progress: Optional[ProgressCb] = None,
    register_pid: Optional[Callable[[int], None]] = None,
    unregister_pid: Optional[Callable[[int], None]] = None,
) -> bool:
    """Run tdl download command with retry and optional progress callback."""

    proc: Optional[asyncio.subprocess.Process] = None
    last_line = ""
    lines: list[str] = []
    max_tail = 50
    last_percent = -1
    last_emit = 0.0

    try:
        await kill_stale_tdl()

        for attempt in range(1, retries + 1):
            logging.info("Attempt %s of %s: %s", attempt, retries, cmd)
            proc = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=env or os.environ.copy(),
            )
            if register_pid and proc.pid:
                try:
                    register_pid(proc.pid)
                except Exception:
                    pass

            while True:
                try:
                    line = await asyncio.wait_for(
                        proc.stdout.readline(), timeout=idle_timeout
                    )
                except asyncio.TimeoutError:
                    logging.error("Download idle for %ss, terminating: %s", idle_timeout, cmd)
                    proc.kill()
                    try:
                        await proc.wait()
                    except Exception:
                        pass
                    break

                if not line:
                    break

                last_line = line.decode().strip()
                if last_line:
                    lines.append(last_line)
                    if len(lines) > max_tail:
                        lines.pop(0)

                if on_progress:
                    percents = re.findall(r"(\d{1,3})(?:\.\d+)?%", last_line)
                    if percents:
                        pct = int(float(percents[-1]))
                        if pct > 100:
                            pct = 100
                        if pct < last_percent and last_percent >= 0:
                            continue
                        now = time.time()
                        if (
                            pct != last_percent
                            and (pct - last_percent >= PROGRESS_MIN_STEP)
                        ) or (now - last_emit >= PROGRESS_MIN_INTERVAL):
                            last_percent = pct
                            last_emit = now
                            try:
                                await on_progress(min(100, pct), last_line)
                            except Exception as cb_err:
                                logging.debug("Progress callback failed: %s", cb_err)

            await proc.wait()
            if unregister_pid and proc.pid:
                try:
                    unregister_pid(proc.pid)
                except Exception:
                    pass

            if proc.returncode == 0:
                if on_progress and last_percent < 100:
                    try:
                        await on_progress(100, last_line)
                    except Exception:
                        pass
                logging.info("Download completed")
                return True

            logging.error("Download failed (attempt %s): %s", attempt, last_line)
            if lines:
                tail = " | ".join(lines[-8:])
                logging.error("TDL tail: %s", tail)

            if attempt < retries:
                await asyncio.sleep(delay)

    except asyncio.CancelledError:
        if proc and proc.returncode is None:
            proc.kill()
            try:
                await proc.wait()
            except Exception:
                pass
        logging.info("Download cancelled")
        raise

    return False
