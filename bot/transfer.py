import os
import re
import asyncio
import logging
import glob
import shutil
import tempfile
import uuid
import weakref
from pyrogram import Client, StopTransmission
from pyrogram.errors.exceptions.bad_request_400 import (
    PhotoExtInvalid,
    PhotoInvalidDimensions,
    PhotoSaveFileInvalid,
    FileReferenceExpired,
    FileReferenceInvalid,
)
from pyrogram.errors import (
    AuthKeyUnregistered, SessionRevoked, SessionExpired,
    AuthKeyInvalid, AuthKeyPermEmpty, UserDeactivated,
    AccessTokenExpired, AccessTokenInvalid,
)

import time
from bot.config import (
    API_ID, API_HASH, user_bots, user_bots_last_used,
    MAX_CLIENT_DOWNLOADS,
)

# Upload size limits
BOT_MAX_FILE_SIZE  = 2_000_000_000   # 2 GB  — hard cap for bot accounts
PART_SAFE_SIZE     = 1_990_000_000   # ~1.99 GB — safe split boundary (free account)


# --- Per-user bot management ---
# Each user registers their own @BotFather bot via /setbot. We instantiate
# their bot Client lazily on first use and keep it cached. Their bot is the
# one that performs all uploads and copies — the shared owner bot only routes
# commands and never uploads bytes.

_user_bot_locks: dict = {}
_user_bot_health_checked: dict = {}
_USER_BOT_HEALTH_TTL = 30.0
_client_download_semaphores = weakref.WeakKeyDictionary()
_client_download_semaphores_fallback = {}
_client_upload_locks = weakref.WeakKeyDictionary()
_client_upload_locks_fallback = {}


def client_download_semaphore(client) -> asyncio.Semaphore:
    """Return the bounded transfer semaphore associated with a Telegram client."""
    try:
        semaphore = _client_download_semaphores.get(client)
        if semaphore is None:
            semaphore = asyncio.Semaphore(MAX_CLIENT_DOWNLOADS)
            _client_download_semaphores[client] = semaphore
        return semaphore
    except TypeError:
        # Keep compatibility with unusual client implementations that are not
        # weak-referenceable or hashable. Their count is bounded by the client
        # object lifetime in normal Pyrogram/Telethon usage.
        key = id(client)
        semaphore = _client_download_semaphores_fallback.get(key)
        if semaphore is None:
            semaphore = asyncio.Semaphore(MAX_CLIENT_DOWNLOADS)
            _client_download_semaphores_fallback[key] = semaphore
        return semaphore


def client_upload_lock(client) -> asyncio.Lock:
    """Return the lock that serializes uploads through one Telegram client."""
    try:
        lock = _client_upload_locks.get(client)
        if lock is None:
            lock = asyncio.Lock()
            _client_upload_locks[client] = lock
        return lock
    except TypeError:
        key = id(client)
        lock = _client_upload_locks_fallback.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _client_upload_locks_fallback[key] = lock
        return lock

# These errors mean that the client object may still report `is_connected=True`
# while its underlying Pyrogram session/transport is no longer usable.  They
# must not be retried against the same client: the caller needs a fresh client.
_STALE_TRANSPORT_MARKERS = (
    "TCPTransport closed",
    "handler is closed",
    "Connection lost",
    "[Errno 32]",
    "[Errno 104]",
    "unknown constructor",
    "Cannot operate on a closed database",
    "FILE_WRITE_FAILED",
)


