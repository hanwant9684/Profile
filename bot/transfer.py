import os
import time
import uuid
import shutil
import asyncio
import logging
import mimetypes
from pyrogram import Client
from pyrogram.types import Message
from pyrogram.errors import FloodWait, FloodPremiumWait, AuthKeyUnregistered, SessionRevoked
from pyrogram.errors.exceptions.unauthorized_401 import AuthKeyUnregistered as AuthKeyUnregistered401
from pyrogram.errors.exceptions.bad_request_400 import PhotoExtInvalid
from pyrogram.errors.exceptions.bad_request_400 import FileReferenceExpired, FileReferenceInvalid

# Must match pyrotgfork's internal chunk size (client.py line 1153)
_PYROGRAM_CHUNK_SIZE = 1024 * 1024  # 1 MB

# Only use parallel download for files larger than this — smaller files don't benefit
_MIN_PARALLEL_SIZE = 5 * 1024 * 1024  # 5 MB


async def download_media_parallel(
    client: Client,
    message: Message,
    file_name=None,
    num_workers: int = 2,
    progress_callback=None,
    progress_args=()
):
    """
    Parallel chunk downloader — overcomes pyrotgfork's ~4 MB/s serial ceiling.

    Background:
      pyrotgfork fetches file chunks ONE at a time (1 MB each) over a single
      media session.  Each GetFile RPC takes ~250-300 ms at Telegram's DC,
      giving a hard ceiling of ~4 MB/s per connection regardless of network speed.

      This function splits the file into `num_workers` byte ranges and fetches
      them CONCURRENTLY via asyncio.gather().  pyrotgfork's Session.invoke()
      uses per-request msg_id matching and supports multiple in-flight requests
      on the same TCP connection, so N workers × ~4 MB/s ≈ N× throughput.

      Each user already has their own Client → own media_sessions dict → own
      TCP connection to Telegram's file DC, so there is zero inter-user
      contention even with 10 concurrent users.

      Falls back silently to standard serial download on any unrecoverable error.

    Upload note:
      pyrotgfork's save_file() already uses 3 sessions × 4 workers (12 streams)
      for files > 10 MB — upload is already optimally parallelised in the library.
    """
    from pyrogram.file_id import FileId

    media = (
        getattr(message, "document", None) or
        getattr(message, "video", None) or
        getattr(message, "audio", None) or
        getattr(message, "voice", None) or
        getattr(message, "video_note", None) or
        getattr(message, "animation", None) or
        getattr(message, "sticker", None)
    )

    if not media:
        return await download_media_fast(client, message, file_name, progress_callback, progress_args)

    file_size = getattr(media, "file_size", 0) or 0
    file_id_str = getattr(media, "file_id", None)

    if not file_id_str or file_size < _MIN_PARALLEL_SIZE:
        return await download_media_fast(client, message, file_name, progress_callback, progress_args)

    try:
        file_id_obj = FileId.decode(file_id_str)
    except Exception as e:
        logging.warning(f"Parallel download: could not decode file_id ({e}), using serial fallback")
        return await download_media_fast(client, message, file_name, progress_callback, progress_args)

    # Determine output file extension from original filename, then MIME type
    original_name = getattr(media, "file_name", "") or ""
    ext = os.path.splitext(original_name)[-1] if original_name else ""
    if not ext:
        mime = getattr(media, "mime_type", "") or ""
        ext_map = {
            "video/mp4": ".mp4", "video/x-matroska": ".mkv",
            "video/quicktime": ".mov", "video/x-msvideo": ".avi",
            "video/webm": ".webm", "audio/mpeg": ".mp3",
            "audio/mp4": ".m4a", "audio/ogg": ".ogg",
            "audio/wav": ".wav", "audio/flac": ".flac",
            "image/jpeg": ".jpg", "image/png": ".png",
            "image/webp": ".webp",
        }
        ext = ext_map.get(mime) or mimetypes.guess_extension(mime) or ".bin"

    uid = uuid.uuid4().hex[:10]
    out_path = os.path.join("downloads", f"dl_{uid}{ext}")

    # Split file into N chunk ranges
    total_chunks = (file_size + _PYROGRAM_CHUNK_SIZE - 1) // _PYROGRAM_CHUNK_SIZE
    actual_workers = min(num_workers, total_chunks)

    base, rem = divmod(total_chunks, actual_workers)
    ranges = []  # (offset_in_chunks, count_in_chunks)
    pos = 0
    for i in range(actual_workers):
        cnt = base + (1 if i < rem else 0)
        ranges.append((pos, cnt))
        pos += cnt

    temp_paths = [os.path.join("downloads", f"dl_{uid}_p{i}.tmp") for i in range(actual_workers)]
    downloaded = [0] * actual_workers
    last_report = [time.time()]
    failed = [False]

    async def worker(idx, chunk_offset, chunk_limit):
        """Fetch one byte-range with FloodWait retry support."""
        for attempt in range(3):
            try:
                # Clear any partial data from a previous attempt
                open(temp_paths[idx], "wb").close()
                with open(temp_paths[idx], "wb") as fh:
                    async for chunk in client.get_file(
                        file_id_obj, file_size,
                        limit=chunk_limit,
                        offset=chunk_offset
                    ):
                        fh.write(chunk)
                        downloaded[idx] += len(chunk)
                        if progress_callback and not failed[0]:
                            now = time.time()
                            if now - last_report[0] >= 2:
                                last_report[0] = now
                                total_dl = min(sum(downloaded), file_size)
                                if asyncio.iscoroutinefunction(progress_callback):
                                    await progress_callback(total_dl, file_size, *progress_args)
                return  # success

            except (FloodWait, FloodPremiumWait) as e:
                wait = e.value
                logging.warning(f"Parallel worker {idx} FloodWait {wait}s (attempt {attempt+1}/3)")
                if attempt < 2:
                    downloaded[idx] = 0
                    await asyncio.sleep(wait)
                else:
                    failed[0] = True
                    raise

            except (FileReferenceExpired, FileReferenceInvalid):
                failed[0] = True
                raise

            except Exception as e:
                if attempt < 2:
                    logging.warning(f"Parallel worker {idx} error (attempt {attempt+1}): {e}, retrying")
                    downloaded[idx] = 0
                    await asyncio.sleep(1 * (attempt + 1))
                else:
                    failed[0] = True
                    raise

    try:
        await asyncio.gather(*[worker(i, off, cnt) for i, (off, cnt) in enumerate(ranges)])

        if failed[0]:
            raise RuntimeError("One or more parallel workers failed after retries")

        # Final progress update at 100%
        if progress_callback:
            if asyncio.iscoroutinefunction(progress_callback):
                await progress_callback(file_size, file_size, *progress_args)

        # Assemble part files in order
        with open(out_path, "wb") as out:
            for tp in temp_paths:
                if not os.path.exists(tp):
                    raise FileNotFoundError(f"Part missing: {tp}")
                with open(tp, "rb") as inp:
                    shutil.copyfileobj(inp, out)

        actual_size = os.path.getsize(out_path) if os.path.exists(out_path) else 0
        if actual_size == 0:
            raise ValueError("Assembled file is empty")

        logging.info(
            f"Parallel download: {file_size/1048576:.1f} MB in {actual_workers} workers → {out_path}"
        )
        return out_path

    except Exception as e:
        logging.warning(f"Parallel download failed ({type(e).__name__}: {e}), falling back to serial")
        for p in [out_path] + temp_paths:
            try:
                if os.path.exists(p):
                    os.remove(p)
            except Exception:
                pass
        return await download_media_fast(client, message, file_name, progress_callback, progress_args)

    finally:
        for tp in temp_paths:
            try:
                if os.path.exists(tp):
                    os.remove(tp)
            except Exception:
                pass


