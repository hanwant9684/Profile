import os
import logging
import asyncio
from pyrogram import Client
from pyrogram.types import Message
from pyrogram.errors import FloodWait, FloodPremiumWait

from bot.config import get_smart_download_workers

async def download_media_fast(client: Client, message: Message, file_name, progress_callback=None, progress_args=()):
    """Fast media downloader with FloodWait handling"""
    # Get file size to determine worker count
    file_size = 0
    if message.document:
        file_size = message.document.file_size
    elif message.video:
        file_size = message.video.file_size
    elif message.audio:
        file_size = message.audio.file_size
    elif message.photo:
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
        if file_path.lower().endswith((".mp4", ".mkv", ".mov", ".avi")):
            upload_kwargs.update(kwargs)
            upload_kwargs["thumb"] = thumb
            if file_path_lower.endswith(".gif"):
                return await client.send_animation(chat_id, file_path, **upload_kwargs)
            return await client.send_video(
                chat_id,
                file_path,
                supports_streaming=True,
                **upload_kwargs
            )
        #Audio
        elif file_path_lower.endswith((".mp3", ".m4a", ".ogg", ".wav")):
            upload_kwargs["duration"] = kwargs.get("duration", 0)
            if file_path_lower.endswith((".ogg", ".wav")): # Voice formats
                return await client.send_voice(chat_id, file_path, **upload_kwargs)
            #Normal Audio
            upload_kwargs["thumb"] = thumb
            return await client.send_audio(chat_id, file_path, **upload_kwargs)
        #Images
        elif file_path.lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
            return await client.send_photo(
                chat_id,
                file_path,
                **upload_kwargs
            )
         #Documents   
        upload_kwargs["thumb"] = thumb    
        return await client.send_document(
            chat_id,
            file_path,
            **upload_kwargs
        )
    except Exception:
        logging.exception("Upload Error:")
        raise