def is_stale_transport_error(error: BaseException) -> bool:
    """Return whether *error* requires replacing the Pyrogram client."""
    text = str(error)
    return any(marker in text for marker in _STALE_TRANSPORT_MARKERS)


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
        workers=20,
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
    Re-raises AccessTokenExpired/Invalid so callers can show a user-facing message.
    """
    cached = user_bots.get(user_id)
    if cached is not None:
        # `is_connected` can remain true after Pyrogram's packet handler has
        # died.  A short get_me probe catches that state before save_file is
        # called.  Avoid probing more than once per health TTL.
        try:
            if not cached.is_connected:
                logging.warning(f"Cached user bot for {user_id} is disconnected — evicting and reconnecting")
                await stop_user_bot(user_id)
            else:
                now = time.time()
                last_health = _user_bot_health_checked.get(user_id, 0)
                if now - last_health >= _USER_BOT_HEALTH_TTL:
                    await asyncio.wait_for(cached.get_me(), timeout=10)
                    _user_bot_health_checked[user_id] = now
                user_bots_last_used[user_id] = time.time()
                return cached
        except (AccessTokenExpired, AccessTokenInvalid):
            raise
        except Exception as e:
            logging.warning(f"Cached user bot for {user_id} failed health check — evicting: {e!r}")
            await stop_user_bot(user_id)
            cached = None

    from bot.database import get_bot_token  # lazy to avoid circular import
    bot_token = await get_bot_token(user_id)
    if not bot_token:
        return None

    async with _bot_lock_for(user_id):
        cached = user_bots.get(user_id)
        if cached is not None:
            _user_bot_health_checked[user_id] = time.time()
            return cached
        client = Client(
            f"user_bot_{user_id}",
            api_id=API_ID,
            api_hash=API_HASH,
            bot_token=bot_token,
            in_memory=True,
            no_updates=True,
            sleep_threshold=30,
            workers=20,
        )
        try:
            await asyncio.wait_for(client.start(), timeout=30)
        except (AccessTokenExpired, AccessTokenInvalid) as e:
            # Fix 4: re-raise so batch/mlinks callers can show a clear user-facing message.
            logging.error(f"Bot token invalid for user {user_id}: {type(e).__name__} — clearing stored token")
            from bot.database import remove_bot_token
            await remove_bot_token(user_id)
            raise
        except Exception as e:
            logging.error(f"Failed to start user bot for {user_id}: {e!r}")
            return None
        user_bots[user_id] = client
        user_bots_last_used[user_id] = time.time()
        _user_bot_health_checked[user_id] = time.time()
        logging.info(f"Started per-user bot client for user {user_id}")
        return client


async def stop_user_bot(user_id: int):
    """Stop & evict the cached bot client. Call from /rembot."""
    # Keep the lock object for the lifetime of the process.  Removing it while
    # another get_user_bot() call is waiting can create a second lock for the
    # same user and reintroduce start/stop races.
    lock = _bot_lock_for(user_id)
    async with lock:
        client = user_bots.pop(user_id, None)
        user_bots_last_used.pop(user_id, None)
        _user_bot_health_checked.pop(user_id, None)
        if client is not None:
            logging.info(f"Stopping user bot for user {user_id}")
            try:
                await asyncio.wait_for(client.stop(), timeout=10)
            except Exception:
                pass


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


_TG_CAPTION_LIMIT = 1024


def apply_caption_filter(caption: str, caption_filters: list, append_text: str = "") -> str:
    """Strip words/phrases from caption per user's filter list, then append custom text.
    When appending would exceed Telegram's caption limit, the original caption is trimmed
    to make room so the custom append text is always kept in full."""
    text = caption or ""
    if caption_filters:
        lines = text.split('\n')
        result = []
        for line in lines:
            filtered = line
            for entry in caption_filters:
                filtered = re.sub(re.escape(entry), '', filtered, flags=re.IGNORECASE)
            if filtered.strip():
                result.append(filtered.rstrip())
        text = '\n'.join(result)
    if append_text:
        separator = "\n" if text else ""
        combined = text + separator + append_text
        if len(combined) > _TG_CAPTION_LIMIT:
            # Trim the original caption to make room for the full append text
            budget = _TG_CAPTION_LIMIT - len(append_text) - 1  # 1 for the newline
            if budget > 3:
                text = text[:budget - 1].rstrip() + "…"
            else:
                text = ""
            separator = "\n" if text else ""
        text = text + separator + append_text
    return text


def truncate_caption(caption, max_length=1024):
    if not caption:
        return ""
    s = str(caption)
    return s if len(s) <= max_length else s[:max_length - 3] + "..."


async def check_user_premium(user_client) -> bool:
    """Return True if the user's Telegram account has an active Premium subscription."""
    try:
        me = await user_client.get_me()
        return bool(getattr(me, "is_premium", False))
    except Exception as e:
        logging.warning(f"check_user_premium failed: {e!r} — treating as non-premium")
        return False


_SPLIT_BUFFER = 16 * 1024 * 1024  # 16 MB read buffer — avoids loading GB into RAM

_VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v", ".ts", ".flv"}
_MP4_EXTS   = {".mp4", ".mov", ".m4v"}


