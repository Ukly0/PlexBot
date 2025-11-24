import asyncio
import logging
import os
from typing import Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from config.settings import load_settings
from core.naming import safe_title
from core.ingest import process_directory
from bot.download_manager import DownloadManager
from core.tmdb import tmdb_search, TMDbItem


def pick_library_root(libs, lib_type: str) -> Optional[str]:
    for lib in libs:
        if lib.type == lib_type:
            return lib.root
    return None


def get_message_link(message) -> str:
    chat = message.chat
    if chat.username:
        return f"https://t.me/{chat.username}/{message.message_id}"
    chat_id_str = str(chat.id)
    base = chat_id_str[4:] if chat_id_str.startswith("-100") else chat_id_str.lstrip("-")
    return f"https://t.me/c/{base}/{message.message_id}"


async def run_download(cmd: str, retries: int = 3, delay: int = 5) -> bool:
    for attempt in range(1, retries + 1):
        logging.info("Attempt %s of %s: %s", attempt, retries, cmd)
        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        last_line = ""
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            last_line = line.decode().strip()
            print("\r" + last_line, end="", flush=True)
        await proc.wait()
        print("")
        if proc.returncode == 0:
            logging.info("Download completed")
            return True
        logging.error("Download failed (attempt %s): %s", attempt, last_line)
        if attempt < retries:
            await asyncio.sleep(delay)
    return False


def ensure_dir(path: str) -> None:
    try:
        os.makedirs(path, exist_ok=True)
    except Exception as e:
        logging.error("No pude crear %s: %s", path, e)


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
        await update.effective_message.reply_text(f"No hay biblioteca configurada para {lib_type}.")
        return

    base_title = original_title_en or label or "Contenido"
    if year:
        base_title = f"{base_title} ({year})"
    folder_name = safe_title(base_title)
    download_dir = os.path.join(root, folder_name)
    if season is not None:
        download_dir = os.path.join(download_dir, f"Season {season:02d}")
        context.chat_data["tdl_extra_flags"] = "--group"
        context.chat_data["season_hint"] = season
    else:
        context.chat_data.pop("tdl_extra_flags", None)
        context.chat_data.pop("season_hint", None)

    ensure_dir(download_dir)
    context.chat_data["download_dir"] = f'"{download_dir}"'
    context.user_data["manual_title"] = folder_name
    # Limpia estados de espera que no tengan sentido tras fijar destino
    context.user_data.pop("awaiting", None)

    msg = f"Destino fijado: {download_dir}\nEnvía enlace o archivo para descargar."
    await update.effective_message.reply_text(msg)

    # If there is a pending link (user sent file/link before selecting), enqueue it now
    pending_link = context.chat_data.pop("pending_link", None)
    if pending_link:
        title = context.user_data.get("manual_title") or original_title_en or label or "Content"
        season_hint = context.chat_data.get("season_hint")
        await queue_download_task(update.effective_message, context, pending_link, download_dir, title, season_hint)


async def queue_download_task(message, context: ContextTypes.DEFAULT_TYPE, link: str, download_dir: str, title: str, season_hint: Optional[int]):
    tdl_template = load_settings().download.tdl_template
    cmd = tdl_template.format(url=link, dir=download_dir)
    extra_flags = context.chat_data.get("tdl_extra_flags")
    if extra_flags:
        cmd = f"{cmd} {extra_flags}"

    mgr: DownloadManager = context.bot_data.setdefault("dl_manager", DownloadManager(max_concurrent=3))
    path_clean = download_dir.strip('"')

    async def _run():
        await message.reply_text(f"▶️ Starting download: {title}")
        ok = await run_download(cmd)
        if ok:
            try:
                process_directory(path_clean, title, season_hint)
            except Exception as e:
                logging.error("Post-process failed: %s", e)
            await message.reply_text(f"✅ Done: {path_clean}")
        else:
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
            f"Detected file: {fname}\nNo TMDb results for '{guess}'. Use /buscar or manual entry.",
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
    if not download_dir:
        file_name = None
        if message.document:
            file_name = message.document.file_name
        elif message.video:
            file_name = message.video.file_name
        context.chat_data["pending_link"] = link
        if file_name:
            context.chat_data["pending_filename"] = file_name
            await auto_match_and_prompt_category(message, context, file_name)
            return
        if message.text:
            await auto_match_and_prompt_category(message, context, message.text)
            return
        await message.reply_text("Set a destination first with /buscar.")
        return

    title = context.user_data.get("manual_title") or context.user_data.get("pending_show", {}).get("label", "Content")
    season_hint = context.chat_data.get("season_hint")
    await queue_download_task(message, context, link, download_dir, title, season_hint)
