import math
import os
import time
import asyncio
from pyrogram import Client, utils
from pyrogram.raw import types, functions
from bot.config import DOWNLOAD_WORKERS, UPLOAD_WORKERS

async def download_media_fast(client: Client, message, file_name, progress_callback=None, progress_args=()):
    """Fast media downloader using separate worker pool logic if supported, or standard with optimized config"""
    # Pyrogram doesn't have a per-call worker setting, but we can manage concurrency via semaphores 
    # and chunking. For now, we use the global client config which is tuned for the total load.
    return await message.download(file_name, progress=progress_callback, progress_args=progress_args)

async def upload_media_fast(client: Client, chat_id, file_path, caption="", progress_callback=None):
    """Fast media uploader"""
    # Standard upload - Pyrogram handles the internal threading
    return await client.send_document(
        chat_id, 
        file_path, 
        caption=caption, 
        progress=progress_callback
    )
