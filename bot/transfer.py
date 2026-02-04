import os
import logging
from pyrogram import Client
from pyrogram.types import Message

from bot.config import get_smart_download_workers

async def download_media_fast(client: Client, message: Message, file_name, progress_callback=None, progress_args=()):
    """Fast media downloader using parallel chunk requests"""
    # Get file size to determine worker count
    file_size = 0
    if message.document:
        file_size = message.document.file_size
    elif message.video:
        file_size = message.video.file_size
    elif message.audio:
        file_size = message.audio.file_size
    elif message.photo:
        file_size = message.photo.file_size

    return await client.download_media(
        message,
        file_name=file_name or "downloads/",
        progress=progress_callback if progress_callback else None,
        progress_args=progress_args
    )

async def upload_media_fast(client: Client, chat_id, file_path, caption="", thumb=None, progress_callback=None, progress_args=(), **kwargs):
    """Refactored upload function focusing on hardware-accelerated transfers via TgCrypto."""
    safe_caption = str(caption) if caption is not None else ""
    
    # Base arguments for all upload methods
    upload_kwargs = {
        "caption": safe_caption,
        "progress": progress_callback,
        "progress_args": progress_args,
        "thumb": thumb
    }
    # Merge additional kwargs (like duration, width, height)
    upload_kwargs.update(kwargs)

    try:
        if file_path.lower().endswith((".mp4", ".mkv", ".mov", ".avi")):
            return await client.send_video(
                chat_id,
                file_path,
                supports_streaming=True,
                **upload_kwargs
            )
        
        if file_path.lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
            return await client.send_photo(
                chat_id,
                file_path,
                **upload_kwargs
            )
            
        return await client.send_document(
            chat_id,
            file_path,
            **upload_kwargs
        )
    except Exception:
        logging.exception("Upload Error:")
        raise
