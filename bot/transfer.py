import os
import asyncio
import logging
from pyrogram import Client
from pyrogram.types import Message
from pyrogram.errors import FloodWait, FloodPremiumWait, AuthKeyUnregistered, SessionRevoked
from pyrogram.errors.exceptions.bad_request_400 import (
    PhotoExtInvalid,
    PhotoInvalidDimensions,
    PhotoSaveFileInvalid,
    FileReferenceExpired,
    FileReferenceInvalid,
)


class FileReferenceSwallowed(Exception):
    pass


def truncate_caption(caption, max_length=1024):
    if not caption:
        return ""
    s = str(caption)
    return s if len(s) <= max_length else s[:max_length - 3] + "..."


async def _with_floodwait(coro_fn, max_retries=3):
    for attempt in range(max_retries):
        try:
            return await coro_fn()
        except (FloodWait, FloodPremiumWait) as e:
            logging.warning(f"FloodWait {e.value}s (attempt {attempt + 1}/{max_retries})")
            if attempt < max_retries - 1:
                await asyncio.sleep(e.value)
            else:
                raise


async def download_media(
    client: Client,
    message: Message,
    file_name=None,
    progress_callback=None,
    progress_args=(),
):
    max_attempts = 5
    for attempt in range(max_attempts):
        try:
            path = await client.download_media(
                message,
                file_name=file_name or "downloads/",
                progress=progress_callback,
                progress_args=progress_args,
            )
            if path and os.path.exists(path) and os.path.getsize(path) == 0:
                try:
                    os.remove(path)
                except Exception:
                    pass
                raise FileReferenceSwallowed(
                    f"download_media returned an empty file on attempt {attempt + 1}"
                )
            return path

        except (FileReferenceExpired, FileReferenceInvalid, FileReferenceSwallowed):
            raise

        except Exception as e:
            if attempt == max_attempts - 1:
                raise
            logging.warning(f"Download attempt {attempt + 1} failed: {e} — retrying")
            await asyncio.sleep(2 * (attempt + 1))


_VIDEO_EXTS     = {".mp4", ".mkv", ".mov", ".avi", ".webm"}
_ANIMATION_EXTS = {".gif"}
_AUDIO_EXTS     = {".mp3", ".m4a", ".flac"}
_VOICE_EXTS     = {".ogg", ".wav"}
_IMAGE_EXTS     = {".jpg", ".jpeg", ".png", ".webp"}


async def upload_media(
    client: Client,
    chat_id,
    file_path: str,
    caption="",
    thumb=None,
    file_name=None,
    has_spoiler=None,
    progress_callback=None,
    progress_args=(),
    **kwargs,
):
    safe_caption = truncate_caption(caption)
    ext = os.path.splitext(file_path)[1].lower()

    if isinstance(chat_id, str) and chat_id.lower() == "me":
        target = "me"
    else:
        try:
            target = int(chat_id)
        except (ValueError, TypeError):
            target = chat_id

    base = {
        "caption": safe_caption,
        "progress": progress_callback,
        "progress_args": progress_args,
    }

    try:
        if ext in _VIDEO_EXTS:
            kw = {**base, "thumb": thumb, **kwargs}
            if has_spoiler is not None:
                kw["has_spoiler"] = has_spoiler
            return await _with_floodwait(
                lambda: client.send_video(target, file_path, supports_streaming=True, **kw)
            )

        if ext in _ANIMATION_EXTS:
            return await _with_floodwait(
                lambda: client.send_animation(target, file_path, **base)
            )

        if ext in _AUDIO_EXTS:
            kw = {**base, "thumb": thumb, "duration": kwargs.get("duration", 0)}
            if file_name:
                kw["file_name"] = file_name
            return await _with_floodwait(
                lambda: client.send_audio(target, file_path, **kw)
            )

        if ext in _VOICE_EXTS:
            kw = {**base, "duration": kwargs.get("duration", 0)}
            return await _with_floodwait(
                lambda: client.send_voice(target, file_path, **kw)
            )

        if ext in _IMAGE_EXTS:
            photo_kw = {**base}
            if has_spoiler is not None:
                photo_kw["has_spoiler"] = has_spoiler
            try:
                return await _with_floodwait(
                    lambda: client.send_photo(target, file_path, **photo_kw)
                )
            except (PhotoExtInvalid, PhotoInvalidDimensions, PhotoSaveFileInvalid):
                logging.warning(f"Photo rejected for {file_path} — sending as document")
                doc_kw = {**base, "thumb": thumb}
                if file_name:
                    doc_kw["file_name"] = file_name
                return await _with_floodwait(
                    lambda: client.send_document(target, file_path, **doc_kw)
                )

        doc_kw = {**base, "thumb": thumb}
        if file_name:
            doc_kw["file_name"] = file_name
        return await _with_floodwait(
            lambda: client.send_document(target, file_path, **doc_kw)
        )

    except (AuthKeyUnregistered, SessionRevoked):
        raise
    except Exception:
        logging.exception(f"Upload error for {file_path}:")
        raise
