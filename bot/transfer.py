import os
import logging
import mimetypes
from hydrogram import Client
from hydrogram.types import Message

from bot.config import get_smart_download_workers

async def download_media_fast(client: Client, message: Message, file_name, progress_callback=None, progress_args=()):
    """Fast media downloader using parallel chunk requests"""
    return await client.download_media(
        message,
        file_name=file_name or "downloads/",
        progress=progress_callback if progress_callback else None,
        progress_args=progress_args
    )

async def upload_media_fast(client: Client, chat_id, file_path, caption="", thumb=None, progress_callback=None, progress_args=(), **kwargs):
    """Refactored upload function focusing on hardware-accelerated transfers via TgCrypto."""
    safe_caption = str(caption) if caption is not None else ""
    mime_type, _ = mimetypes.guess_type(file_path)
    
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
        if mime_type:
            if mime_type.startswith("video/"):
                return await client.send_video(
                    chat_id,
                    file_path,
                    supports_streaming=True,
                    **upload_kwargs
                )
            elif mime_type.startswith("audio/"):
                if file_path.lower().endswith(".ogg"):
                    return await client.send_voice(
                        chat_id,
                        file_path,
                        **upload_kwargs
                    )
                else:
                    return await client.send_audio(
                        chat_id,
                        file_path,
                        **upload_kwargs
                    )
            elif mime_type.startswith("image/"):
                return await client.send_photo(
                    chat_id,
                    file_path,
                    **upload_kwargs
                )
        
        # Fallback for document or unknown types
        return await client.send_document(
            chat_id,
            file_path,
            **upload_kwargs
        )
    except Exception:
        logging.exception("Upload Error:")
        raise
