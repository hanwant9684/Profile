"""
Fast parallel Telethon downloader — segment-based iter_download approach.

Divides the file into N equal segments and runs N workers in parallel, each
calling client.iter_download() for its own segment.  Telethon's iter_download
handles all the internal details (request-size clamping, 4096-byte alignment,
DC switching, EOF detection) so we never touch GetFileRequest directly.

Public API:
    path = await download_file_fast(tl_client, message, out_path, progress_callback)

Raises on failure — callers handle the fallback.
"""

import asyncio
import logging
import math
import os
from pyrogram import StopTransmission

logger = logging.getLogger(__name__)

# Per-request size fed to iter_download's request_size param.
# Telethon automatically clamps this to a valid multiple of 4096, so we
# don't need to worry about alignment ourselves.
REQUEST_SIZE = 512 * 1024   # 512 KB
DEFAULT_WORKERS = 5


# ---------------------------------------------------------------------------
# File-size helper
# ---------------------------------------------------------------------------

def _get_file_size(message) -> int:
    """Return file size in bytes from a Telethon message, or 0 if unknown."""
    media = getattr(message, "media", None)
    if not media:
        return 0
    doc = getattr(media, "document", None)
    if doc:
        return getattr(doc, "size", 0) or 0
    photo = getattr(media, "photo", None)
    if photo:
        sizes = [s for s in photo.sizes if hasattr(s, "size")]
        return sizes[-1].size if sizes else 0
    return 0


# ---------------------------------------------------------------------------
# Single-segment worker
# ---------------------------------------------------------------------------

async def _download_segment(
    client,
    message,
    start_offset: int,
    chunk_size: int,
    n_chunks: int,
    out_path: str,
    file_size: int,
    downloaded_ref: list,
    progress_callback,
):
    """
    Download one contiguous file segment to *out_path*.

    Uses client.iter_download with:
      offset     = start byte of this segment
      stride     = chunk_size (sequential: each step advances by one chunk)
      limit      = n_chunks (stop after this many chunks)
      request_size = chunk_size
    """
    with open(out_path, "wb") as fh:
        async for chunk in client.iter_download(
            message,
            offset=start_offset,
            stride=chunk_size,       # sequential stride
            limit=n_chunks,          # stop after our segment
            request_size=chunk_size,
        ):
            data = bytes(chunk)      # chunk may be a memoryview
            fh.write(data)
            downloaded_ref[0] += len(data)
            if progress_callback and file_size:
                try:
                    await progress_callback(downloaded_ref[0], file_size)
                except StopTransmission:
                    raise
                except Exception:
                    pass


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

async def download_file_fast(
    client,
    message,
    out_path: str,
    progress_callback=None,
    workers: int = DEFAULT_WORKERS,
    request_size: int = REQUEST_SIZE,
) -> str:
    """
    Download *message*'s media to *out_path* using N parallel segment workers.

    Each worker calls client.iter_download() for a contiguous portion of the
    file, writing to a temporary part file.  When all workers finish the part
    files are concatenated in order into *out_path*.

    Raises on failure — no silent fallback here so callers can decide what
    to do (show the user an informative message, fall back to slow download,
    etc.).

    Args:
        client:            Connected TelegramClient.
        message:           Telethon Message with .media.
        out_path:          Destination file path.
        progress_callback: async (current_bytes, total_bytes) — optional.
        workers:           Number of parallel workers (default 5).
        request_size:      Bytes per individual request (default 512 KB).
                           Telethon clamps/aligns this automatically.
    Returns:
        *out_path* on success.
    """
    file_size = _get_file_size(message)
    if not file_size:
        raise ValueError("File size unknown — cannot split into parallel segments.")

    total_chunks = math.ceil(file_size / request_size)
    n_workers = min(workers, total_chunks)
    chunks_per_worker = math.ceil(total_chunks / n_workers)

    logger.info(
        "Fast-TL download: %.1f MB | %d chunks | %d workers | seg=%.1f MB → %s",
        file_size / 1_000_000,
        total_chunks,
        n_workers,
        (chunks_per_worker * request_size) / 1_000_000,
        out_path,
    )

    # Build segment specs: (start_offset_bytes, number_of_chunks)
    segments = []
    for i in range(n_workers):
        start_chunk = i * chunks_per_worker
        if start_chunk >= total_chunks:
            break
        n_chunks = min(chunks_per_worker, total_chunks - start_chunk)
        segments.append((start_chunk * request_size, n_chunks))

    os.makedirs(
        os.path.dirname(out_path) if os.path.dirname(out_path) else "downloads",
        exist_ok=True,
    )

    part_paths = [f"{out_path}.seg{i}" for i in range(len(segments))]
    downloaded_ref = [0]   # shared byte counter across workers

    tasks = [
        asyncio.ensure_future(_download_segment(
            client=client,
            message=message,
            start_offset=seg[0],
            chunk_size=request_size,
            n_chunks=seg[1],
            out_path=part_paths[i],
            file_size=file_size,
            downloaded_ref=downloaded_ref,
            progress_callback=progress_callback,
        ))
        for i, seg in enumerate(segments)
    ]
    try:
        # Run all segment workers in parallel; cancel all if any one fails or is cancelled
        try:
            await asyncio.gather(*tasks)
        except Exception:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

        # Concatenate segment files in order into the final output
        with open(out_path, "wb") as out_fh:
            for part_path in part_paths:
                with open(part_path, "rb") as part_fh:
                    while True:
                        buf = part_fh.read(4 * 1024 * 1024)  # 4 MB copy buffer
                        if not buf:
                            break
                        out_fh.write(buf)

        logger.info(
            "Fast-TL download complete: %s (%.1f MB)", out_path, file_size / 1_000_000
        )
        return out_path

    finally:
        # Always clean up part files, even on error
        for part_path in part_paths:
            try:
                if os.path.exists(part_path):
                    os.remove(part_path)
            except Exception:
                pass