async def download_media_fast(
    client: Client,
    message: Message,
    file_name,
    progress_callback=None,
    progress_args=()
):
    """Serial fallback downloader — used for small files and on parallel error."""
    retries = 5
    for i in range(retries):
        try:
            path = await client.download_media(
                message,
                file_name=file_name or "downloads/",
                progress=progress_callback if progress_callback else None,
                progress_args=progress_args
            )
            if path and os.path.exists(path) and os.path.getsize(path) == 0:
                logging.warning(f"Download returned empty file on attempt {i+1}: {path} — retrying")
                try:
                    os.remove(path)
                except Exception:
                    pass
                if i < retries - 1:
                    await asyncio.sleep(2 * (i + 1))
                    continue
                raise ValueError(f"File downloaded as empty after {retries} attempts: {path}")
            return path
        except (FloodWait, FloodPremiumWait) as e:
            logging.warning(f"FloodWait: Sleeping for {e.value} seconds")
            await asyncio.sleep(e.value)
        except (FileReferenceExpired, FileReferenceInvalid) as e:
            logging.error(f"File reference expired on attempt {i+1}: {e}")
            raise
        except Exception as e:
            if i == retries - 1:
                raise e
            logging.error(f"Download attempt {i+1} failed: {e}. Retrying...")
            await asyncio.sleep(2 * (i + 1))


def truncate_caption(caption, max_length=1024):
    if not caption:
        return ""
    caption_str = str(caption)
    if len(caption_str) <= max_length:
        return caption_str
    return caption_str[:max_length - 3] + "..."


