"""Direct Telegram file download for private chats.

tdl cannot resolve message links from private chats (it requires public
groups or channels with a public invite link). When the bot receives a
forwarded file in a private chat, we download the file directly via the
Telegram Bot API and place it in the target directory, then run the same
post-processing pipeline (extract, rename, set permissions).

The Bot API has a 20 MB file size limit for downloads. For larger files,
tdl must be used from a public group context.
"""

import logging
import os
import re
from typing import Optional

from app.services.namer import safe_title

TELEGRAM_MAX_FILE_SIZE = 20 * 1024 * 1024  # 20 MB


def _safe_download_filename(filename: Optional[str]) -> str:
    raw = os.path.basename(filename or "file")
    stem, ext = os.path.splitext(raw)
    clean_stem = safe_title(stem or "file")
    clean_ext = ext.lower() if re.fullmatch(r"\.[A-Za-z0-9]{1,10}", ext or "") else ""
    return f"{clean_stem}{clean_ext}"


def _get_file_info(message):
    """Extract file object and filename from a Telegram message.

    Returns (file_id, filename, file_size) or None if no downloadable file.
    """
    if message.document:
        return message.document.file_id, message.document.file_name, message.document.file_size
    if message.video:
        return message.video.file_id, message.video.file_name, message.video.file_size
    if message.audio:
        return message.audio.file_id, message.audio.file_name, message.audio.file_size
    if message.photo:
        best = message.photo[-1]
        return best.file_id, f"photo_{best.file_unique_id}.jpg", best.file_size
    return None


def _is_private_chat(message) -> bool:
    """Check if the message comes from a private (1-on-1) chat."""
    chat = message.chat
    return chat and chat.type == "private"


def _is_too_large(message) -> bool:
    """Check if the file exceeds the Bot API download limit."""
    for attr in (message.document, message.video, message.audio):
        if attr and attr.file_size and attr.file_size > TELEGRAM_MAX_FILE_SIZE:
            return True
    if message.photo:
        best = message.photo[-1]
        if best.file_size and best.file_size > TELEGRAM_MAX_FILE_SIZE:
            return True
    return False


async def download_telegram_file(
    bot,
    file_id: str,
    dest_dir: str,
    filename: str,
) -> Optional[str]:
    """Download a file directly from Telegram Bot API.

    Args:
        bot: The telegram Bot instance (context.bot).
        file_id: The Telegram file_id to download.
        dest_dir: Directory to save the file in.
        filename: Target filename.

    Returns:
        The full path to the downloaded file, or None on failure.
    """
    try:
        os.makedirs(dest_dir, exist_ok=True)
        tg_file = await bot.get_file(file_id)
        safe_filename = _safe_download_filename(filename)
        dest_path = os.path.join(dest_dir, safe_filename)

        base, ext = os.path.splitext(safe_filename)
        counter = 1
        while os.path.exists(dest_path):
            dest_path = os.path.join(dest_dir, f"{base}-dup{counter}{ext}")
            counter += 1

        await tg_file.download_to_drive(dest_path)
        logging.info("Direct download complete: %s", dest_path)
        return dest_path
    except Exception as e:
        logging.error("Direct Telegram download failed for %s: %s", filename, e)
        return None
