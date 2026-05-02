import os
import asyncio
import logging
from pyrogram import Client
from pyrogram.errors import FloodWait, FloodPremiumWait
from pyrogram.errors.exceptions.bad_request_400 import (
    PhotoExtInvalid,
    PhotoInvalidDimensions,
    PhotoSaveFileInvalid,
    FileReferenceExpired,
    FileReferenceInvalid,
)

from bot.config import API_ID, API_HASH, user_bots


# --- Per-user bot management ----------------------------------------------
# Each user registers their own @BotFather bot via /setbot. We instantiate
# their bot Client lazily on first upload and keep it cached. Their bot is
# the one that performs the actual file upload — the shared owner bot only
# routes commands and never uploads bytes.

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
    )
    try:
        await probe.start()
        me = await probe.get_me()
        return me
    finally:
        try:
            await probe.stop()
        except Exception:
            pass


async def get_user_bot(user_id: int):
    """Return the user's started bot Client, or None if they haven't run /setbot.
    Lazily instantiates and starts on first call; cached afterwards.
    """
    cached = user_bots.get(user_id)
    if cached is not None:
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
            max_concurrent_transmissions=10,
            workers=10,
        )
        try:
            await client.start()
        except Exception as e:
            logging.error(f"Failed to start user bot for {user_id}: {e}")
            return None
        user_bots[user_id] = client
        logging.info(f"Started per-user bot client for user {user_id}")
        return client


async def stop_user_bot(user_id: int):
    """Stop & evict the cached bot client. Call from /rembot."""
    client = user_bots.pop(user_id, None)
    if client is not None:
        try:
            await client.stop()
        except Exception:
            pass
    _user_bot_locks.pop(user_id, None)


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
        except (FloodWait, FloodPremiumWait) as e:
            logging.warning(f"FloodWait {e.value}s on download (attempt {attempt + 1}/3)")
            await asyncio.sleep(e.value)
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
):
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
            return await client.send_document(
                chat_id, path, file_name=file_name, **kw
            )
    else:
        return await client.send_document(
            chat_id, path, thumb=thumb, file_name=file_name, **kw
        )


# Fallback to user_client (userbot) is intentionally restricted to size-limit /
# bot-cant-upload-this-file errors only. We do NOT fall back on FloodWait or
# PEER_FLOOD, because shifting upload load onto the user's irreplaceable Telegram
# account is more dangerous than waiting on a (replaceable) bot.
FALLBACK_FAST_FAIL_MARKERS = (
    "bigger than",          # local size-limit ValueError from save_file (>2 GB)
    "FILE_PARTS_INVALID",
    "FILE_PART_TOO_BIG",
)


def _should_fast_fallback(exc: Exception) -> bool:
    msg = str(exc)
    return any(marker in msg for marker in FALLBACK_FAST_FAIL_MARKERS)


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
    fallback_client: Client = None,
    fallback_chat_id=None,
):
    """
    Upload a local file to Telegram and return the sent Message object.
    Selects the appropriate send method based on file extension.
    Primary client gets up to 3 attempts on transient errors / FloodWait.
    The fallback client (if provided) is used **only** for size-limit /
    FILE_PARTS_INVALID style errors (e.g. >2 GB files where the bot
    physically can't upload). FloodWait and PEER_FLOOD are absorbed by the
    primary client — we never shift them to the user's account.
    """
    safe_cap = truncate_caption(caption)
    ext = os.path.splitext(path)[1].lower()
    kw = dict(caption=safe_cap, progress=progress, progress_args=progress_args)
    has_fallback = fallback_client is not None and fallback_client is not client
    fast_fallback_triggered = False

    last_exc = None
    for attempt in range(3):
        try:
            return await _send_by_ext(
                client, chat_id, path, ext, kw,
                thumb, file_name, duration, width, height,
            )
        except (FloodWait, FloodPremiumWait) as e:
            last_exc = e
            logging.warning(f"FloodWait {e.value}s on upload (attempt {attempt + 1}/3)")
            await asyncio.sleep(e.value)
        except Exception as e:
            last_exc = e
            if has_fallback and _should_fast_fallback(e):
                logging.warning(
                    f"Primary upload hit non-recoverable error ({type(e).__name__}: {e}) "
                    f"— going straight to fallback"
                )
                fast_fallback_triggered = True
                break
            if attempt == 2:
                break
            await asyncio.sleep(2 ** attempt)

    # Only use fallback for size-limit class errors. Plain FloodWait or 3-attempt
    # exhaustion does NOT trigger fallback to the user's account.
    if has_fallback and fast_fallback_triggered:
        target_chat = fallback_chat_id if fallback_chat_id is not None else chat_id
        logging.info(f"Falling back to user_client for upload to {target_chat}")
        try:
            return await _send_by_ext(
                fallback_client, target_chat, path, ext, kw,
                thumb, file_name, duration, width, height,
            )
        except (FloodWait, FloodPremiumWait) as e:
            logging.warning(f"FloodWait {e.value}s on upload fallback")
            await asyncio.sleep(e.value)
            try:
                return await _send_by_ext(
                    fallback_client, target_chat, path, ext, kw,
                    thumb, file_name, duration, width, height,
                )
            except Exception as e2:
                last_exc = e2
        except Exception as e:
            last_exc = e

    if last_exc is not None:
        raise last_exc
    return None
