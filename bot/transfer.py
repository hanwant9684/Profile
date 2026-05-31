import os
import asyncio
import logging
from pyrogram import Client
from pyrogram.errors.exceptions.bad_request_400 import (
    PhotoExtInvalid,
    PhotoInvalidDimensions,
    PhotoSaveFileInvalid,
    FileReferenceExpired,
    FileReferenceInvalid,
)

import time
from bot.config import API_ID, API_HASH, user_bots, user_bots_last_used


# --- Per-user bot management ---
# Each user registers their own @BotFather bot via /setbot. We instantiate
# their bot Client lazily on first use and keep it cached. Their bot is the
# one that performs all uploads and copies — the shared owner bot only routes
# commands and never uploads bytes.

_user_bot_locks: dict = {}


def _bot_lock_for(user_id: int) -> asyncio.Lock:
    lock = _user_bot_locks.get(user_id)
    if lock is None:
        lock = asyncio.Lock()
        _user_bot_locks[user_id] = lock
    return lock


async def validate_bot_token(bot_token: str):
    """Spin up a temporary Client to confirm the token is valid.
    Returns the bot's `me` object on success, raises on failure.
    """
    probe = Client(
        f"probe_{bot_token.split(':')[0]}",
        api_id=API_ID,
        api_hash=API_HASH,
        bot_token=bot_token,
        in_memory=True,
        no_updates=True,
        workers=100,
    )
    try:
        await asyncio.wait_for(probe.start(), timeout=20)
        me = await asyncio.wait_for(probe.get_me(), timeout=10)
        logging.info(f"Bot token validated: @{me.username if me.username else me.id}")
        return me
    finally:
        try:
            await asyncio.wait_for(probe.stop(), timeout=10)
        except Exception:
            pass


async def get_user_bot(user_id: int):
    """Return the user's started bot Client, or None if they haven't run /setbot.
    Lazily instantiates and starts on first call; cached afterwards.
    """
    cached = user_bots.get(user_id)
    if cached is not None:
        user_bots_last_used[user_id] = time.time()
        return cached

    from bot.database import get_bot_token  # lazy to avoid circular import
    bot_token = await get_bot_token(user_id)
    if not bot_token:
        return None

    async with _bot_lock_for(user_id):
        cached = user_bots.get(user_id)
        if cached is not None:
            return cached
        client = Client(
            f"user_bot_{user_id}",
            api_id=API_ID,
            api_hash=API_HASH,
            bot_token=bot_token,
            in_memory=True,
            no_updates=True,
            sleep_threshold=30,
            workers=100,
        )
        try:
            await asyncio.wait_for(client.start(), timeout=30)
        except Exception as e:
            logging.error(f"Failed to start user bot for {user_id}: {e}")
            return None
        user_bots[user_id] = client
        user_bots_last_used[user_id] = time.time()
        logging.info(f"Started per-user bot client for user {user_id}")
        return client


async def stop_user_bot(user_id: int):
    """Stop & evict the cached bot client. Call from /rembot."""
    client = user_bots.pop(user_id, None)
    if client is not None:
        logging.info(f"Stopping user bot for user {user_id}")
        try:
            await client.stop()
        except Exception:
            pass
    _user_bot_locks.pop(user_id, None)


def _parse_media_info_sync(path: str) -> tuple[int, int, int]:
    try:
        from pymediainfo import MediaInfo
        info = MediaInfo.parse(path)
        duration = width = height = 0
        for track in info.tracks:
            if track.track_type == "General" and track.duration and not duration:
                duration = int(float(track.duration) / 1000)
            if track.track_type == "Video":
                if track.duration and not duration:
                    duration = int(float(track.duration) / 1000)
                width = int(track.width or 0)
                height = int(track.height or 0)
        return duration, width, height
    except Exception:
        return 0, 0, 0


