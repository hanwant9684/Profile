import math
import os
import time
import asyncio
import logging
from pyrogram import Client, utils
from pyrogram.raw import types, functions
from bot.config import get_smart_download_workers, get_smart_upload_workers, get_smart_chunk_size

async def download_media_fast(client: Client, message, file_name, progress_callback=None, progress_args=()):
    """Fast media downloader using smart download-specific worker logic"""
    # Use helper to get file size from various media types, including Story objects
    def get_file_size(m):
        if hasattr(m, "video") and m.video: return getattr(m.video, "file_size", 0)
        if hasattr(m, "document") and m.document: return getattr(m.document, "file_size", 0)
        if hasattr(m, "audio") and m.audio: return getattr(m.audio, "file_size", 0)
        if hasattr(m, "photo") and m.photo: return getattr(m.photo, "file_size", 0)
        return 0

    file_size = get_file_size(message)
    workers = get_smart_download_workers(file_size)
    chunk_size = get_smart_chunk_size(file_size)
    
    # Logging to verify smart logic is working
    logging.info(f"Smart Download: File={file_name}, Size={file_size}, Workers={workers}, Chunk={chunk_size}")
    
    # Using message.download with calculated parameters
    return await message.download(
        file_name, 
        progress=progress_callback, 
        progress_args=progress_args
    )

async def upload_media_fast(client: Client, chat_id, file_path, caption="", progress_callback=None, **kwargs):
    """Fast media uploader using smart upload-specific worker logic"""
    file_size = os.path.getsize(file_path) if os.path.exists(file_path) else 0
    workers = get_smart_upload_workers(file_size)
    chunk_size = get_smart_chunk_size(file_size)
    
    # Logging to verify smart logic is working
    logging.info(f"Smart Upload: File={file_path}, Size={file_size}, Workers={workers}, Chunk={chunk_size}")
    
    # Check if this is a video upload by checking for 'duration' or other video-specific kwargs
    if "duration" in kwargs or file_path.lower().endswith((".mp4", ".mkv", ".mov", ".avi")):
        return await client.send_video(
            chat_id, 
            file_path, 
            caption=caption, 
            progress=progress_callback,
            **kwargs
        )
        
    return await client.send_document(
        chat_id, 
        file_path, 
        caption=caption, 
        progress=progress_callback,
        **kwargs
    )
