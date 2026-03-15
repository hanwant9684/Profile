import os
import logging
import asyncio
from pyrogram import Client
from pyrogram.types import Message
from pyrogram.errors import FloodWait, FloodPremiumWait, AuthKeyUnregistered, SessionRevoked
from pyrogram.errors.exceptions.unauthorized_401 import AuthKeyUnregistered as AuthKeyUnregistered401
from pyrogram.errors.exceptions.bad_request_400 import PhotoExtInvalid
from pyrogram.errors.exceptions.bad_request_400 import FileReferenceExpired, FileReferenceInvalid
from bot.config import get_smart_download_workers

async def download_media_fast(client: Client, message: Message, file_name, progress_callback=None, progress_args=()):
    """Fast media downloader with FloodWait handling"""
    file_size = 0
    if getattr(message, "document", None):
        file_size = message.document.file_size
    elif getattr(message, "video", None):
        file_size = message.video.file_size
    elif getattr(message, "audio", None):
        file_size = message.audio.file_size
    elif getattr(message, "photo", None):
        file_size = message.photo.sizes[-1].file_size
    elif type(message).__name__ == "Story":
        if getattr(message, "video", None):
            file_size = message.video.file_size
        elif getattr(message, "photo", None):
            file_size = message.photo.sizes[-1].file_size

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
                logging.warning(f"Download returned empty file on attempt {i+1}: {path} — retrying")
                try:
                    os.remove(path)
                except Exception:
                    pass
                if i < retries - 1:
                    await asyncio.sleep(2 * (i + 1))
                    continue
                raise ValueError(f"File downloaded as empty after {retries} attempts: {path}")
            return path
        except (FloodWait, FloodPremiumWait) as e:
            logging.warning(f"FloodWait: Sleeping for {e.value} seconds")
            await asyncio.sleep(e.value)
        except (FileReferenceExpired, FileReferenceInvalid) as e:
            logging.error(f"File reference expired on attempt {i+1}: {e}")
            raise
        except Exception as e:
            try:
                import glob as _glob
                for tmp in _glob.glob("downloads/*.temp"):
                    try:
                        os.remove(tmp)
                    except Exception:
                        pass
            except Exception:
                pass
            if i == retries - 1:
                raise e
            logging.error(f"Download attempt {i+1} failed: {e}. Retrying...")
            await asyncio.sleep(2 * (i + 1))

def truncate_caption(caption, max_length=1024):
    """Safely truncate caption to Telegram's limit (1024 characters)"""
    if not caption:
        return ""
    caption_str = str(caption)
    if len(caption_str) <= max_length:
        return caption_str
    return caption_str[:max_length-3] + "..."

def check_file_size(file_path, max_size_mib=2000):
    """Check if file size is within Telegram limits"""
    if not os.path.exists(file_path):
        raise ValueError(f"File not found: {file_path}")
    
    file_size_bytes = os.path.getsize(file_path)
    file_size_mib = file_size_bytes / (1024 * 1024)
    
    if file_size_mib > max_size_mib:
        raise ValueError(f"File size ({file_size_mib:.2f} MiB) exceeds Telegram limit of {max_size_mib} MiB")
    
    if file_size_bytes == 0:
        raise ValueError(f"File is empty: {file_path}")
    
    return file_size_bytes

async def _send_with_floodwait(coro_fn, max_retries=3):
    """Execute a send coroutine, sleeping through FloodWait up to max_retries times."""
    for attempt in range(max_retries):
        try:
            return await coro_fn()
        except (FloodWait, FloodPremiumWait) as e:
            wait = e.value
            logging.warning(f"Upload FloodWait {wait}s (attempt {attempt+1}/{max_retries})")
            if attempt < max_retries - 1:
                await asyncio.sleep(min(wait, 60))
            else:
                raise

async def upload_media_fast(client: Client, chat_id, file_path, caption="", thumb=None, progress_callback=None, progress_args=(), **kwargs):
    """Upload function with FloodWait retry and PhotoExtInvalid fallback."""
    safe_caption = truncate_caption(caption)

    file_path_lower = file_path.lower()
    
    try:
        check_file_size(file_path)
    except ValueError as e:
        logging.error(f"File validation error: {e}")
        return None
    
    upload_kwargs = {
        "caption": safe_caption,
        "progress": progress_callback,
        "progress_args": progress_args,
    }

    try:
        if not client.is_connected:
            await client.start()
            
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
            return await _send_with_floodwait(
                lambda: client.send_audio(target_id, file_path, **upload_kwargs)
            )

        elif file_path_lower.endswith((".jpg", ".jpeg", ".png", ".webp")):
            try:
                return await _send_with_floodwait(
                    lambda: client.send_photo(target_id, file_path, **upload_kwargs)
                )
            except PhotoExtInvalid:
                logging.warning(f"PhotoExtInvalid for {file_path} — falling back to send_document")
                doc_kwargs = dict(upload_kwargs)
                doc_kwargs["thumb"] = thumb
                return await _send_with_floodwait(
                    lambda: client.send_document(target_id, file_path, **doc_kwargs)
                )

        upload_kwargs["thumb"] = thumb
        return await _send_with_floodwait(
            lambda: client.send_document(target_id, file_path, **upload_kwargs)
        )

    except (AuthKeyUnregistered, AuthKeyUnregistered401, SessionRevoked) as e:
        logging.error(f"AuthKeyUnregistered during transfer for chat {chat_id}: {e}")
        raise
    except Exception:
        logging.exception("Upload Error:")
        raise
