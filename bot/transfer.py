import sys
import math
import os
import time
import asyncio
import logging
from pyrogram import Client, utils
from pyrogram.raw import types, functions
from bot.config import get_smart_download_workers, get_smart_upload_workers, get_smart_chunk_size

async def download_media_fast(client: Client, message, file_name, progress_callback=None, progress_args=()):
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
        file_name,
        progress=progress_callback,
        progress_args=progress_args
    )

async def upload_media_fast(client: Client, chat_id, file_path, caption="", progress_callback=None, **kwargs):
    """Fast media uploader using parallel chunk uploads"""
    file_size = os.path.getsize(file_path) if os.path.exists(file_path) else 0
    
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
                logging.exception("Photo Upload Error:")
            
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
                logging.exception("Voice Upload Error:")

        return await client.send_document(
            chat_id, 
            file_path, 
            caption=caption, 
            progress=progress_callback,
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

    try:
        # Determine media type and use appropriate method
        if file_path.lower().endswith((".mp4", ".mkv", ".mov", ".avi")):
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
                logging.exception("Photo Stream Upload Error:")

        return await client.send_document(
            chat_id,
            file_path,
            caption=caption,
            progress=progress_callback,
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
        try:
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
        except Exception as e:
            logging.error(f"Error uploading part {part_num}: {e}")
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
    # Determine the media type
    if file_path.lower().endswith((".mp4", ".mkv", ".mov", ".avi")):
        media = types.InputMediaUploadedVideo(
            file=input_file,
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
            **kwargs
        )

    # Remove file_path and caption from kwargs as they are handled explicitly
    kwargs.pop("file_path", None)
    kwargs.pop("caption", None)

    return await client.invoke(
        functions.messages.SendMedia(
            peer=await client.resolve_peer(chat_id),
            media=media,
            message=caption,
            random_id=client.rnd_id()
        )
    )
