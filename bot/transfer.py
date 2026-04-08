import os
import time
import uuid
import shutil
import asyncio
import logging
import mimetypes
import sqlite3
from pyrogram import Client
from pyrogram.types import Message
from pyrogram.errors import FloodWait, FloodPremiumWait, AuthKeyUnregistered, SessionRevoked
from pyrogram.errors.exceptions.unauthorized_401 import AuthKeyUnregistered as AuthKeyUnregistered401
from pyrogram.errors.exceptions.bad_request_400 import (
    PhotoExtInvalid,
    PhotoInvalidDimensions,
    FileReferenceExpired,
    FileReferenceInvalid,
    AuthBytesInvalid,
)

# Must match pyrotgfork's internal chunk size (client.py line 1153)
_PYROGRAM_CHUNK_SIZE = 1024 * 1024  # 1 MB

# Only use parallel download for files larger than this — smaller files don't benefit
_MIN_PARALLEL_SIZE = 5 * 1024 * 1024  # 5 MB


class _FileRefSwallowedByPyrogram(Exception):
    """Surrogate for FileReferenceExpired raised when pyrotgfork's get_file()
    silently swallows the error.

    Root cause (pyrotgfork client.py lines 1319-1322):
        except pyrogram.StopTransmission:
            raise
        except Exception as e:
            log.exception(e)   # logs but does NOT re-raise

    When FileReferenceExpired hits that catch, the async generator exits without
    yielding a single byte.  Our workers see no exception — they just write empty
    temp files.  We detect the empty file and raise this sentinel so the refresh
    path in handlers.py fires correctly instead of retrying with the same stale
    reference five times.
    """
    pass