async def get_media_info(path: str) -> tuple[int, int, int]:
    """
    Extract (duration_sec, width, height) from a local file using pymediainfo.
    Runs in a thread pool executor to avoid blocking the async event loop.
    Falls back to (0, 0, 0) if pymediainfo is not installed or parsing fails.
    """
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _parse_media_info_sync, path)


def _parse_audio_tags_sync(path: str) -> tuple[str, str]:
    try:
        from pymediainfo import MediaInfo
        info = MediaInfo.parse(path)
        for track in info.tracks:
            if track.track_type == "General":
                performer = (
                    getattr(track, "performer", "") or
                    getattr(track, "album_performer", "") or
                    getattr(track, "composer", "") or ""
                )
                title = (
                    getattr(track, "track_name", "") or
                    getattr(track, "title", "") or ""
                )
                return performer.strip(), title.strip()
        return "", ""
    except Exception:
        return "", ""


async def get_audio_tags(path: str) -> tuple[str, str]:
    """
    Extract (performer, title) audio tags from a local file using pymediainfo.
    Returns ("", "") on failure.
    """
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _parse_audio_tags_sync, path)


def truncate_caption(caption, max_length=1024):
    if not caption:
        return ""
    s = str(caption)
    return s if len(s) <= max_length else s[:max_length - 3] + "..."


async def download_media(client, message, progress=None, progress_args=()):
    """
    Download media from a Telegram message to the downloads/ folder.
    Returns the local file path on success, raises on unrecoverable error.
    """
    for attempt in range(3):
        try:
            return await client.download_media(
                message,
                file_name="downloads/",
                progress=progress,
                progress_args=progress_args,
            )
        except (FileReferenceExpired, FileReferenceInvalid):
            raise
        except Exception as e:
            if attempt == 2:
                raise
            logging.warning(f"Download attempt {attempt + 1} failed: {e}, retrying")
            await asyncio.sleep(2 ** attempt)
    return None


async def _send_by_ext(
    client: Client, chat_id, path: str, ext: str, kw: dict,
    thumb, file_name, duration, width, height,
    force_document: bool = False,
    performer: str = "",
    title: str = "",
):
    if not force_document and ext in (".mp4", ".mkv", ".mov", ".avi", ".webm"):
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
            thumb=thumb, duration=duration, file_name=file_name,
            performer=performer or None, title=title or None, **kw
        )
    elif ext in (".ogg", ".opus"):
        return await client.send_voice(chat_id, path, duration=duration, **kw)
    elif ext in (".jpg", ".jpeg", ".png", ".webp"):
        try:
            return await client.send_photo(chat_id, path, **kw)
        except (PhotoExtInvalid, PhotoInvalidDimensions, PhotoSaveFileInvalid):
            return await client.send_document(
                chat_id, path, file_name=file_name, **kw
            )
    else:
        return await client.send_document(
            chat_id, path, thumb=thumb, file_name=file_name, **kw
        )


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
    progress_args=(),
    force_document: bool = False,
    performer: str = "",
    title: str = "",
):
    """
    Upload a local file to Telegram via the user's own bot and return the
    sent Message object. Selects the appropriate send method based on file
    extension. Retries up to 3 times on transient errors and FloodWait.
    """
    safe_cap = truncate_caption(caption)
    ext = os.path.splitext(path)[1].lower()
    kw = dict(caption=safe_cap, progress=progress, progress_args=progress_args)

    _NO_RETRY_CODES = ("USER_IS_BLOCKED", "INPUT_USER_DEACTIVATED", "PEER_ID_INVALID")

    last_exc = None
    for attempt in range(3):
        try:
            return await _send_by_ext(
                client, chat_id, path, ext, kw,
                thumb, file_name, duration, width, height,
                force_document=force_document,
                performer=performer,
                title=title,
            )
        except Exception as e:
            last_exc = e
            if attempt == 2 or any(code in str(e) for code in _NO_RETRY_CODES):
                break
            logging.warning(f"Upload attempt {attempt + 1} failed: {e}, retrying")
            await asyncio.sleep(2 ** attempt)

    if last_exc is not None:
        raise last_exc
    return None
