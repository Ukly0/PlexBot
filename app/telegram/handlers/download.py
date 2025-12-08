import asyncio
import json
import logging
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Optional, Callable, Awaitable

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from app.services.naming import safe_title, parse_season_episode, VIDEO_EXT
from app.services.ingest import process_directory
from app.services.download_manager import DownloadManager
from app.services.tmdb_client import tmdb_search, TMDbItem
from config.settings import load_settings
from app.telegram.state import set_state, STATE_SEARCH

SERIES_TYPES = {"series", "anime", "docuseries"}

# Desired ownership/permissions for downloaded content.
TARGET_UID = 1000
TARGET_GID = 1000
DIR_MODE = 0o755
FILE_MODE = 0o644

# Global lock to serialize TDL invocations. Re-bound per event loop to avoid
# "Event loop is closed" when the app restarts.
TDL_LOCK: Optional[asyncio.Lock] = None
_TDL_LOCK_LOOP: Optional[asyncio.AbstractEventLoop] = None


def get_tdl_lock() -> asyncio.Lock:
    global TDL_LOCK, _TDL_LOCK_LOOP
    loop = asyncio.get_running_loop()
    if TDL_LOCK is None or _TDL_LOCK_LOOP is None or _TDL_LOCK_LOOP != loop:
        TDL_LOCK = asyncio.Lock()
        _TDL_LOCK_LOOP = loop
    return TDL_LOCK


async def kill_stale_tdl() -> None:
    """
    Best-effort kill of stray tdl dl processes for this user to avoid TDLib DB locks.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "pkill", "-u", str(os.getuid()), "-f", "tdl dl", stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
        )
        await proc.wait()
    except Exception as e:
        logging.debug("kill_stale_tdl failed: %s", e)


def pick_library_root(libs, lib_type: str) -> Optional[str]:
    for lib in libs:
        if lib.type == lib_type:
            return lib.root
    return None


def reset_destination(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Clear selected destination so the next link forces a new choice."""
    context.chat_data.pop("download_dir", None)
    context.chat_data.pop("season_hint", None)
    context.chat_data.pop("active_selection", None)
    context.chat_data.pop("selected_type", None)
    context.user_data.pop("manual_title", None)
    context.user_data.pop("pending_show", None)
    context.user_data.pop("pending_manual_type", None)


def get_message_link(message) -> str:
    chat = message.chat
    if chat.username:
        return f"https://t.me/{chat.username}/{message.message_id}"
    chat_id_str = str(chat.id)
    base = chat_id_str[4:] if chat_id_str.startswith("-100") else chat_id_str.lstrip("-")
    return f"https://t.me/c/{base}/{message.message_id}"


def should_reset_after_enqueue(context: ContextTypes.DEFAULT_TYPE) -> bool:
    selection = context.chat_data.get("active_selection") or {}
    lib_type = selection.get("lib_type") or context.chat_data.get("selected_type")
    return not lib_type or lib_type not in SERIES_TYPES


ProgressCb = Callable[[int, str], Awaitable[None]]


async def run_download(
    cmd: str,
    *,
    env: Optional[dict[str, str]] = None,
    retries: int = 3,
    delay: int = 5,
    on_progress: Optional[ProgressCb] = None,
    register_pid: Optional[Callable[[int], None]] = None,
    unregister_pid: Optional[Callable[[int], None]] = None,
) -> bool:
    """
    Run TDL download command with retry and optional progress callback.
    Progress is best-effort, parsing percentage from stdout lines.
    """
    proc: Optional[asyncio.subprocess.Process] = None
    last_line = ""
    lines: list[str] = []
    last_percent = -1
    last_emit = 0.0
    try:
        lock = get_tdl_lock()
        async with lock:
            # Before starting a new TDL process, kill any stale ones to avoid TDLib DB locks.
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
                    line = await proc.stdout.readline()
                    if not line:
                        break
                    last_line = line.decode().strip()
                    if last_line:
                        lines.append(last_line)
                    print("\r" + last_line, end="", flush=True)
                    if on_progress:
                        percents = re.findall(r"(\d{1,3})(?:\.\d+)?%", last_line)
                        if percents:
                            pct_float = float(percents[-1])  # take the last percent on the line
                            pct = int(pct_float)
                            if pct > 100:
                                pct = 100
                            if pct < last_percent and last_percent >= 0:
                                # Ignore regressions (multiple internal bars), keep monotonic.
                                continue
                            now = time.time()
                            if pct != last_percent and (pct - last_percent >= 2 or now - last_emit >= 1.0):
                                last_percent = pct
                                last_emit = now
                                try:
                                    await on_progress(min(100, pct), last_line)
                                except Exception as cb_err:
                                    logging.debug("Progress callback failed: %s", cb_err)
                await proc.wait()
                print("")
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
                    tail = " | ".join(lines[-5:])
                    logging.error("TDL output tail: %s", tail)
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