async def download_media_parallel(
    client: Client,
    message: Message,
    file_name=None,
    num_workers: int = 2,
    progress_callback=None,
    progress_args=(),
    extra_clients=None,
):
    """
    Parallel chunk downloader — overcomes pyrotgfork's ~4 MB/s serial ceiling.

    Background:
      pyrotgfork fetches file chunks ONE at a time (1 MB each).  Each GetFile
      RPC takes ~250-300 ms, giving a hard ceiling of ~4 MB/s per TCP connection
      to the file DC regardless of network speed.

      This function splits the file into `num_workers` byte ranges and fetches
      them CONCURRENTLY.  When `extra_clients` are supplied (one per additional
      worker), each worker uses its own Client instance → its own TCP socket to
      the file DC → its own independent ~4 MB/s stream.  Two workers on two
      separate clients gives ~2× throughput instead of sharing one socket.

      Without extra_clients all workers share the primary client's single media
      session; multiple in-flight GetFile RPCs on one connection still yield some
      benefit via MTProto's msg_id multiplexing, but the gain is smaller.

      Each user already has their own Client → own media_sessions dict → own
      TCP socket, so there is zero inter-user contention.

      Falls back silently to standard serial download on any unrecoverable error.

    extra_clients:
      Optional list of additional pre-started Client objects.  Worker 0 uses
      `client`; worker i uses extra_clients[i-1] if available, else falls back
      to `client`.  Each extra client should be created with
      max_concurrent_transmissions=1 since it serves exactly one worker.

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

    # Build per-worker client list.
    # Worker 0 → primary client; worker i → extra_clients[i-1] when available.
    # This gives each worker its own TCP connection to the file DC, bypassing the
    # single-socket bottleneck of sharing one media session across all workers.
    _all_clients = [client] + list(extra_clients or [])

    async def worker(idx, chunk_offset, chunk_limit):
        """Fetch one byte-range with per-error retry policy.

        Error classification:
          FATAL  — session/reference is dead; retrying floods logs.  Set failed[0], raise.
          FLOOD  — Telegram rate limit: sleep then RESUME from last written chunk.
                   Never restart the whole range — that is the root cause of the
                   70-80% speed-drop bug (worker re-downloads chunks it already wrote).
          RETRY  — transient network error: restart the range from the beginning.
        """
        # Each worker uses its own Client for an independent TCP socket to the file DC.
        _client = _all_clients[idx % len(_all_clients)]

        # _chunks_done tracks how many 1-MB chunks were successfully written to disk.
        # On FloodWait we advance offset/limit past these chunks and append to the
        # existing partial file instead of truncating and re-downloading from scratch.
        _chunks_done = 0

        for attempt in range(3):
            # Exit immediately if another worker already failed fatally.
            if failed[0]:
                raise RuntimeError("Aborting — another worker already failed")

            # Compute resume parameters.
            # FloodWait retry: _chunks_done > 0, so we skip ahead in the range.
            # Fresh start / generic retry: _chunks_done == 0, full range.
            _resume_offset = chunk_offset + _chunks_done
            _resume_limit  = chunk_limit  - _chunks_done

            if _resume_limit <= 0:
                return  # All chunks already written — nothing left to do.

            # Append to partial file on FloodWait resume; truncate on fresh start.
            _file_mode = "ab" if _chunks_done > 0 else "wb"
            if _chunks_done == 0:
                open(temp_paths[idx], "wb").close()  # create / truncate

            _chunks_before = _chunks_done  # to detect silent empty-response

            try:
                with open(temp_paths[idx], _file_mode) as fh:
                    async for chunk in _client.get_file(
                        file_id_obj, file_size,
                        limit=_resume_limit,
                        offset=_resume_offset
                    ):
                        fh.write(chunk)
                        _chunks_done += 1
                        downloaded[idx] += len(chunk)
                        if progress_callback and not failed[0]:
                            now = time.time()
                            if now - last_report[0] >= 2:
                                last_report[0] = now
                                total_dl = min(sum(downloaded), file_size)
                                if asyncio.iscoroutinefunction(progress_callback):
                                    await progress_callback(total_dl, file_size, *progress_args)

                # ── Detect pyrotgfork silently swallowing FileReferenceExpired ──────
                # get_file() catches ALL exceptions internally and logs without
                # re-raising (client.py lines 1319-1322).  When FileReferenceExpired
                # hits that handler the async generator exits without yielding a
                # single byte.  Detect this by checking that this attempt produced
                # ZERO new chunks even though chunks were expected.
                _chunks_got = _chunks_done - _chunks_before
                if _resume_limit > 0 and _chunks_got == 0:
                    failed[0] = True
                    raise _FileRefSwallowedByPyrogram(
                        f"Worker {idx}: get_file() yielded 0 chunks for a remaining "
                        f"{_resume_limit}-chunk range — pyrotgfork swallowed an error "
                        f"(likely FileReferenceExpired)"
                    )
                return  # success

            except (FloodWait, FloodPremiumWait) as e:
                # ── FLOOD: do NOT restart the range. ───────────────────────────────
                # The old code set downloaded[idx]=0 and re-opened the file with "wb",
                # which truncated all written data and restarted from chunk_offset.
                # That is the root cause of the 70-80% speed-drop: one worker finishes
                # its half, the other worker hits a FloodWait, resets, and has to
                # re-fetch its entire range from scratch while only one worker runs.
                #
                # Fix: honour the wait, then resume from _chunks_done (append mode).
                # downloaded[idx] is NOT reset — the bytes are still on disk.
                wait = e.value
                logging.warning(
                    f"Parallel worker {idx} FloodWait {wait}s — "
                    f"resuming from chunk {_chunks_done}/{chunk_limit} (attempt {attempt+1}/3)"
                )
                if attempt < 2:
                    await asyncio.sleep(wait)
                    # Loop continues with _chunks_done intact → resumes mid-range.
                else:
                    failed[0] = True
                    raise

            except (FileReferenceExpired, FileReferenceInvalid, _FileRefSwallowedByPyrogram):
                # FATAL: file reference is stale (or pyrotgfork swallowed it).
                failed[0] = True
                raise

            except AuthBytesInvalid:
                # FATAL: exported auth for the file DC is invalid.
                # Clear the cached media session so the next call creates a fresh one.
                failed[0] = True
                try:
                    _client.media_sessions.clear()
                except Exception:
                    pass
                raise

            except sqlite3.ProgrammingError:
                # FATAL: client's in-memory SQLite storage was closed (session evicted).
                failed[0] = True
                logging.warning(f"Parallel worker {idx}: client SQLite storage closed mid-download")
                raise

            except Exception as e:
                error_str = str(e)

                # FATAL: intentional user cancellation.
                if error_str == "StopProcess":
                    failed[0] = True
                    raise

                # FATAL: session connection torn down (NoneType iterable error).
                if "NoneType" in error_str and (
                    "iterable" in error_str or "* must be" in error_str
                ):
                    failed[0] = True
                    logging.warning(f"Parallel worker {idx}: session connection torn down mid-transfer")
                    raise

                # FATAL: TCP transport explicitly closed.
                if "TCPTransport" in error_str and "closed=True" in error_str:
                    failed[0] = True
                    logging.warning(f"Parallel worker {idx}: TCP transport closed mid-transfer")
                    raise

                # RETRY: generic transient error — restart the full range from scratch.
                # (Unlike FloodWait, these errors may have left the file in a
                # partially-corrupt state, so we truncate and re-download the range.)
                if attempt < 2:
                    logging.warning(f"Parallel worker {idx} error (attempt {attempt+1}/3): {e}, retrying from start")
                    _chunks_done = 0
                    downloaded[idx] = 0
                    await asyncio.sleep(1 * (attempt + 1))
                else:
                    failed[0] = True
                    raise

    def _is_session_fatal(exc: BaseException) -> bool:
        """Return True for errors caused by a dead/evicted session.

        These errors will recur on serial fallback with the same client, so we
        re-raise them directly instead of attempting a pointless serial retry.
        """
        if isinstance(exc, sqlite3.ProgrammingError):
            return True
        if isinstance(exc, AuthBytesInvalid):
            return True
        if isinstance(exc, OSError):
            s = str(exc)
            if "NoneType" in s and ("iterable" in s or "* must be" in s):
                return True
            if "TCPTransport" in s and "closed=True" in s:
                return True
        return False

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

    except (FileReferenceExpired, FileReferenceInvalid, _FileRefSwallowedByPyrogram):
        # Stale file reference (or pyrotgfork silently swallowed it) — re-raise
        # so handlers.py can re-fetch the message and get a fresh reference.
        for p in [out_path] + temp_paths:
            try:
                if os.path.exists(p):
                    os.remove(p)
            except Exception:
                pass
        raise

    except Exception as e:
        # User cancellation — re-raise immediately so handlers.py catches StopProcess.
        # Falling through to serial would call the progress callback again, causing
        # pyrotgfork to log a noisy ERROR before handlers.py ever sees the exception.
        if str(e) == "StopProcess":
            for p in [out_path] + temp_paths:
                try:
                    if os.path.exists(p):
                        os.remove(p)
                except Exception:
                    pass
            raise

        # Session-fatal errors: re-raise so handlers.py can reconnect the client
        # rather than attempting a serial download with the same dead session.
        if _is_session_fatal(e):
            logging.warning(f"Parallel download: session fatal error ({type(e).__name__}: {e}), re-raising to caller")
            for p in [out_path] + temp_paths:
                try:
                    if os.path.exists(p):
                        os.remove(p)
                except Exception:
                    pass
            raise

        # Transient / recoverable error — fall back to serial download.
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
                try:
                    os.remove(path)
                except Exception:
                    pass
                # pyrotgfork's get_file() silently swallows FileReferenceExpired
                # (client.py lines 1319-1322: `except Exception as e: log.exception(e)`
                # with no re-raise).  The result is an empty file regardless of how many
                # times we retry with the same stale reference.  Raise immediately so
                # handlers.py can re-fetch the message and get a fresh file reference.
                raise _FileRefSwallowedByPyrogram(
                    f"download_media returned empty file on attempt {i+1}: {path} "
                    f"(pyrotgfork swallowed FileReferenceExpired)"
                )
            return path
        except (FloodWait, FloodPremiumWait) as e:
            logging.warning(f"FloodWait: Sleeping for {e.value} seconds")
            await asyncio.sleep(e.value)
        except (FileReferenceExpired, FileReferenceInvalid, _FileRefSwallowedByPyrogram) as e:
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
                await asyncio.sleep(wait)
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
            except (PhotoExtInvalid, PhotoInvalidDimensions):
                logging.warning(f"Photo rejected (invalid ext or dimensions) for {file_path} — falling back to send_document")
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
