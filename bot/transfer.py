import os
import asyncio
import logging
from pyrogram import Client
from pyrogram.errors import FloodWait, FloodPremiumWait
from pyrogram.errors.exceptions.bad_request_400 import (
    PhotoExtInvalid,
    PhotoInvalidDimensions,
    PhotoSaveFileInvalid,
    FileReferenceExpired,
    FileReferenceInvalid,
)


def truncate_caption(caption, max_length=1024):
    if not caption:
        return ""
    s = str(caption)
    return s if len(s) <= max_length else s[:max_length - 3] + "..."


async def download_media(client, message, progress=None, progress_args=()):
    """
    Download media from a Telegram message to the downloads/ folder.
    Returns the local file path on success, raises on unrecoverable error.
    """
    for attempt in range(3):
        try:
            return await client.download_media(
                message,
                file_name="downloads/",
                progress=progress,
                progress_args=progress_args,
            )
        except (FloodWait, FloodPremiumWait) as e:
            logging.warning(f"FloodWait {e.value}s on download (attempt {attempt + 1}/3)")
            await asyncio.sleep(e.value)
        except (FileReferenceExpired, FileReferenceInvalid):
            raise
        except Exception as e:
            if attempt == 2:
                raise
            logging.warning(f"Download attempt {attempt + 1} failed: {e}, retrying")
            await asyncio.sleep(2 ** attempt)
    return None


async def _send_by_ext(
    client: Client, chat_id, path: str, ext: str, kw: dict,
    thumb, file_name, duration, width, height,
):
    if ext in (".mp4", ".mkv", ".mov", ".avi", ".webm"):
        return await client.send_video(
            chat_id, path,
            thumb=thumb, duration=duration, width=width, height=height,
            supports_streaming=True, file_name=file_name, **kw
        )
    elif ext == ".gif":
        return await client.send_animation(chat_id, path, **kw)
    elif ext in (".mp3", ".m4a", ".flac"):
        return await client.send_audio(
            chat_id, path,
            thumb=thumb, duration=duration, file_name=file_name, **kw
        )
    elif ext in (".ogg", ".opus"):
        return await client.send_voice(chat_id, path, duration=duration, **kw)
    elif ext in (".jpg", ".jpeg", ".png", ".webp"):
        try:
            return await client.send_photo(chat_id, path, **kw)
        except (PhotoExtInvalid, PhotoInvalidDimensions, PhotoSaveFileInvalid):
            return await client.send_document(
                chat_id, path, file_name=file_name, **kw
            )
    else:
        return await client.send_document(
            chat_id, path, thumb=thumb, file_name=file_name, **kw
        )


async def upload_media(
    client: Client,
    chat_id,
    path: str,
    caption: str = "",
    thumb=None,
    file_name: str = None,
    duration: int = 0,
    width: int = 0,
    height: int = 0,
    progress=None,
    progress_args=(),
    fallback_client: Client = None,
    fallback_chat_id=None,
):
    """
    Upload a local file to Telegram and return the sent Message object.
    Selects the appropriate send method based on file extension.
    Retries up to 3 times on FloodWait or transient errors.
    """
    safe_cap = truncate_caption(caption)
    ext = os.path.splitext(path)[1].lower()
    kw = dict(caption=safe_cap, progress=progress, progress_args=progress_args)

    last_exc = None
    for attempt in range(3):
        try:
            return await _send_by_ext(
                client, chat_id, path, ext, kw,
                thumb, file_name, duration, width, height,
            )
        except (FloodWait, FloodPremiumWait) as e:
            last_exc = e
            logging.warning(f"FloodWait {e.value}s on upload (attempt {attempt + 1}/3)")
            await asyncio.sleep(e.value)
        except Exception as e:
            last_exc = e
            if attempt == 2:
                break
            await asyncio.sleep(2 ** attempt)

    if fallback_client is not None and fallback_client is not client:
        target_chat = fallback_chat_id if fallback_chat_id is not None else chat_id
        try:
            return await _send_by_ext(
                fallback_client, target_chat, path, ext, kw,
                thumb, file_name, duration, width, height,
            )
        except (FloodWait, FloodPremiumWait) as e:
            logging.warning(f"FloodWait {e.value}s on upload fallback")
            await asyncio.sleep(e.value)
            try:
                return await _send_by_ext(
                    fallback_client, target_chat, path, ext, kw,
                    thumb, file_name, duration, width, height,
                )
            except Exception as e2:
                last_exc = e2
        except Exception as e:
            last_exc = e

    if last_exc is not None:
        raise last_exc
    return None