def ensure_dir(path: str) -> None:
    try:
        os.makedirs(path, exist_ok=True)
    except Exception as e:
        logging.error("Could not create %s: %s", path, e)


def set_season_for_selection(context: ContextTypes.DEFAULT_TYPE, season_num: int) -> Optional[str]:
    """Update active series/docuseries selection to a new season and return destination path."""
    selection = context.chat_data.get("active_selection")
    if not selection or selection.get("lib_type") not in SERIES_TYPES:
        return None
    base_dir = selection.get("base_dir")
    if not base_dir:
        return None
    selection["season"] = season_num
    download_dir = os.path.join(base_dir, f"Season {season_num:02d}")
    ensure_dir(download_dir)
    context.chat_data["download_dir"] = download_dir
    context.chat_data["season_hint"] = season_num
    context.chat_data["selected_type"] = selection.get("lib_type")
    context.user_data["manual_title"] = selection.get("title") or context.user_data.get("manual_title")
    selection["download_dir"] = download_dir
    return download_dir


async def finalize_selection(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    lib_type: str,
    season: Optional[int],
    label: Optional[str],
    original_title_en: Optional[str] = None,
    year: Optional[int] = None,
):
    st = load_settings()
    root = pick_library_root(st.libraries, lib_type)
    if not root:
        await update.effective_message.reply_text(f"No library configured for {lib_type}.")
        return

    base_title = original_title_en or label or "Content"
    if year:
        base_title = f"{base_title} ({year})"
    folder_name = safe_title(base_title)
    base_dir = os.path.join(root, folder_name)
    selection = {
        "lib_type": lib_type,
        "base_dir": base_dir,
        "title": base_title,
        "year": year,
    }
    download_dir = base_dir
    if season is not None:
        download_dir = os.path.join(download_dir, f"Season {season:02d}")
        selection["season"] = season
        context.chat_data["season_hint"] = season
    else:
        context.chat_data.pop("season_hint", None)

    ensure_dir(download_dir)
    selection["download_dir"] = download_dir
    context.chat_data["download_dir"] = download_dir
    context.chat_data["active_selection"] = selection
    context.chat_data["selected_type"] = lib_type
    context.user_data["manual_title"] = base_title
    context.user_data.pop("awaiting", None)

    msg = f"Destination set: {download_dir}\nSend a Telegram link or attach a file to download."
    await update.effective_message.reply_text(msg)

    pending_link = context.chat_data.pop("pending_link", None)
    if pending_link:
        title = context.user_data.get("manual_title") or original_title_en or label or "Content"
        season_hint = context.chat_data.get("season_hint")
        await queue_download_task(update.effective_message, context, pending_link, download_dir, title, season_hint)
        if should_reset_after_enqueue(context):
            reset_destination(context)


async def queue_download_task(
    message,
    context: ContextTypes.DEFAULT_TYPE,
    link: str,
    download_dir: str,
    title: str,
    season_hint: Optional[int],
):
    tdl_template = load_settings().download.tdl_template
    cmd = tdl_template.format(url=link, dir=download_dir)
    tdl_home = load_settings().download.tdl_home
    env = os.environ.copy()
    if tdl_home:
        env["TDL_HOME"] = tdl_home
        try:
            os.makedirs(tdl_home, exist_ok=True)
        except Exception as e:
            logging.warning("Could not create TDL_HOME %s: %s", tdl_home, e)
    extra_flags = context.chat_data.get("tdl_extra_flags")
    if extra_flags and "--group" not in cmd:
        cmd = f"{cmd} {extra_flags}"

    mgr: DownloadManager = context.bot_data.setdefault("dl_manager", DownloadManager(max_concurrent=3))
    path_clean = download_dir

    def _apply_permissions(path: str) -> None:
        """
        Set ownership and permissions recursively.
        Best-effort: logs and continues on error.
        """
        try:
            # Ensure the top-level folder exists with correct perms.
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

    async def _run():
        status_msg = await message.reply_text(f"▶️ Starting download: {title}")

        async def report_progress(pct: int, line: str):
            try:
                bar_len = 20
                filled = int(bar_len * pct / 100)
                bar = "█" * filled + "░" * (bar_len - filled)
                await status_msg.edit_text(f"⬇️ {title}\n[{bar}] {pct}%")
            except Exception as e:
                logging.debug("Could not update progress message: %s", e)

        try:
            register_pid = lambda pid: mgr.child_pids.setdefault(message.chat_id, []).append(pid)
            unregister_pid = lambda pid: mgr.child_pids.get(message.chat_id, []).remove(pid) if pid in mgr.child_pids.get(message.chat_id, []) else None
            ok = await run_download(cmd, env=env, on_progress=report_progress, register_pid=register_pid, unregister_pid=unregister_pid)
        except asyncio.CancelledError:
            try:
                await status_msg.edit_text(f"⛔️ Cancelled: {title}")
            except Exception:
                pass
            return
        if ok:
            try:
                lib_type = context.chat_data.get("selected_type")
                process_directory(path_clean, title, season_hint, lib_type)
            except Exception as e:
                logging.error("Post-process failed: %s", e)
            try:
                _apply_permissions(path_clean)
            except Exception as e:
                logging.warning("Permission fix failed for %s: %s", path_clean, e)
            try:
                await status_msg.edit_text(f"✅ Done: {path_clean}")
            except Exception:
                await message.reply_text(f"✅ Done: {path_clean}")
        else:
            try:
                await status_msg.edit_text("❌ Download failed. Check the link and try again.")
            except Exception:
                await message.reply_text("❌ Download failed. Check the link and try again.")

    position = mgr.enqueue(message.chat_id, _run)
    if position == 1 and mgr.running.get(message.chat_id, 0) < mgr.max_concurrent:
        await message.reply_text("⏳ Download queued and starting...")
    else:
        await message.reply_text(f"⏳ Queued at position #{position} for download: {title}")


