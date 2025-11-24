import asyncio
import logging
import os
from typing import Optional

from telegram import Update
from telegram.ext import ContextTypes

from config.settings import load_settings
from core.naming import safe_title
from core.ingest import process_directory
from bot.download_manager import DownloadManager


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
        proc = await asyncio.to_thread(
            asyncio.subprocess.create_subprocess_shell,
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
        await message.reply_text("Set a destination first with /buscar.")
        return

    tdl_template = load_settings().download.tdl_template
    cmd = tdl_template.format(url=link, dir=download_dir)
    extra_flags = context.chat_data.get("tdl_extra_flags")
    if extra_flags:
        cmd = f"{cmd} {extra_flags}"

    # Queue-based download to allow concurrent tasks per chat
    mgr: DownloadManager = context.bot_data.setdefault("dl_manager", DownloadManager(max_concurrent=3))
    title = context.user_data.get("manual_title") or context.user_data.get("pending_show", {}).get("label", "Content")
    season_hint = context.chat_data.get("season_hint")
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
