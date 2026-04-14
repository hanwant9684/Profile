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


class _FileRefSwallowedByPyrogram(Exception):
    """Raised when pyrotgfork's download_media() returns an empty file.

    pyrotgfork's get_file() wraps all errors in a bare
      `except Exception as e: log.exception(e)`
    with NO re-raise (client.py lines 1319-1322). When FileReferenceExpired
    hits that handler the async generator exits without yielding a single byte,
    so download_media() returns a 0-byte file with no exception raised.
    We detect the empty file and raise this sentinel so the refresh path in
    handlers.py fires correctly instead of silently delivering an empty file.
    """
    pass


async def download_media_fast(
    client: Client,
    message: Message,
    file_name=None,
    progress_callback=None,
    progress_args=()
):
    """
    Download using pyrotgfork's native download_media().

    pyrotgfork's download_media() uses the library's built-in transfer
    pipeline which respects Telegram's expected behaviour and does not
    trigger the long-term throttling that custom parallel chunk fetching
    causes. Speed is stable and consistent across many files over time.
    """
    retries = 5
    for i in range(retries):
        try:
            path = await client.download_media(
                message,
                file_name=file_name or "downloads/",
                progress=progress_callback if progress_callback else None,
                progress_args=progress_args
            )

            if path and os.path.exists(path) and os.path.getsize(path) == 0:
                try:
                    os.remove(path)
                except Exception:
                    pass
                # pyrotgfork silently swallows FileReferenceExpired in get_file()
                # (client.py: `except Exception as e: log.exception(e)` with no re-raise).
                # The result is a 0-byte file. Raise our sentinel so handlers.py
                # can re-fetch the message and get a fresh file reference.
                raise _FileRefSwallowedByPyrogram(
                    f"download_media returned empty file on attempt {i+1}: {path} "
                    f"(pyrotgfork swallowed FileReferenceExpired)"
                )

            return path

        except (FloodWait, FloodPremiumWait) as e:
            logging.warning(f"FloodWait: Sleeping for {e.value} seconds")
            await asyncio.sleep(e.value)

        except (FileReferenceExpired, FileReferenceInvalid, _FileRefSwallowedByPyrogram) as e:
            logging.error(f"File reference expired on attempt {i+1}: {e}")
            raise

        except Exception as e:
            if i == retries - 1:
                raise e
            logging.error(f"Download attempt {i+1} failed: {e}. Retrying...")
            await asyncio.sleep(2 * (i + 1))


def truncate_caption(caption, max_length=1024):
    if not caption:
        return ""
    caption_str = str(caption)
    if len(caption_str) <= max_length:
        return caption_str
    return caption_str[:max_length - 3] + "..."


async def _send_with_floodwait(coro_fn, max_retries=3):
    for attempt in range(max_retries):
        try:
            return await coro_fn()
        except (FloodWait, FloodPremiumWait) as e:
            wait = e.value
            logging.warning(f"Upload FloodWait {wait}s (attempt {attempt+1}/{max_retries})")
            if attempt < max_retries - 1:
                await asyncio.sleep(wait)
            else:
                raise


async def upload_media_fast(
    client: Client,
    chat_id,
    file_path,
    caption="",
    thumb=None,
    file_name=None,
    has_spoiler=None,
    progress_callback=None,
    progress_args=(),
    **kwargs
):
    """
    Upload using pyrotgfork's native send methods with FloodWait retry
    and PhotoExtInvalid fallback.

    pyrotgfork's save_file() already handles multi-part uploads internally
    for large files — no additional parallelism is needed here.
    """
    safe_caption = truncate_caption(caption)
    file_path_lower = file_path.lower()

    upload_kwargs = {
        "caption": safe_caption,
        "progress": progress_callback,
        "progress_args": progress_args,
    }

    try:
        if not client.is_connected:
            try:
                await client.start()
            except Exception as start_err:
                if "already" not in str(start_err).lower():
                    raise

        if isinstance(chat_id, str) and chat_id.lower() == "me":
            target_id = "me"
        else:
            try:
                target_id = int(chat_id)
            except (ValueError, TypeError):
                target_id = chat_id

        if file_path_lower.endswith((".mp4", ".mkv", ".mov", ".avi")):
            upload_kwargs.update(kwargs)
            upload_kwargs["thumb"] = thumb
            if file_name:
                upload_kwargs["file_name"] = file_name
            if has_spoiler is not None:
                upload_kwargs["has_spoiler"] = has_spoiler
            if file_path_lower.endswith(".gif"):
                return await _send_with_floodwait(
                    lambda: client.send_animation(target_id, file_path, **upload_kwargs)
                )
            return await _send_with_floodwait(
                lambda: client.send_video(target_id, file_path, supports_streaming=True, **upload_kwargs)
            )

        elif file_path_lower.endswith((".mp3", ".m4a", ".ogg", ".wav")):
            upload_kwargs["duration"] = kwargs.get("duration", 0)
            if file_path_lower.endswith((".ogg", ".wav")):
                return await _send_with_floodwait(
                    lambda: client.send_voice(target_id, file_path, **upload_kwargs)
                )
            upload_kwargs["thumb"] = thumb
            if file_name:
                upload_kwargs["file_name"] = file_name
            return await _send_with_floodwait(
                lambda: client.send_audio(target_id, file_path, **upload_kwargs)
            )

        elif file_path_lower.endswith((".jpg", ".jpeg", ".png", ".webp")):
            try:
                if has_spoiler is not None:
                    upload_kwargs["has_spoiler"] = has_spoiler
                return await _send_with_floodwait(
                    lambda: client.send_photo(target_id, file_path, **upload_kwargs)
                )
            except (PhotoExtInvalid, PhotoInvalidDimensions, PhotoSaveFileInvalid):
                logging.warning(f"Photo rejected (invalid ext, dimensions, or save error) for {file_path} — falling back to send_document")
                doc_kwargs = dict(upload_kwargs)
                doc_kwargs.pop("has_spoiler", None)
                doc_kwargs["thumb"] = thumb
                if file_name:
                    doc_kwargs["file_name"] = file_name
                return await _send_with_floodwait(
                    lambda: client.send_document(target_id, file_path, **doc_kwargs)
                )

        upload_kwargs["thumb"] = thumb
        if file_name:
            upload_kwargs["file_name"] = file_name
        return await _send_with_floodwait(
            lambda: client.send_document(target_id, file_path, **upload_kwargs)
        )

    except (AuthKeyUnregistered, SessionRevoked) as e:
        logging.error(f"AuthKeyUnregistered during transfer for chat {chat_id}: {e}")
        raise
    except Exception:
        logging.exception("Upload Error:")
        raise