def _guess_title_from_filename(fname: str) -> str:
    stem = os.path.splitext(fname)[0]
    cleaned = stem.replace(".", " ").replace("_", " ").replace("-", " ")
    tokens = cleaned.split()
    skip = {"1080p", "720p", "480p", "bluray", "webdl", "webrip", "hdrip", "x264", "x265", "h264", "h265"}
    filtered = [t for t in tokens if t.lower() not in skip]
    title = " ".join(filtered) or cleaned
    return title.strip()


def _build_results_keyboard(results: list[TMDbItem], page: int = 0) -> InlineKeyboardMarkup:
    PAGE_SIZE = 5
    start = page * PAGE_SIZE
    slice_items = results[start : start + PAGE_SIZE]
    buttons: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for item in slice_items:
        label = f"{item.title} ({item.year})" if item.year else item.title
        row.append(InlineKeyboardButton(label[:64], callback_data=f"tmdb|{item.type}|{item.id}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    pagination = []
    total_pages = (len(results) - 1) // PAGE_SIZE if results else 0
    if page > 0:
        pagination.append(InlineKeyboardButton("⬅️", callback_data=f"page|{page-1}"))
    if page < total_pages:
        pagination.append(InlineKeyboardButton("➡️", callback_data=f"page|{page+1}"))
    if pagination:
        buttons.append(pagination)
    buttons.append([InlineKeyboardButton("✍️ Manual entry", callback_data="manual|start")])
    buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel|flow")])
    return InlineKeyboardMarkup(buttons)


async def prompt_tmdb_from_filename(message, context, fname: str):
    guess = _guess_title_from_filename(fname)
    results = tmdb_search(guess)
    context.user_data["results_list"] = results
    context.user_data["results_map"] = {f"{r.type}:{r.id}": r for r in results}
    context.user_data["results_page"] = 0
    if results:
        await message.reply_text(
            f"Detected file: {fname}\nSelect the title or type another query.",
            reply_markup=_build_results_keyboard(results, 0),
        )
    else:
        await message.reply_text(
            f"Detected file: {fname}\nNo TMDb results for '{guess}'. Use /search or manual entry.",
        )


async def handle_download_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message:
        return

    link = None
    if message.text and "https://t.me" in message.text:
        link = message.text.strip()
    elif any([message.document, message.video, message.audio, message.photo]):
        link = get_message_link(message)

    if not link:
        return

    download_dir = context.chat_data.get("download_dir")

    # No destination set: prompt TMDb first, then category selection will set destination
    if not download_dir:
        file_name = None
        if message.document:
            file_name = message.document.file_name
        elif message.video:
            file_name = message.video.file_name

        context.chat_data["pending_link"] = link
        if file_name:
            context.chat_data["pending_filename"] = file_name
            await prompt_tmdb_from_filename(message, context, file_name)
            return

        # If no filename to infer, ask user to search/select title explicitly.
        set_state(context.user_data, STATE_SEARCH)
        await message.reply_text("Type a title to search (TMDb) to set destination.")
        return

    title = context.user_data.get("manual_title") or context.user_data.get("pending_show", {}).get("label", "Content")
    season_hint = context.chat_data.get("season_hint")
    await queue_download_task(message, context, link, download_dir, title, season_hint)
    if should_reset_after_enqueue(context):
        reset_destination(context)


async def resolve_filename_from_link(link: str) -> Optional[str]:
    """
    Use tdl chat export to fetch filename metadata from a Telegram link.
    Supports:
    - https://t.me/c/CHATID/MSGID[/THREADID]
    - https://t.me/USERNAME/MSGID
    """
    import re

    match_private = re.search(r"t\.me/c/(\d+)/(\d+)(?:/(\d+))?", link)
    match_public = re.search(r"t\.me/([^/]+)/(\d+)", link)

    chat_id = None
    msg_id = None
    topic_id = None

    if match_private:
        base_id = match_private.group(1)
        chat_id = f"-100{base_id}"
        if match_private.group(3):
            msg_id = match_private.group(3)
            topic_id = match_private.group(2)
        else:
            msg_id = match_private.group(2)
    elif match_public:
        chat_id = match_public.group(1)
        if chat_id == "c":
            return None
        msg_id = match_public.group(2)

    if not chat_id or not msg_id:
        return None

    try:
        variants = []
        base = [
            "tdl",
            "chat",
            "export",
            "-c",
            chat_id,
            "-i",
            msg_id,
            "-T",
            "id",
        ]
        if topic_id:
            base += ["--topic", topic_id]

        variants.append(base + ["-f", "json", "-o", "-", "--with-content"])
        variants.append(base + ["-f", "json", "--with-content"])
        variants.append(base + ["-f", "json"])
        if topic_id:
            base_no_topic = [p for p in base if p not in ("--topic", str(topic_id))]
            variants.append(base_no_topic + ["-f", "json", "-o", "-", "--with-content"])
            variants.append(base_no_topic + ["-f", "json", "--with-content"])
            variants.append(base_no_topic + ["-f", "json"])
        if chat_id.startswith("-100") and match_private:
            base_id_only = match_private.group(1)
            base_plain = [
                "tdl",
                "chat",
                "export",
                "-c",
                base_id_only,
                "-i",
                msg_id,
                "-T",
                "id",
            ]
            variants.append(base_plain + ["-f", "json", "-o", "-", "--with-content"])
            variants.append(base_plain + ["-f", "json", "--with-content"])
            variants.append(base_plain + ["-f", "json"])

        data = None
        for idx, cmd in enumerate(variants):
            tmp_path = None
            cmd_exec = cmd
            try:
                if "-o" not in cmd:
                    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".json")
                    tmp_path = tmp.name
                    tmp.close()
                    cmd_exec = cmd + ["-o", tmp_path]

                logging.info("Resolving filename via tdl (variant %s): %s", idx + 1, " ".join(cmd_exec))
                proc = await asyncio.create_subprocess_exec(
                    *cmd_exec, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
                )
                stdout, stderr = await proc.communicate()

                if proc.returncode != 0:
                    logging.error("tdl export failed (variant %s): %s", idx + 1, stderr.decode().strip())
                    if tmp_path:
                        try:
                            os.remove(tmp_path)
                        except Exception:
                            pass
                    continue

                raw = stdout.decode() if stdout else ""
                if tmp_path:
                    try:
                        with open(tmp_path, "r", encoding="utf-8") as f:
                            raw = f.read()
                    finally:
                        try:
                            os.remove(tmp_path)
                        except Exception:
                            pass

                try:
                    data = json.loads(raw)
                    break
                except json.JSONDecodeError:
                    logging.error("tdl export returned non-JSON output (variant %s): %s", idx + 1, raw[:200])
                    continue
            except Exception as inner:
                logging.error("tdl export exception (variant %s): %s", idx + 1, inner)
                continue

        if not data:
            return None

        msgs = data.get("messages", []) if isinstance(data, dict) else data
        if not msgs:
            return None

        msg = msgs[0]
        media = msg.get("media", {})
        if not media:
            return None

        doc = media.get("document", {})
        if doc:
            direct_name = doc.get("file_name")
            if direct_name:
                return direct_name
            for attr in doc.get("attributes", []):
                if attr.get("_") == "DocumentAttributeFilename":
                    return attr.get("file_name")

        video = media.get("video", {})
        if video:
            name = video.get("file_name") or video.get("file", {}).get("name")
            if name:
                return name

        def find_key(obj, key):
            if isinstance(obj, dict):
                if key in obj:
                    return obj[key]
                for k, v in obj.items():
                    res = find_key(v, key)
                    if res:
                        return res
            elif isinstance(obj, list):
                for item in obj:
                    res = find_key(item, key)
                    if res:
                        return res
            return None

        return find_key(msg, "file_name")

    except Exception as e:
        logging.error(f"Error resolving filename: {e}")
        return None
