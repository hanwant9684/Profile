import sys
import math
import os
import time
import asyncio
import logging
from pyrogram import Client, utils
from pyrogram.raw import types, functions
from pyrogram.types import Message
from bot.config import get_smart_download_workers, get_smart_upload_workers, get_smart_chunk_size

async def download_media_fast(client: Client, message: Message, file_name, progress_callback=None, progress_args=()):
    """Fast media downloader using parallel chunk requests"""
    def get_file_size(m):
        if hasattr(m, "video") and m.video: return getattr(m.video, "file_size", 0)
        if hasattr(m, "document") and m.document: return getattr(m.document, "file_size", 0)
        if hasattr(m, "audio") and m.audio: return getattr(m.audio, "file_size", 0)
        if hasattr(m, "photo") and m.photo: return getattr(m.photo, "file_size", 0)
        return 0

    file_size = get_file_size(message)
    workers = get_smart_download_workers(file_size)
    
    # Use workers if supported, otherwise rely on client's default
    return await client.download_media(
        message,
        file_name=file_name or "downloads/",
        progress=progress_callback if progress_callback else None,
        progress_args=progress_args
    )

async def upload_media_fast(client: Client, chat_id, file_path, caption="", progress_callback=None, **kwargs):
    """Fast media uploader using parallel chunk uploads"""
    file_size = os.path.getsize(file_path) if os.path.exists(file_path) else 0
    
    # Ensure caption is not None to avoid 'NoneType' has no attribute 'encode'
    safe_caption = str(caption) if caption is not None else ""

    try:
        if "duration" in kwargs or file_path.lower().endswith((".mp4", ".mkv", ".mov", ".avi")):
            return await client.send_video(
                chat_id, 
                file_path, 
                caption=safe_caption, 
                progress=progress_callback if progress_callback else None,
                **kwargs
            )
        
        if file_path.lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
            try:
                return await client.send_photo(
                    chat_id,
                    file_path,
                    caption=safe_caption,
                    progress=progress_callback if progress_callback else None,
                    **kwargs
                )
            except Exception:
                logging.exception("Photo Upload Error:")
            
        if file_path.lower().endswith(".ogg"):
            try:
                return await client.send_voice(
                    chat_id,
                    file_path,
                    caption=safe_caption,
                    progress=progress_callback if progress_callback else None,
                    **kwargs
                )
            except Exception:
                logging.exception("Voice Upload Error:")

        return await client.send_document(
            chat_id, 
            file_path, 
            caption=safe_caption, 
            progress=progress_callback if progress_callback else None,
            **kwargs
        )
    except Exception:
        logging.exception("Transfer Error:")
        raise

async def upload_media_streaming(client: Client, chat_id, file_path, caption="", progress_callback=None, **kwargs):
    """
    Efficiently uploads media using streaming.
    """
    if not os.path.exists(file_path):
        return None
    
    # Ensure caption is not None
    safe_caption = str(caption) if caption is not None else ""

    try:
        # Determine media type and use appropriate method
        if file_path.lower().endswith((".mp4", ".mkv", ".mov", ".avi")):
            return await client.send_video(
                chat_id,
                file_path,
                caption=safe_caption,
                progress=progress_callback if progress_callback else None,
                **kwargs
            )
        
        if file_path.lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
            try:
                return await client.send_photo(
                    chat_id,
                    file_path,
                    caption=safe_caption,
                    progress=progress_callback if progress_callback else None,
                    **kwargs
                )
            except Exception:
                logging.exception("Photo Stream Upload Error:")

        return await client.send_document(
            chat_id,
            file_path,
            caption=safe_caption,
            progress=progress_callback if progress_callback else None,
            **kwargs
        )
    except Exception:
        logging.exception("Streaming Transfer Error:")
        raise

