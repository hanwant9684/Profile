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
_MIN_PARALLEL_SIZE = 2 * 1024 * 1024  # 2 MB


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
    num_workers: int = 8,
    extra_clients: list = None,
    progress_callback=None,
    progress_args=()
):
    """
    FastTelethon-style parallel downloader using N independent raw MTProto sessions.

    How it works (and why it beats the old single-session approach):
      pyrotgfork's client.get_file() routes every chunk through ONE shared
      media session (one TCP connection to Telegram's file DC) protected by a
      get_file_semaphore that limits in-flight requests to max_concurrent_transmissions.
      No matter how many asyncio workers you spin up, they all queue through that
      single pipe — and Telegram throttles each TCP connection to ~4 MB/s.

      This function instead creates N independent raw pyrogram.session.Session
      objects, each establishing its OWN TCP connection to Telegram's file DC
      with its OWN auth key export.  Workers bypass get_file_semaphore entirely
      and call session.invoke(GetFile(...)) directly.  N sessions = N independent
      bandwidth streams from Telegram's DC → true N× throughput.

      This is the same technique used by FastTelethon on the Telethon library.
      With 8 sessions on a fast VPS near Telegram's DC you can realistically
      reach 15-30 MB/s on large files.

    Auth strategy:
      One new MTProto auth key is created for the file DC (via DH handshake).
      That key is authorised with ExportAuthorization/ImportAuthorization on the
      first session, then reused for all other sessions.  MTProto allows multiple
      concurrent connections sharing the same auth key — each Session object
      generates its own unique 8-byte session_id (os.urandom(8)) so Telegram
      treats them as separate connections.

    Falls back to serial download on any unrecoverable error.

    Upload note:
      pyrotgfork's save_file() already uses 3 sessions × 4 workers (12 streams)
      for files > 10 MB — upload is already optimally parallelised in the library.
    """
    from pyrogram.file_id import FileId, FileType
    from pyrogram.session import Session
    from pyrogram.session.auth import Auth
    import pyrogram.raw as raw

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

    # Build the Telegram file location object for GetFile RPC calls.
    file_type = file_id_obj.file_type
    if file_type == FileType.PHOTO:
        location = raw.types.InputPhotoFileLocation(
            id=file_id_obj.media_id,
            access_hash=file_id_obj.access_hash,
            file_reference=file_id_obj.file_reference,
            thumb_size=file_id_obj.thumbnail_size or ""
        )
    else:
        location = raw.types.InputDocumentFileLocation(
            id=file_id_obj.media_id,
            access_hash=file_id_obj.access_hash,
            file_reference=file_id_obj.file_reference,
            thumb_size=""
        )

    dc_id = file_id_obj.dc_id
    test_mode = await client.storage.test_mode()
    client_dc_id = await client.storage.dc_id()

    total_chunks = (file_size + _PYROGRAM_CHUNK_SIZE - 1) // _PYROGRAM_CHUNK_SIZE
    actual_sessions = min(num_workers, total_chunks)

    # Distribute chunks evenly across sessions — each session downloads a
    # contiguous range sequentially (no cross-session locking needed).
    base, rem = divmod(total_chunks, actual_sessions)
    ranges = []
    pos = 0
    for i in range(actual_sessions):
        cnt = base + (1 if i < rem else 0)
        ranges.append((pos, cnt))
        pos += cnt

    temp_paths = [os.path.join("downloads", f"dl_{uid}_p{i}.tmp") for i in range(actual_sessions)]
    downloaded = [0] * actual_sessions
    last_report = [time.time()]
    failed = [False]
    raw_sessions = []

    try:
        # ── Auth key ────────────────────────────────────────────────────────────
        # Create ONE new MTProto auth key for the file DC (single DH handshake).
        # All sessions will share this key — each Session gets its own random
        # session_id (os.urandom(8)) so Telegram treats them as separate connections.
        if dc_id != client_dc_id:
            auth_key = await Auth(client, dc_id, test_mode).create()
        else:
            auth_key = await client.storage.auth_key()

        # ── Session 0 + authorization (BEFORE starting the rest) ────────────────
        # CRITICAL ORDER: session 0 must connect and ImportAuthorization BEFORE
        # sessions 1-N start.  If sessions 1-N connect while the auth_key is still
        # unlinked (no user import done yet), the DC sends transport error 404
        # ("auth key not found") and kills those connections immediately.
        # After ImportAuthorization, the auth_key is permanently recognized on the
        # DC — all subsequent sessions with the same key are accepted.
        session_0 = Session(client, dc_id, auth_key, test_mode, is_media=True)
        await session_0.start()
        raw_sessions.append(session_0)

        if dc_id != client_dc_id:
            auth_imported = False
            for attempt in range(3):
                try:
                    exported = await client.invoke(
                        raw.functions.auth.ExportAuthorization(dc_id=dc_id)
                    )
                    await session_0.invoke(
                        raw.functions.auth.ImportAuthorization(
                            id=exported.id,
                            bytes=exported.bytes
                        )
                    )
                    auth_imported = True
                    break
                except Exception as e:
                    logging.warning(f"Auth export attempt {attempt + 1}/3 failed: {e}")
                    await asyncio.sleep(1)
            if not auth_imported:
                raise RuntimeError("Failed to authorize sessions with Telegram file DC")

        # ── Sessions 1-N (started AFTER auth is established) ────────────────────
        # auth_key is now linked to the user on the file DC, so these sessions
        # are accepted immediately when they connect.
        for i in range(1, actual_sessions):
            session = Session(client, dc_id, auth_key, test_mode, is_media=True)
            await session.start()
            raw_sessions.append(session)

        # ── Per-session download worker ──────────────────────────────────────────
        async def session_worker(idx, chunk_offset, chunk_count):
            """Download a sequential range of chunks via a dedicated raw Session.

            Direct session.invoke(GetFile) bypasses get_file_semaphore entirely.
            No contention with other workers — each session has its own TCP pipe.

            Reconnection: if Telegram drops the TCP connection mid-transfer
            (TCPTransport closed, network hiccup, DC rebalance, etc.) the session
            is stopped and a NEW Session is created with the same auth_key — no
            new ExportAuthorization needed because the key is already linked to
            the user on that DC.  Download resumes from the failed chunk.
            """
            reconnects = 0
            chunk_idx = 0
            with open(temp_paths[idx], "wb") as fh:
                while chunk_idx < chunk_count:
                    if failed[0]:
                        return
                    offset_bytes = (chunk_offset + chunk_idx) * _PYROGRAM_CHUNK_SIZE
                    session = raw_sessions[idx]
                    try:
                        r = await session.invoke(
                            raw.functions.upload.GetFile(
                                location=location,
                                offset=offset_bytes,
                                limit=_PYROGRAM_CHUNK_SIZE
                            )
                        )
                        data = getattr(r, "bytes", b"")
                        if not data and chunk_idx < chunk_count - 1:
                            raise ValueError(f"Empty chunk at byte offset {offset_bytes}")
                        fh.write(data)
                        downloaded[idx] += len(data)
                        if progress_callback and not failed[0]:
                            now = time.time()
                            if now - last_report[0] >= 2:
                                last_report[0] = now
                                total_dl = min(sum(downloaded), file_size)
                                if asyncio.iscoroutinefunction(progress_callback):
                                    await progress_callback(total_dl, file_size, *progress_args)
                        chunk_idx += 1
                        reconnects = 0  # reset reconnect counter on any success

                    except (FileReferenceExpired, FileReferenceInvalid):
                        failed[0] = True
                        raise

                    except (FloodWait, FloodPremiumWait) as e:
                        logging.warning(f"Session {idx} FloodWait {e.value}s on chunk {chunk_idx}")
                        await asyncio.sleep(e.value)
                        # retry same chunk_idx

                    except Exception as e:
                        if str(e) == "StopProcess":
                            failed[0] = True
                            raise

                        reconnects += 1
                        if reconnects > 3:
                            failed[0] = True
                            raise

                        # TCP dropped or session closed — rebuild the connection.
                        # auth_key is already linked to the user on this DC so
                        # no ExportAuthorization needed for the replacement session.
                        logging.warning(
                            f"Session {idx} chunk {chunk_idx} dropped (reconnect {reconnects}/3): {e}"
                        )
                        try:
                            await session.stop()
                        except Exception:
                            pass
                        new_session = Session(client, dc_id, auth_key, test_mode, is_media=True)
                        await new_session.start()
                        raw_sessions[idx] = new_session
                        await asyncio.sleep(0.5 * reconnects)
                        # retry same chunk_idx with new session

        # ── Run all sessions in parallel ─────────────────────────────────────────
        await asyncio.gather(*[
            session_worker(i, off, cnt) for i, (off, cnt) in enumerate(ranges)
        ])

        if failed[0]:
            raise RuntimeError("One or more download sessions failed")

        # Final progress at 100%
        if progress_callback and asyncio.iscoroutinefunction(progress_callback):
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
            f"Fast-session download: {file_size/1048576:.1f} MB in {actual_sessions} sessions → {out_path}"
        )
        return out_path

    except (FileReferenceExpired, FileReferenceInvalid):
        for p in [out_path] + temp_paths:
            try:
                if os.path.exists(p):
                    os.remove(p)
            except Exception:
                pass
        raise

    except Exception as e:
        if str(e) == "StopProcess":
            for p in [out_path] + temp_paths:
                try:
                    if os.path.exists(p):
                        os.remove(p)
                except Exception:
                    pass
            raise

        logging.warning(f"Fast-session download failed ({type(e).__name__}: {e}), falling back to serial")
        for p in [out_path] + temp_paths:
            try:
                if os.path.exists(p):
                    os.remove(p)
            except Exception:
                pass
        return await download_media_fast(client, message, file_name, progress_callback, progress_args)

    finally:
        for session in raw_sessions:
            try:
                await session.stop()
            except Exception:
                pass
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
