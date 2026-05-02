"""Small Telegram API wrappers for noisy/fragile message operations."""

from __future__ import annotations

import asyncio
import logging

from telegram.error import BadRequest, RetryAfter, TimedOut


async def safe_answer(query, *, max_retries: int = 2) -> bool:
    for attempt in range(max_retries):
        try:
            await query.answer()
            return True
        except RetryAfter as e:
            wait = getattr(e, "retry_after", 5) or 5
            logging.warning("Flood control while answering callback; retrying in %ss", wait)
            await asyncio.sleep(wait)
        except TimedOut:
            logging.warning("Timed out answering callback (attempt %s/%s)", attempt + 1, max_retries)
            await asyncio.sleep(1)
        except BadRequest as e:
            logging.warning("Could not answer callback query: %s", e)
            return False
        except Exception as e:
            logging.warning("Could not answer callback query: %s", e)
            return False
    return False


async def reply_text_safely(message, text: str, reply_markup=None, *, max_retries: int = 3):
    for attempt in range(max_retries):
        try:
            return await message.reply_text(text, reply_markup=reply_markup)
        except RetryAfter as e:
            wait = getattr(e, "retry_after", 30) or 30
            logging.warning("Flood control: retrying reply in %ss (attempt %s/%s)", wait, attempt + 1, max_retries)
            await asyncio.sleep(wait)
        except TimedOut:
            logging.warning("Timed out sending reply (attempt %s/%s)", attempt + 1, max_retries)
            await asyncio.sleep(2)
        except BadRequest as e:
            if "not modified" in str(e).lower():
                return None
            logging.warning("Reply failed: %s", e)
            return None
        except Exception as e:
            logging.warning("Reply failed: %s", e)
            return None
    logging.error("Reply failed after %s retries", max_retries)
    return None


async def reply_photo_safely(message, photo, caption: str, reply_markup=None, *, max_retries: int = 3):
    for attempt in range(max_retries):
        try:
            return await message.reply_photo(photo=photo, caption=caption, reply_markup=reply_markup)
        except RetryAfter as e:
            wait = getattr(e, "retry_after", 30) or 30
            logging.warning("Flood control: retrying photo in %ss (attempt %s/%s)", wait, attempt + 1, max_retries)
            await asyncio.sleep(wait)
        except TimedOut:
            logging.warning("Timed out sending photo (attempt %s/%s)", attempt + 1, max_retries)
            await asyncio.sleep(2)
        except Exception as e:
            logging.warning("Photo reply failed: %s", e)
            return None
    logging.error("Photo reply failed after %s retries", max_retries)
    return None


async def edit_message_safely(message, text: str, reply_markup=None) -> bool:
    try:
        if message.photo:
            await message.edit_caption(caption=text, reply_markup=reply_markup)
        else:
            await message.edit_text(text, reply_markup=reply_markup)
        return True
    except BadRequest as e:
        err = str(e).lower()
        if "not modified" in err:
            return True
        logging.warning("Edit failed; sending a new message instead: %s", e)
    except Exception as e:
        logging.warning("Edit failed; sending a new message instead: %s", e)

    fallback = await reply_text_safely(message, text, reply_markup=reply_markup)
    return fallback is not None


async def delete_safely(message) -> bool:
    try:
        await message.delete()
        return True
    except BadRequest as e:
        logging.warning("Delete failed; keeping message: %s", e)
        return False
    except Exception as e:
        logging.warning("Delete failed; keeping message: %s", e)
        return False
