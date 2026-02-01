import math
import os
import time
import asyncio
import logging
from pyrogram import Client, utils
from pyrogram.raw import types, functions
from bot.config import get_smart_download_workers, get_smart_upload_workers, get_smart_chunk_size

async def download_media_fast(client: Client, message, file_name, progress_callback=None, progress_args=()):
    """Fast media downloader using parallel chunk requests (Pattern from Pyrogram2)"""
    def get_file_size(m):
        if hasattr(m, "video") and m.video: return getattr(m.video, "file_size", 0)
        if hasattr(m, "document") and m.document: return getattr(m.document, "file_size", 0)
        if hasattr(m, "audio") and m.audio: return getattr(m.audio, "file_size", 0)
        if hasattr(m, "photo") and m.photo: return getattr(m.photo, "file_size", 0)
        return 0

    file_size = get_file_size(message)
    workers = get_smart_download_workers(file_size)
    
    # We use Kurigram's built-in download_media which already implements 
    # the parallel downloading logic found in Pyrogram2-style forks.
    return await client.download_media(
        message,
        file_name,
        progress=progress_callback,
        progress_args=progress_args
    )

async def upload_media_fast(client: Client, chat_id, file_path, caption="", progress_callback=None, **kwargs):
    """Fast media uploader using parallel chunk uploads (Pattern from Pyrogram2)"""
    file_size = os.path.getsize(file_path) if os.path.exists(file_path) else 0
    workers = get_smart_upload_workers(file_size)
    
    # We set the number of workers directly on the client for this transmission
    # Pattern seen in Pyrogram2 for handling large files with multiple workers
    client.max_concurrent_transmissions = workers
    
    try:
        if "duration" in kwargs or file_path.lower().endswith((".mp4", ".mkv", ".mov", ".avi")):
            return await client.send_video(
                chat_id, 
                file_path, 
                caption=caption, 
                progress=progress_callback,
                **kwargs
            )
        
        if file_path.lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
            try:
                return await client.send_photo(
                    chat_id,
                    file_path,
                    caption=caption,
                    progress=progress_callback,
                    **kwargs
                )
            except Exception:
                pass
            
        if file_path.lower().endswith(".ogg"):
            try:
                return await client.send_voice(
                    chat_id,
                    file_path,
                    caption=caption,
                    progress=progress_callback,
                    **kwargs
                )
            except Exception:
                pass

        return await client.send_document(
            chat_id, 
            file_path, 
            caption=caption, 
            progress=progress_callback,
            **kwargs
        )
    finally:
        # Reset to a safe default after transfer
        client.max_concurrent_transmissions = 50
