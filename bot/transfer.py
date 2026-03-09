import os
import logging
import asyncio
from pyrogram import Client
from pyrogram.types import Message
from pyrogram.errors import FloodWait, FloodPremiumWait, AuthKeyUnregistered, SessionRevoked
from pyrogram.errors.exceptions.unauthorized_401 import AuthKeyUnregistered as AuthKeyUnregistered401

from bot.config import get_smart_download_workers

async def download_media_fast(client: Client, message: Message, file_name, progress_callback=None, progress_args=()):
    """Fast media downloader with FloodWait handling"""
    # Get file size to determine worker count
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
            return await client.download_media(
                message,
                file_name=file_name or "downloads/",
                progress=progress_callback if progress_callback else None,
                progress_args=progress_args
            )
        except (FloodWait, FloodPremiumWait) as e:
            logging.warning(f"FloodWait: Sleeping for {e.value} seconds")
            await asyncio.sleep(e.value)
        except Exception as e:
            if i == retries - 1:
                raise e
            logging.error(f"Download attempt {i+1} failed: {e}. Retrying...")
            await asyncio.sleep(2 * (i + 1))

async def upload_media_fast(client: Client, chat_id, file_path, caption="", thumb=None, progress_callback=None, progress_args=(), **kwargs):
    """Refactored upload function focusing on hardware-accelerated transfers via TgCrypto."""
    safe_caption = str(caption) if caption is not None else ""

    file_path_lower = file_path.lower()
    # Base arguments for all upload methods
    upload_kwargs = {
        "caption": safe_caption,
        "progress": progress_callback,
        "progress_args": progress_args,
    }

    if not os.path.exists(file_path) or os.path.getsize(file_path) == 0:
        logging.error(f"Upload failed: File {file_path} is empty or does not exist.")
        return None

    try:
        if not client.is_connected:
            await client.start()
            
        # Resolve chat_id: if it's "me", we keep it as is, otherwise ensure it's an int
        if isinstance(chat_id, str) and chat_id.lower() == "me":
            target_id = "me"
        else:
            try:
                target_id = int(chat_id)
            except (ValueError, TypeError):
                target_id = chat_id

        # Peer resolution attempt for channels/groups
        try:
            await client.get_chat(target_id)
        except Exception as e:
            logging.debug(f"Upload peer resolution failed for {target_id}: {e}")

        if file_path.lower().endswith((".mp4", ".mkv", ".mov", ".avi")):
            upload_kwargs.update(kwargs)
            upload_kwargs["thumb"] = thumb
            if file_path_lower.endswith(".gif"):
                return await client.send_animation(target_id, file_path, **upload_kwargs)
            return await client.send_video(
                target_id,
                file_path,
                supports_streaming=True,
                **upload_kwargs
            )
        #Audio
        elif file_path_lower.endswith((".mp3", ".m4a", ".ogg", ".wav")):
            upload_kwargs["duration"] = kwargs.get("duration", 0)
            if file_path_lower.endswith((".ogg", ".wav")): # Voice formats
                return await client.send_voice(target_id, file_path, **upload_kwargs)
            #Normal Audio
            upload_kwargs["thumb"] = thumb
            return await client.send_audio(target_id, file_path, **upload_kwargs)
        #Images
        elif file_path.lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
            return await client.send_photo(
                target_id,
                file_path,
                **upload_kwargs
            )
         #Documents   
        upload_kwargs["thumb"] = thumb    
        return await client.send_document(
            target_id,
            file_path,
            **upload_kwargs
        )
    except (AuthKeyUnregistered, AuthKeyUnregistered401, SessionRevoked) as e:
        logging.error(f"AuthKeyUnregistered during transfer for chat {chat_id}: {e}")
        raise
    except Exception:
        logging.exception("Upload Error:")
        raise