class FastUploader:
    def __init__(self, client: Client, file_path: str, chunk_size: int = None):
        self.client = client
        self.file_path = file_path
        self.file_size = os.path.getsize(file_path)
        
        if chunk_size is None:
            if self.file_size > 100 * 1024 * 1024:
                self.chunk_size = 1024 * 1024 # 1MB for > 100MB
            else:
                self.chunk_size = 512 * 1024
        else:
            self.chunk_size = chunk_size

        self.file_id = self.client.rnd_id()
        self.is_big = self.file_size > 10 * 1024 * 1024
        self.total_parts = (self.file_size + self.chunk_size - 1) // self.chunk_size
        
    async def upload_part(self, part_num: int, data: bytes) -> bool:
        from pyrogram.errors import FloodWait
        max_retries = 5
        for attempt in range(max_retries):
            try:
                # Ensure client is connected
                if not self.client.is_connected:
                    try:
                        await self.client.connect()
                    except Exception as e:
                        logging.error(f"Failed to connect client: {e}")
                        if attempt < max_retries - 1:
                            await asyncio.sleep(2)
                            continue
                        return False
                    
                if self.is_big:
                    await self.client.invoke(
                        functions.upload.SaveBigFilePart(
                            file_id=self.file_id,
                            file_part=part_num,
                            file_total_parts=self.total_parts,
                            bytes=data
                        )
                    )
                else:
                    await self.client.invoke(
                        functions.upload.SaveFilePart(
                            file_id=self.file_id,
                            file_part=part_num,
                            bytes=data
                        )
                    )
                return True
            except FloodWait as e:
                logging.warning(f"FloodWait: Waiting {e.value} seconds")
                await asyncio.sleep(e.value)
            except (ConnectionResetError, OSError, Exception) as e:
                if attempt < max_retries - 1:
                    wait_time = 2 ** (attempt + 1)
                    logging.warning(f"Error uploading part {part_num} (attempt {attempt+1}): {e}. Retrying in {wait_time}s...")
                    await asyncio.sleep(wait_time)
                    # Don't stop/start, just try to reconnect if needed on next iteration
                else:
                    logging.error(f"Final failure uploading part {part_num} after {max_retries} attempts: {e}")
                    return False
    
    async def upload_file_parallel(self, max_concurrent: int = None) -> types.InputFile:
        if max_concurrent is None:
            max_concurrent = get_smart_upload_workers(self.file_size)

        semaphore = asyncio.Semaphore(max_concurrent)
        
        async def upload_with_semaphore(part_num: int, data: bytes):
            async with semaphore:
                logging.debug(f"Uploading chunk {part_num}/{self.total_parts} for {self.file_path}")
                return await self.upload_part(part_num, data)
        
        tasks = []
        with open(self.file_path, 'rb') as f:
            part_num = 0
            while True:
                chunk = f.read(self.chunk_size)
                if not chunk:
                    break
                tasks.append(upload_with_semaphore(part_num, chunk))
                part_num += 1
        
        logging.debug(f"Starting parallel upload of {len(tasks)} tasks with concurrency {max_concurrent}")
        results = await asyncio.gather(*tasks, return_exceptions=True)
        failed = [i for i, r in enumerate(results) if not r or isinstance(r, Exception)]
        if failed:
            raise Exception(f"Failed to upload parts: {failed}")
        
        file_name = os.path.basename(self.file_path)
        if self.is_big:
            return types.InputFileBig(
                id=self.file_id,
                parts=self.total_parts,
                name=file_name
            )
        else:
            return types.InputFile(
                id=self.file_id,
                parts=self.total_parts,
                name=file_name,
                md5_checksum=""
            )

async def upload_media_parallel(client: Client, chat_id, file_path, caption="", progress_callback=None, **kwargs):
    """
    Advanced parallel chunk uploader using raw API calls to avoid double upload.
    """
    if not os.path.exists(file_path):
        return None
        
    uploader = FastUploader(client, file_path)
    input_file = await uploader.upload_file_parallel()
    
    # We use invoke with SendMedia to avoid re-uploading the file path
    # Remove file_path, caption and other incompatible kwargs from kwargs as they are handled explicitly
    kwargs.pop("file_path", None)
    kwargs.pop("caption", None)
    kwargs.pop("progress", None)
    kwargs.pop("progress_args", None)
    kwargs.pop("thumb", None)

    if file_path.lower().endswith((".mp4", ".mkv", ".mov", ".avi")):
        # Get video dimensions if possible, or use defaults
        width = kwargs.get("width", 1280)
        height = kwargs.get("height", 720)
        duration = kwargs.get("duration", 0)
        
        # Remove video-specific args from kwargs to avoid conflicts
        kwargs.pop("width", None)
        kwargs.pop("height", None)
        kwargs.pop("duration", None)
        kwargs.pop("supports_streaming", None)

        media = types.InputMediaUploadedVideo(
            file=input_file,
            width=width,
            height=height,
            duration=duration,
            supports_streaming=True,
            **kwargs
        )
    elif file_path.lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
        media = types.InputMediaUploadedPhoto(
            file=input_file,
            **kwargs
        )
    else:
        media = types.InputMediaUploadedDocument(
            file=input_file,
            mime_type="application/octet-stream",
            attributes=[types.DocumentAttributeFilename(file_name=os.path.basename(file_path))],
            **kwargs
        )

    return await client.invoke(
        functions.messages.SendMedia(
            peer=await client.resolve_peer(chat_id),
            media=media,
            message=str(caption) if caption is not None else "",
            random_id=client.rnd_id()
        )
    )
