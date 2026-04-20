import os
import logging
from pyrogram import Client
from pyrogram.errors.exceptions.bad_request_400 import (
    PhotoExtInvalid,
    PhotoInvalidDimensions,
    PhotoSaveFileInvalid,
)


def truncate_caption(caption, max_length=1024):
    if not caption:
        return ""
    s = str(caption)
    return s if len(s) <= max_length else s[:max_length - 3] + "..."


async def download_media(client, message, progress=None):
    kwargs = {"file_name": "downloads/"}
    if progress:
        kwargs["progress"] = progress
    return await client.download_media(message, **kwargs)


async def upload_media(
    client: Client,
    chat_id,
    path: str,
    caption: str = "",
    thumb=None,
    file_name: str = None,
    duration: int = 0,
    width: int = 0,
    height: int = 0,
    progress=None,
):
    safe_cap = truncate_caption(caption)
    ext = os.path.splitext(path)[1].lower()
    kw = dict(caption=safe_cap)
    if progress:
        kw["progress"] = progress

    if ext in (".mp4", ".mkv", ".mov", ".avi", ".webm"):
        return await client.send_video(
            chat_id, path,
            thumb=thumb, duration=duration, width=width, height=height,
            supports_streaming=True, file_name=file_name, **kw
        )
    elif ext == ".gif":
        return await client.send_animation(chat_id, path, **kw)
    elif ext in (".mp3", ".m4a", ".flac"):
        return await client.send_audio(
            chat_id, path,
            thumb=thumb, duration=duration, file_name=file_name, **kw
        )
    elif ext in (".ogg", ".opus"):
        return await client.send_voice(chat_id, path, duration=duration, **kw)
    elif ext in (".jpg", ".jpeg", ".png", ".webp"):
        try:
            return await client.send_photo(chat_id, path, **kw)
        except (PhotoExtInvalid, PhotoInvalidDimensions, PhotoSaveFileInvalid):
            return await client.send_document(chat_id, path, file_name=file_name, **kw)
    else:
        return await client.send_document(chat_id, path, thumb=thumb, file_name=file_name, **kw)