def check_file_size(file_path, max_size_mib=2000):
    if not os.path.exists(file_path):
        raise ValueError(f"File not found: {file_path}")
    file_size_bytes = os.path.getsize(file_path)
    file_size_mib = file_size_bytes / (1024 * 1024)
    if file_size_mib > max_size_mib:
        raise ValueError(f"File size ({file_size_mib:.2f} MiB) exceeds Telegram limit of {max_size_mib} MiB")
    if file_size_bytes == 0:
        raise ValueError(f"File is empty: {file_path}")
    return file_size_bytes


async def _send_with_floodwait(coro_fn, max_retries=3):
    for attempt in range(max_retries):
        try:
            return await coro_fn()
        except (FloodWait, FloodPremiumWait) as e:
            wait = e.value
            logging.warning(f"Upload FloodWait {wait}s (attempt {attempt+1}/{max_retries})")
            if attempt < max_retries - 1:
                await asyncio.sleep(min(wait, 60))
            else:
                raise


async def upload_media_fast(
    client: Client,
    chat_id,
    file_path,
    caption="",
    thumb=None,
    file_name=None,
    has_spoiler=None,
    progress_callback=None,
    progress_args=(),
    **kwargs
):
    """
    Upload with FloodWait retry and PhotoExtInvalid fallback.

    Note: pyrotgfork's save_file() already uses 3 sessions × 4 workers (12 parallel
    streams) for files > 10 MB — no additional parallelism needed here.
    """
    safe_caption = truncate_caption(caption)
    file_path_lower = file_path.lower()

    try:
        check_file_size(file_path)
    except ValueError as e:
        logging.error(f"File validation error: {e}")
        return None

    upload_kwargs = {
        "caption": safe_caption,
        "progress": progress_callback,
        "progress_args": progress_args,
    }

    try:
        if not client.is_connected:
            try:
                await client.start()
            except Exception as start_err:
                if "already" not in str(start_err).lower():
                    raise

        if isinstance(chat_id, str) and chat_id.lower() == "me":
            target_id = "me"
        else:
            try:
                target_id = int(chat_id)
            except (ValueError, TypeError):
                target_id = chat_id

        if file_path_lower.endswith((".mp4", ".mkv", ".mov", ".avi")):
            upload_kwargs.update(kwargs)
            upload_kwargs["thumb"] = thumb
            if file_name:
                upload_kwargs["file_name"] = file_name
            if has_spoiler is not None:
                upload_kwargs["has_spoiler"] = has_spoiler
            if file_path_lower.endswith(".gif"):
                return await _send_with_floodwait(
                    lambda: client.send_animation(target_id, file_path, **upload_kwargs)
                )
            return await _send_with_floodwait(
                lambda: client.send_video(target_id, file_path, supports_streaming=True, **upload_kwargs)
            )

        elif file_path_lower.endswith((".mp3", ".m4a", ".ogg", ".wav")):
            upload_kwargs["duration"] = kwargs.get("duration", 0)
            if file_path_lower.endswith((".ogg", ".wav")):
                return await _send_with_floodwait(
                    lambda: client.send_voice(target_id, file_path, **upload_kwargs)
                )
            upload_kwargs["thumb"] = thumb
            if file_name:
                upload_kwargs["file_name"] = file_name
            return await _send_with_floodwait(
                lambda: client.send_audio(target_id, file_path, **upload_kwargs)
            )

        elif file_path_lower.endswith((".jpg", ".jpeg", ".png", ".webp")):
            try:
                if has_spoiler is not None:
                    upload_kwargs["has_spoiler"] = has_spoiler
                return await _send_with_floodwait(
                    lambda: client.send_photo(target_id, file_path, **upload_kwargs)
                )
            except PhotoExtInvalid:
                logging.warning(f"PhotoExtInvalid for {file_path} — falling back to send_document")
                doc_kwargs = dict(upload_kwargs)
                doc_kwargs.pop("has_spoiler", None)
                doc_kwargs["thumb"] = thumb
                if file_name:
                    doc_kwargs["file_name"] = file_name
                return await _send_with_floodwait(
                    lambda: client.send_document(target_id, file_path, **doc_kwargs)
                )

        upload_kwargs["thumb"] = thumb
        if file_name:
            upload_kwargs["file_name"] = file_name
        return await _send_with_floodwait(
            lambda: client.send_document(target_id, file_path, **upload_kwargs)
        )

    except (AuthKeyUnregistered, AuthKeyUnregistered401, SessionRevoked) as e:
        logging.error(f"AuthKeyUnregistered during transfer for chat {chat_id}: {e}")
        raise
    except Exception:
        logging.exception("Upload Error:")
        raise