def cleanup_download_artifacts(*paths: str) -> None:
    """Remove downloaded files and any sidecar files created during a transfer.

    Transfer failures can leave the destination, segment files, or split parts
    behind.  Only paths supplied by the caller and their known sidecars are
    touched; unrelated concurrent downloads are not scanned or removed.
    """
    seen = set()
    for path in paths:
        if not path:
            continue
        path = os.fspath(path)
        base, ext = os.path.splitext(path)
        candidates = [path, f"{path}.tmp"]
        candidates.extend(glob.glob(f"{path}.seg*"))
        candidates.extend(glob.glob(f"{path}.part*"))
        # split_file/split_video_ffmpeg place parts beside the original
        # extension, e.g. video.part1of2.mp4 rather than video.mp4.part1.
        candidates.extend(glob.glob(f"{base}.part*{ext}"))
        candidates.extend(glob.glob(f"{base}.__ffpart*{ext}"))
        for candidate in candidates:
            if candidate in seen:
                continue
            seen.add(candidate)
            try:
                if os.path.isfile(candidate) or os.path.islink(candidate):
                    os.remove(candidate)
            except OSError:
                logging.debug("Could not remove transfer artifact %s", candidate, exc_info=True)


def _split_file_sync(path: str, part_size: int) -> list:
    total = os.path.getsize(path)
    n_parts = (total + part_size - 1) // part_size
    base, ext = os.path.splitext(path)
    parts = []
    try:
        with open(path, "rb") as src:
            for i in range(1, n_parts + 1):
                part_path = f"{base}.part{i}of{n_parts}{ext}"
                remaining = part_size
                with open(part_path, "wb") as dst:
                    written = 0
                    while remaining > 0:
                        buf = src.read(min(_SPLIT_BUFFER, remaining))
                        if not buf:
                            break
                        dst.write(buf)
                        written += len(buf)
                        remaining -= len(buf)
                if not written or remaining:
                    raise IOError(f"Incomplete split part: {part_path}")
                parts.append(part_path)
    except Exception:
        cleanup_download_artifacts(path, *parts)
        raise
    return parts


async def split_file(path: str, part_size: int = PART_SAFE_SIZE) -> list:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _split_file_sync, path, part_size)


async def split_video_ffmpeg(path: str, part_size: int = PART_SAFE_SIZE) -> list:
    ext = os.path.splitext(path)[1].lower()
    if ext not in _VIDEO_EXTS:
        return await split_file(path, part_size)

    total_size = os.path.getsize(path)
    if total_size <= part_size:
        return [path]

    duration_sec, _, _ = await get_media_info(path)
    if not duration_sec:
        logging.warning(f"split_video_ffmpeg: no duration for {path!r} — raw fallback")
        return await split_file(path, part_size)

    n_parts = (total_size + part_size - 1) // part_size
    seg_seconds = max(1, int(duration_sec / n_parts))

    base, _ = os.path.splitext(path)
    tmp_pattern = f"{base}.__ffpart%03d{ext}"

    cmd = [
        "ffmpeg", "-y",
        "-i", path,
        "-c", "copy",
        "-map", "0",
        "-f", "segment",
        "-segment_time", str(seg_seconds),
        "-reset_timestamps", "1",
    ]
    if ext in _MP4_EXTS:
        cmd += ["-movflags", "+faststart"]
    cmd.append(tmp_pattern)

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=3600)
    except asyncio.TimeoutError:
        logging.warning("split_video_ffmpeg: ffmpeg timed out — raw fallback")
        _cleanup_glob(base, ext)
        return await split_file(path, part_size)
    except FileNotFoundError:
        logging.warning("split_video_ffmpeg: ffmpeg not found — raw fallback")
        return await split_file(path, part_size)

    if proc.returncode != 0:
        logging.warning(
            f"split_video_ffmpeg: ffmpeg exited {proc.returncode} — raw fallback\n"
            f"{stderr.decode()[-400:]}"
        )
        _cleanup_glob(base, ext, prefix=".__ffpart")
        return await split_file(path, part_size)

    import glob as _glob
    tmp_parts = sorted(_glob.glob(f"{base}.__ffpart*{ext}"))
    if not tmp_parts:
        logging.warning("split_video_ffmpeg: ffmpeg produced no output — raw fallback")
        return await split_file(path, part_size)

    if any(os.path.getsize(part) == 0 for part in tmp_parts):
        logging.warning("split_video_ffmpeg: ffmpeg produced an empty part — raw fallback")
        _cleanup_glob(base, ext)
        return await split_file(path, part_size)

    n = len(tmp_parts)
    parts = []
    try:
        for i, tmp in enumerate(tmp_parts, 1):
            dest = f"{base}.part{i}of{n}{ext}"
            os.rename(tmp, dest)
            parts.append(dest)
    except Exception:
        cleanup_download_artifacts(path, *parts)
        _cleanup_glob(base, ext)
        raise

    logging.info(f"split_video_ffmpeg: {path!r} → {n} parts")
    return parts


def _cleanup_glob(base: str, ext: str, prefix: str = ".__ffpart"):
    import glob as _glob
    for f in _glob.glob(f"{base}{prefix}*{ext}"):
        try:
            os.remove(f)
        except Exception:
            pass


async def download_media(client, message, progress=None, progress_args=()):
    """
    Download media from a Telegram message to the downloads/ folder.
    Returns the local file path on success, raises on unrecoverable error.
    """
    os.makedirs("downloads", exist_ok=True)
    progress_args = tuple(progress_args or ())
    for attempt in range(3):
        temp_dir = tempfile.mkdtemp(prefix=".download-", dir="downloads")
        path = None
        try:
            path = await client.download_media(
                message,
                # Isolate each attempt so a failed Pyrogram write cannot be
                # mistaken for a completed download or collide with another
                # album item.
                file_name=f"{temp_dir}{os.sep}",
                progress=progress,
                progress_args=progress_args,
            )
            if not path:
                cleanup_download_artifacts(temp_dir)
                try:
                    os.rmdir(temp_dir)
                except OSError:
                    pass
                return None
            # Guard: Telegram sometimes returns an empty file — catch it early
            # before we waste time uploading 0 bytes or hitting send_media_group errors.
            if not os.path.isfile(path) or os.path.getsize(path) == 0:
                raise ValueError("File size equals to 0 B")
            final_path = os.path.join(
                "downloads",
                f".media-{uuid.uuid4().hex}-{os.path.basename(path)}",
            )
            os.replace(path, final_path)
            shutil.rmtree(temp_dir, ignore_errors=True)
            return final_path
        except (FileReferenceExpired, FileReferenceInvalid):
            cleanup_download_artifacts(path, temp_dir)
            shutil.rmtree(temp_dir, ignore_errors=True)
            raise
        except (AuthKeyUnregistered, SessionRevoked, SessionExpired,
                AuthKeyInvalid, AuthKeyPermEmpty, UserDeactivated):
            cleanup_download_artifacts(path, temp_dir)
            shutil.rmtree(temp_dir, ignore_errors=True)
            raise
        except StopTransmission:
            cleanup_download_artifacts(path, temp_dir)
            shutil.rmtree(temp_dir, ignore_errors=True)
            raise
        except Exception as e:
            cleanup_download_artifacts(path, temp_dir)
            shutil.rmtree(temp_dir, ignore_errors=True)
            if is_stale_transport_error(e):
                # The caller owns client replacement. Retrying this exception
                # on the same broken session only floods the log and delays
                # recovery.
                raise
            if attempt == 2:
                raise
            logging.warning(f"Download attempt {attempt + 1} failed: {e!r}, retrying")
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


async def _upload_media_unlocked(
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
    if not path or not os.path.isfile(path) or os.path.getsize(path) == 0:
        raise ValueError(f"Cannot upload an empty or missing file: {path!r}")
    safe_cap = truncate_caption(caption)
    ext = os.path.splitext(path)[1].lower()
    progress_args = tuple(progress_args or ())
    kw = dict(caption=safe_cap, progress=progress, progress_args=progress_args)

    _NO_RETRY_CODES = (
        "USER_IS_BLOCKED", "INPUT_USER_DEACTIVATED", "PEER_ID_INVALID",
        # Terminal bot-account errors — retrying wastes time and floods logs
        "USER_DEACTIVATED", "ACCESS_TOKEN_INVALID", "ACCESS_TOKEN_EXPIRED",
    )

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
        except StopTransmission:
            raise
        except Exception as e:
            last_exc = e
            # Retrying a dead Pyrogram transport only produces more
            # save_file/handler-is-closed noise.  Let the caller evict the
            # client and retry with a fresh connection.
            if is_stale_transport_error(e):
                raise
            if attempt == 2 or any(code in str(e) for code in _NO_RETRY_CODES):
                break
            logging.warning(f"Upload attempt {attempt + 1} failed: {e!r}, retrying")
            await asyncio.sleep(2 ** attempt)

    if last_exc is not None:
        raise last_exc
    return None


async def upload_media(client: Client, chat_id, path: str, **kwargs):
    """Serialize all uploads made through the same Telegram client."""
    async with client_upload_lock(client):
        return await _upload_media_unlocked(client, chat_id, path, **kwargs)


async def send_media_group(client: Client, chat_id, media):
    """Serialize album uploads made through the same Telegram client."""
    async with client_upload_lock(client):
        return await client.send_media_group(chat_id, media)
