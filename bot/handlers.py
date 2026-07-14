import asyncio
import os
import re
import time
import logging
from collections import deque

import pyrogram
from pyrogram import filters, Client, enums
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, LinkPreviewOptions
from pyrogram import StopTransmission
from pyrogram.errors import (
    AuthKeyUnregistered, SessionRevoked, SessionExpired,
    AuthKeyInvalid, AuthKeyPermEmpty, UserDeactivated,
    AccessTokenExpired, AccessTokenInvalid,
)

from bot.link_utils import TG_LINK_HOST_RE, normalize_telegram_link
from bot.config import (
    app, API_ID, API_HASH,
    active_downloads, global_download_semaphore,
    cancel_flags, batch_sessions, login_states,
    SUPPORT_CHAT_LINK,
    telethon_clients, telethon_clients_last_used,
)
from bot.database import get_user, check_and_update_quota, get_setting, increment_quota, logout_user
from bot.transfer import (
    download_media, upload_media, truncate_caption, apply_caption_filter, get_user_bot,
    get_media_info, get_audio_tags,
    check_user_premium, split_file, split_video_ffmpeg,
    BOT_MAX_FILE_SIZE, PART_SAFE_SIZE,
)
from bot.log_channel import log_download


PREMIUM_MAX_FILE_SIZE = 4_000_000_000

_DOWNLOADABLE_TYPES = {
    enums.MessageMediaType.AUDIO,
    enums.MessageMediaType.DOCUMENT,
    enums.MessageMediaType.PHOTO,
    enums.MessageMediaType.STICKER,
    enums.MessageMediaType.VIDEO,
    enums.MessageMediaType.ANIMATION,
    enums.MessageMediaType.VOICE,
    enums.MessageMediaType.VIDEO_NOTE,
}


def _get_msg_file_size(m) -> int:
    for attr in ("video", "document", "audio", "voice", "video_note", "animation", "sticker"):
        obj = getattr(m, attr, None)
        if obj and getattr(obj, "file_size", None):
            return obj.file_size
    photo = getattr(m, "photo", None)
    if photo and photo.sizes:
        return photo.sizes[-1].file_size
    return 0


# User session cache (userbot clients, keyed by user_id)
user_clients: dict = {}
active_sessions: set = set()
_cleanup_started = False


async def _evict_user_session(user_id: int) -> None:
    """Disconnect and remove a stale phone session from all in-memory caches.

    Clears the Pyrogram userbot client (user_clients) and the Telethon client
    (telethon_clients) — both use the phone session string that just expired.
    Does NOT touch user_bots: the upload bot uses a separate bot token and is
    unaffected by phone-session expiry.

    Called immediately before logout_user() so the next request gets a fresh
    client rather than reusing the now-invalid cached one.
    """
    entry = user_clients.pop(user_id, None)
    if entry:
        try:
            await asyncio.wait_for(entry["client"].stop(), timeout=5)
        except Exception:
            pass
    # Also evict Telethon client — it uses the same phone session
    tl_entry = telethon_clients.pop(user_id, None)
    telethon_clients_last_used.pop(user_id, None)
    if tl_entry:
        try:
            await tl_entry["client"].disconnect()
        except Exception:
            pass



async def get_user_client(user_id: int, session_str: str) -> Client:
    global _cleanup_started
    now = time.time()

    if user_id in user_clients:
        entry = user_clients[user_id]
        if entry["client"].is_connected:
            entry["last_used"] = now
            return entry["client"]
        try:
            await asyncio.wait_for(entry["client"].stop(), timeout=10)
        except Exception:
            pass
        del user_clients[user_id]

    if len(user_clients) >= 10:
        idle = [
            (uid, d["last_used"]) for uid, d in user_clients.items()
            if uid not in active_sessions and uid not in batch_sessions
        ]
        if idle:
            oldest = min(idle, key=lambda x: x[1])[0]
            old = user_clients.pop(oldest)
            try:
                await asyncio.wait_for(old["client"].stop(), timeout=10)
            except Exception:
                pass

    client = Client(
        f"user_{user_id}",
        session_string=session_str,
        api_id=API_ID,
        api_hash=API_HASH,
        in_memory=True,
        sleep_threshold=30,
        no_updates=True,
        workers=100,
    )
    await asyncio.wait_for(client.start(), timeout=30)
    user_clients[user_id] = {"client": client, "last_used": now}

    if not _cleanup_started:
        asyncio.create_task(_cleanup_loop())
        _cleanup_started = True

    return client


async def _cleanup_loop():
    while True:
        await asyncio.sleep(120)
        now = time.time()

        stale = [
            uid for uid, d in list(user_clients.items())
            if uid not in active_sessions
            and uid not in batch_sessions
            and now - d["last_used"] > 600
        ]
        for uid in stale:
            entry = user_clients.pop(uid, None)
            if entry:
                try:
                    await asyncio.wait_for(entry["client"].stop(), timeout=10)
                except Exception:
                    pass

        expired_logins = [
            uid for uid, state in list(login_states.items())
            if now - state.get("timestamp", 0) > 300
        ]
        for uid in expired_logins:
            state = login_states.pop(uid, None)
            if state and "client" in state:
                try:
                    await state["client"].disconnect()
                except Exception:
                    pass
            try:
                if state and state.get("step") == "AWAITING_SETBOT_TOKEN":
                    await app.send_message(uid, "⚠️ /setbot session expired. Run /setbot again when ready.")
                else:
                    await app.send_message(uid, "⚠️ Login session expired due to inactivity. Run /login again.")
            except Exception:
                pass

        from bot.config import user_bots, user_bots_last_used
        from bot.transfer import stop_user_bot
        stale_bots = [
            uid for uid, last in list(user_bots_last_used.items())
            if uid not in active_sessions
            and uid not in batch_sessions
            and uid not in active_downloads
            and now - last > 3600
        ]
        for uid in stale_bots:
            if uid in active_downloads:  # re-check: may have entered since list was built
                continue
            user_bots_last_used.pop(uid, None)
            await stop_user_bot(uid)
            logging.info(f"Evicted idle user bot for user {uid}")

        stale_tl = [
            uid for uid, last in list(telethon_clients_last_used.items())
            if uid not in active_sessions
            and uid not in batch_sessions
            and uid not in active_downloads
            and now - last > 3600
        ]
        for uid in stale_tl:
            if uid in active_downloads:  # re-check: may have entered since list was built
                continue
            telethon_clients_last_used.pop(uid, None)
            entry = telethon_clients.pop(uid, None)
            if entry:
                try:
                    await entry["client"].disconnect()
                except Exception:
                    pass
            logging.info(f"Evicted idle Telethon client for user {uid}")


# Utilities
class _LazyStatus:
    def __init__(self, message):
        self._message = message
        self._sent = None

    @property
    def chat(self):
        return self._message.chat

    @property
    def id(self):
        return self._sent.id if self._sent else self._message.id

    async def edit_text(self, text, reply_markup=None):
        if self._sent is None:
            try:
                self._sent = await self._message.reply(text, reply_markup=reply_markup)
            except Exception as e:
                logging.debug(f"_LazyStatus.reply: {e}")
        else:
            try:
                await self._sent.edit_text(text, reply_markup=reply_markup)
            except Exception as e:
                if "MESSAGE_NOT_MODIFIED" not in str(e):
                    logging.debug(f"_LazyStatus.edit_text: {e}")

    async def delete(self):
        if self._sent:
            try:
                await self._sent.delete()
            except Exception:
                pass


async def update_status(msg, text: str, reply_markup=None):
    if not msg:
        return
    try:
        await msg.edit_text(text, reply_markup=reply_markup)
    except Exception as e:
        if "MESSAGE_NOT_MODIFIED" not in str(e):
            logging.debug(f"update_status: {e}")


def _fmt_size(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def _fmt_time(seconds: float) -> str:
    if seconds <= 0:
        return "0s"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}h {m}m {s}s" if h else (f"{m}m {s}s" if m else f"{s}s")


async def progress_bar(current: int, total: int, msg, label: str):
    if total == 0:
        return
    if msg.chat.id in cancel_flags:
        raise StopTransmission

    if not hasattr(progress_bar, "_data"):
        progress_bar._data = {}

    now = time.time()
    mid = msg.id
    data = progress_bar._data.setdefault(
        mid, {"start": now, "last": 0, "samples": deque(maxlen=30)}
    )
    data["samples"].append((now, current))

    if now - data["last"] < 5 and current < total:
        return

    pct = current * 100 / total
    bar = "█" * int(pct / 5) + "░" * (20 - int(pct / 5))

    speed = 0.0
    if len(data["samples"]) >= 2:
        t0, b0 = data["samples"][0]
        dt, db = now - t0, current - b0
        if dt > 0 and db > 0:
            speed = db / dt
    if not speed:
        elapsed = now - data["start"]
        speed = current / elapsed if elapsed > 0 else 0

    eta = (total - current) / speed if speed > 0 else 0

    text = (
        f"**{label}**\n"
        f"`[{bar}]` {pct:.1f}%\n"
        f"⚡ {_fmt_size(speed)}/s  ⏳ {_fmt_time(eta)}\n"
        f"📦 {_fmt_size(current)} / {_fmt_size(total)}"
    )

    if current >= total:
        progress_bar._data.pop(mid, None)
    else:
        data["last"] = now

    await update_status(msg, text)


# Force subscribe check
async def verify_force_sub(client: Client, user_id: int):
    setting = await get_setting("force_sub_channel")
    if not setting or not setting.get("value"):
        return True, None

    channel = setting["value"]
    if not channel.startswith("@") and not channel.startswith("-100"):
        channel = f"@{channel}"

    try:
        member = await client.get_chat_member(channel, user_id)
        if member.status in (enums.ChatMemberStatus.LEFT, enums.ChatMemberStatus.BANNED):
            return False, channel
        return True, None
    except pyrogram.errors.UserNotParticipant:
        return False, channel
    except Exception as e:
        logging.error(f"Force sub check error: {e}")
        return True, None


# Link parsing
def _parse_story_link(link: str):
    link_clean = normalize_telegram_link(re.sub(r"\?.*$", "", link).rstrip("/"))

    m = re.fullmatch(r"https://t\.me/c/(\d+)/s/(\d+)", link_clean)
    if m:
        return int("-100" + m.group(1)), int(m.group(2)), True

    m = re.fullmatch(r"https://t\.me/([^/+][^/]+)/s/(\d+)", link_clean)
    if m:
        return m.group(1), int(m.group(2)), False

    return None


def _parse_bot_start_link(link: str):
    link = normalize_telegram_link(link)
    m = re.match(r"https://t\.me/([^/?#]+)\?start=([^&\s]+)", link)
    if m:
        return m.group(1), m.group(2)
    return None


async def _bot_start_collect_media(user_client, username, after_id):
    found = []
    async for m in user_client.get_chat_history(username, limit=10):
        if m.id <= after_id:
            break
        if getattr(m, "media", None):
            found.append(m)
    return found



def _parse_link(link: str):
    link = normalize_telegram_link(link)
    comment_id = None
    comment_match = re.search(r"[?&]comment=(\d+)", link)
    if comment_match:
        comment_id = int(comment_match.group(1))

    thread_id = None
    thread_match = re.search(r"[?&]thread=(\d+)", link)
    if thread_match:
        thread_id = int(thread_match.group(1))

    link = re.sub(r"\?.*$", "", link).rstrip("/")

    m = re.fullmatch(r"https://t\.me/c/(\d+)/(\d+)", link)
    if m:
        return int("-100" + m.group(1)), int(m.group(2)), True, comment_id, thread_id, False

    m = re.fullmatch(r"https://t\.me/c/(\d+)/\d+/(\d+)", link)
    if m:
        return int("-100" + m.group(1)), int(m.group(2)), True, comment_id, thread_id, True

    m = re.fullmatch(r"https://t\.me/([^/+][^/]*)/(\d+)", link)
    if m:
        return m.group(1), int(m.group(2)), False, comment_id, thread_id, False

    m = re.fullmatch(r"https://t\.me/([^/+][^/]*)/(\d+)/(\d+)", link)
    if m:
        return m.group(1), int(m.group(3)), False, comment_id, thread_id, True

    return None


# ---------------------------------------------------------------------------
# Telethon engine helpers
# ---------------------------------------------------------------------------

async def get_telethon_client(user_id: int, session_str: str):
    """Get or create a cached Telethon client for a user (premium only)."""
    import time as _time
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    now = _time.time()

    if user_id in telethon_clients:
        entry = telethon_clients[user_id]
        try:
            if entry["client"].is_connected():
                entry["last_used"] = now
                telethon_clients_last_used[user_id] = now
                return entry["client"]
        except Exception:
            pass
        # Stale — evict
        try:
            await entry["client"].disconnect()
        except Exception:
            pass
        telethon_clients.pop(user_id, None)
        telethon_clients_last_used.pop(user_id, None)

    tl_client = TelegramClient(
        StringSession(session_str),
        int(API_ID),
        str(API_HASH),
        connection_retries=3,
    )
    await tl_client.connect()
    if not await tl_client.is_user_authorized():
        await tl_client.disconnect()
        raise RuntimeError("Telethon session is no longer authorized. Please /tlogin again.")

    telethon_clients[user_id] = {"client": tl_client, "last_used": now}
    telethon_clients_last_used[user_id] = now
    logging.info(f"Started Telethon client for user {user_id}")
    return tl_client


async def _tl_entity(chat_id):
    """Convert a Pyrogram-style chat_id to a Telethon-compatible peer."""
    if isinstance(chat_id, int) and chat_id < 0:
        chat_id_abs = abs(chat_id)
        s = str(chat_id_abs)
        if s.startswith("100") and len(s) > 3:
            from telethon.tl.types import PeerChannel
            return PeerChannel(int(s[3:]))
        else:
            from telethon.tl.types import PeerChat
            return PeerChat(chat_id_abs)
    return chat_id  # username string or positive int user ID


async def _handle_telethon_download(
    client, user_id, user, link, chat_id, msg_id,
    is_private, is_bot_dm, is_bot_start,
    comment_id, thread_id,
    _ltype, _username, _cap_filters, _cap_append,
    status, skip_quota,
    status_msg_override=None,
):
    """Download via Telethon (faster parallel transfers), then upload via Pyrogram user_bot."""
    import os as _os

    if is_bot_start:
        await update_status(
            status,
            "❌ Bot start links are not supported with the Telethon engine.\n"
            "Switch to Pyrogram with `/setengine pyrogram` and try again."
        )
        return None

    tl_session = user.get("telethon_session_string")
    if not tl_session:
        await update_status(status, "❌ No Telethon session found. Use /tlogin to connect first.")
        return None

    try:
        tl_client = await get_telethon_client(user_id, tl_session)
    except Exception as e:
        logging.warning(f"Telethon client error for user {user_id}: {e}")
        await update_status(status, f"❌ Could not start Telethon session: {e}\n\nUse /tlogin to reconnect.")
        return None

    entity = await _tl_entity(chat_id)

    # Fetch the target message
    try:
        await update_status(status, "🔍 Fetching message (Telethon)...")
        fetched = await tl_client.get_messages(entity, ids=[msg_id])
        msg = fetched[0] if fetched else None
        if not msg:
            await update_status(status, "❌ Message not found via Telethon.")
            return None
    except Exception as e:
        logging.error(f"Telethon get_messages error user={user_id}: {e}")
        await update_status(status, f"❌ Could not fetch message via Telethon: {e}")
        return None

    # Resolve comment override
    if comment_id is not None:
        try:
            disc = await tl_client.get_messages(entity, ids=[comment_id])
            disc_msg = disc[0] if disc else None
            if disc_msg and disc_msg.media:
                msg = disc_msg
                msg_id = msg.id
        except Exception as e:
            logging.debug(f"Telethon comment fetch failed (comment={comment_id}): {e}")

    # Collect media group
    grouped_id = getattr(msg, "grouped_id", None)
    if grouped_id:
        try:
            id_range = list(range(max(1, msg_id - 9), msg_id + 10))
            nearby = await tl_client.get_messages(entity, ids=id_range)
            messages = sorted(
                [m for m in nearby if m and getattr(m, "grouped_id", None) == grouped_id and m.media],
                key=lambda m: m.id,
            )
            if not messages:
                messages = [msg]
        except Exception:
            messages = [msg]
    else:
        messages = [msg]

    if not msg.media and not getattr(msg, "message", None):
        await update_status(status, "❌ No downloadable content found at this link.")
        return None

    # Get upload client (Pyrogram user_bot — same as Pyrogram path)
    is_premium_user = user.get("role") in ("premium", "admin", "owner")
    try:
        user_bot = await get_user_bot(user_id)
    except (AccessTokenExpired, AccessTokenInvalid):
        await update_status(status, "❌ Your upload bot token is invalid. Use /setbot to register a new one.")
        return None

    if user_bot is None and is_premium_user:
        await update_status(
            status,
            "❌ **Upload bot not configured.**\n\n"
            "Telethon handles the *download* — your registered bot handles the *upload*.\n"
            "Run /setbot first, then try again."
        )
        return None

    upload_client = user_bot if user_bot is not None else client
    active_sessions.add(user_id)

    # --- Text-only ---
    if not msg.media:
        text_body = getattr(msg, "message", "") or ""
        try:
            await upload_client.send_message(user_id, text_body or "—")
            if not skip_quota and user.get("role", "free") == "free":
                await increment_quota(user_id)
            if not status_msg_override:
                try:
                    await status.delete()
                except Exception:
                    pass
        except Exception as e:
            await update_status(status, f"❌ Failed to forward text: {e}")
        log_download(user_id, _username, link, _ltype, True)
        return msg

    # --- Album ---
    if len(messages) > 1:
        await update_status(status, f"📥 Downloading album ({len(messages)} files)...")

        async def _tl_album_progress(current, total):
            try:
                await progress_bar(current, total, status, "📥 Downloading album")
            except StopTransmission:
                raise
            except Exception:
                pass

        async def _dl_one_tl(m):
            try:
                from bot.fasttelethon import download_file_fast
                _ext = (getattr(m.file, "ext", "") or "") if m.file else ""
                _out = f"downloads/{user_id}_{m.id}{_ext}"
                return await download_file_fast(tl_client, m, _out, _tl_album_progress)
            except StopTransmission:
                raise
            except Exception as dl_e:
                logging.warning(f"Telethon album item download failed: {dl_e}")
                return None

        results = await asyncio.gather(*[_dl_one_tl(m) for m in messages], return_exceptions=True)
        # Propagate cancellation — StopTransmission means /cancel was pressed mid-album
        for result in results:
            if isinstance(result, StopTransmission):
                cancel_flags.discard(user_id)
                await update_status(status, "🛑 Cancelled.")
                return None
        paths = []
        valid_pairs = []
        for m, result in zip(messages, results):
            if isinstance(result, Exception) or not result:
                logging.warning(f"Telethon album item failed: {result}")
                continue
            paths.append(result)
            valid_pairs.append((m, result))

        if not valid_pairs:
            await update_status(status, "❌ All files in the album failed to download.")
            return None

        try:
            from pyrogram.types import (
                InputMediaPhoto, InputMediaVideo,
                InputMediaDocument, InputMediaAudio,
            )

            media_list = []
            for idx, (m, path) in enumerate(valid_pairs):
                raw_cap = getattr(m, "message", "") or ""
                cap = truncate_caption(apply_caption_filter(raw_cap, _cap_filters, _cap_append)) if idx == 0 else ""
                ext = _os.path.splitext(path)[1].lower()

                if ext in (".jpg", ".jpeg", ".png", ".webp"):
                    media_list.append(InputMediaPhoto(path, caption=cap))
                elif ext in (".mp4", ".mkv", ".mov", ".avi", ".webm"):
                    dur = int(getattr(m.file, "duration", 0) or 0)
                    w = int(getattr(m.file, "width", 0) or 0)
                    h = int(getattr(m.file, "height", 0) or 0)
                    if not dur or not w or not h:
                        mi_dur, mi_w, mi_h = await get_media_info(path)
                        dur = dur or mi_dur; w = w or mi_w; h = h or mi_h
                    media_list.append(InputMediaVideo(
                        path, caption=cap,
                        duration=dur, width=w, height=h,
                        supports_streaming=True,
                    ))
                elif ext in (".mp3", ".m4a", ".flac"):
                    dur = int(getattr(m.file, "duration", 0) or 0)
                    perf = getattr(m.file, "performer", "") or ""
                    tit = getattr(m.file, "title", "") or ""
                    if not perf or not tit:
                        p2, t2 = await get_audio_tags(path)
                        perf = perf or p2; tit = tit or t2
                    media_list.append(InputMediaAudio(
                        path, caption=cap,
                        duration=dur,
                        performer=perf or None,
                        title=tit or None,
                    ))
                else:
                    fn = getattr(m.file, "name", None) or _os.path.basename(path)
                    media_list.append(InputMediaDocument(path, caption=cap, file_name=fn))

            await update_status(status, f"📤 Uploading album ({len(media_list)} files)...")
            try:
                await upload_client.send_media_group(user_id, media_list)
            except Exception as grp_exc:
                logging.warning(f"Telethon album send_media_group failed ({grp_exc}), falling back to individual uploads")
                for idx, (m, path) in enumerate(valid_pairs):
                    raw_cap = getattr(m, "message", "") or ""
                    cap = truncate_caption(apply_caption_filter(raw_cap, _cap_filters, _cap_append)) if idx == 0 else ""
                    ext = _os.path.splitext(path)[1].lower()
                    fn = getattr(m.file, "name", None) or _os.path.basename(path)
                    dur = int(getattr(m.file, "duration", 0) or 0)
                    w = int(getattr(m.file, "width", 0) or 0)
                    h = int(getattr(m.file, "height", 0) or 0)
                    if not dur or not w or not h:
                        mi_dur, mi_w, mi_h = await get_media_info(path)
                        dur = dur or mi_dur; w = w or mi_w; h = h or mi_h
                    _force_doc = bool(
                        m.document is not None and
                        m.video is None and m.audio is None and
                        m.voice is None and m.gif is None and m.sticker is None
                    )
                    await upload_media(
                        upload_client, chat_id=user_id, path=path, caption=cap,
                        file_name=fn, duration=dur, width=w, height=h,
                        force_document=_force_doc,
                    )
        finally:
            for path in paths:
                try:
                    if _os.path.exists(path):
                        _os.remove(path)
                except Exception:
                    pass

        if not skip_quota and user.get("role", "free") == "free":
            await increment_quota(user_id, count=len(messages))
        if not status_msg_override:
            try:
                await status.delete()
            except Exception:
                pass
        log_download(user_id, _username, link, _ltype, True)
        return msg

    # --- Single file ---
    m = messages[0]
    path = None
    thumb_path = None
    try:
        file_size = getattr(m.file, "size", 0) or 0 if m.file else 0
        if file_size and file_size > PREMIUM_MAX_FILE_SIZE:
            readable = f"{file_size / 1_000_000_000:.1f} GB"
            await update_status(status, f"❌ File too large ({readable}). Maximum supported size is 4 GB.")
            return None

        async def _tl_progress(current, total):
            try:
                await progress_bar(current, total, status, "📥 Downloading")
            except StopTransmission:
                raise
            except Exception:
                pass

        from bot.fasttelethon import download_file_fast
        _tl_ext = (getattr(m.file, "ext", "") or "") if m.file else ""
        _tl_out = f"downloads/{user_id}_{m.id}{_tl_ext}"

        await update_status(status, "📥 Downloading...")
        try:
            path = await download_file_fast(tl_client, m, _tl_out, _tl_progress)
        except Exception as _fast_exc:
            logging.warning(f"Fast download failed for user {user_id}: {_fast_exc!r}")
            try:
                if _os.path.exists(_tl_out):
                    _os.remove(_tl_out)
            except Exception:
                pass
            await update_status(status, "⚠️ Retrying with fallback method...")
            await asyncio.sleep(1)
            path = await tl_client.download_media(m, file="downloads/", progress_callback=_tl_progress)

        if not path:
            await update_status(status, "❌ Download returned no data. Please try again.")
            return None

        if user_id in cancel_flags:
            cancel_flags.discard(user_id)
            await update_status(status, "🛑 Cancelled.")
            return None

        # Metadata from Telethon message
        raw_cap = getattr(m, "message", "") or ""
        caption = truncate_caption(apply_caption_filter(raw_cap, _cap_filters, _cap_append))

        duration = int(getattr(m.file, "duration", 0) or 0) if m.file else 0
        width = int(getattr(m.file, "width", 0) or 0) if m.file else 0
        height = int(getattr(m.file, "height", 0) or 0) if m.file else 0
        file_name = (getattr(m.file, "name", None) if m.file else None)
        performer = (getattr(m.file, "performer", "") or "") if m.file else ""
        title_tag = (getattr(m.file, "title", "") or "") if m.file else ""

        if not duration or not width or not height:
            mi_dur, mi_w, mi_h = await get_media_info(path)
            duration = duration or mi_dur
            width = width or mi_w
            height = height or mi_h

        if not performer or not title_tag:
            p2, t2 = await get_audio_tags(path)
            performer = performer or p2
            title_tag = title_tag or t2

        _force_doc = bool(
            m.document is not None and
            m.video is None and m.audio is None and
            m.voice is None and m.gif is None and m.sticker is None
        )

        # Extract thumbnail from the Telethon document (videos/audio)
        thumb_path = None
        if not _force_doc:
            try:
                _doc = m.document
                if _doc and getattr(_doc, "thumbs", None):
                    _tp = f"downloads/{user_id}_{m.id}_thumb.jpg"
                    await tl_client.download_media(m, file=_tp, thumb=-1)
                    if _os.path.exists(_tp) and _os.path.getsize(_tp) > 0:
                        thumb_path = _tp
                    elif _os.path.exists(_tp):
                        _os.remove(_tp)
            except Exception:
                thumb_path = None

        actual_size = _os.path.getsize(path)

        upload_kwargs = dict(
            chat_id=user_id, path=path,
            caption=caption, thumb=thumb_path, file_name=file_name,
            duration=duration, width=width, height=height,
            performer=performer, title=title_tag,
            progress=progress_bar, progress_args=(status, "📤 Uploading"),
            force_document=_force_doc,
        )

        _large_handled = False
        if actual_size > BOT_MAX_FILE_SIZE:
            readable = f"{actual_size / 1_000_000_000:.2f} GB"
            await update_status(
                status,
                f"⚠️ File is {readable}. Splitting into parts and uploading...",
            )
            part_paths = []
            try:
                part_paths = await split_video_ffmpeg(path, PART_SAFE_SIZE)
                total_parts = len(part_paths)
                orig_name = file_name or _os.path.basename(path)
                base_name, ext_name = _os.path.splitext(orig_name)

                for i, part_path in enumerate(part_paths, 1):
                    if user_id in cancel_flags:
                        cancel_flags.discard(user_id)
                        await update_status(status, "🛑 Cancelled.")
                        return None

                    part_fn = f"{base_name}.part{i}of{total_parts}{ext_name}"
                    part_cap = truncate_caption(
                        f"{caption}\n📦 Part {i}/{total_parts}" if caption else f"📦 Part {i}/{total_parts}"
                    )
                    p_dur, p_w, p_h = await get_media_info(part_path)
                    part_kw = {
                        **upload_kwargs,
                        "path": part_path, "caption": part_cap,
                        "file_name": part_fn, "progress_args": (status, f"📤 Uploading part {i}/{total_parts}"),
                        "duration": p_dur, "width": p_w, "height": p_h, "thumb": None,
                    }
                    await update_status(status, f"📤 Uploading part {i}/{total_parts}...")

                    _part_up = False
                    if upload_client is not client:
                        try:
                            await asyncio.wait_for(upload_media(upload_client, **part_kw), timeout=1800)
                            _part_up = True
                        except Exception as part_exc:
                            logging.warning(f"Telethon path user bot part {i} failed for {user_id}: {part_exc!r}")
                    if not _part_up:
                        await asyncio.wait_for(upload_media(client, **part_kw), timeout=1800)
            finally:
                for pp in part_paths:
                    try:
                        if _os.path.exists(pp):
                            _os.remove(pp)
                    except Exception:
                        pass
            _large_handled = True

        if not _large_handled:
            _uploaded = False
            if upload_client is not client:
                try:
                    await asyncio.wait_for(upload_media(upload_client, **upload_kwargs), timeout=1800)
                    _uploaded = True
                except Exception as bot_exc:
                    logging.warning(f"Telethon path user bot upload failed for {user_id}: {bot_exc!r}")
                    await update_status(status, "⚠️ Your bot failed, retrying with main bot...")
            if not _uploaded:
                await asyncio.wait_for(upload_media(client, **upload_kwargs), timeout=1800)

        if not skip_quota and user.get("role", "free") == "free":
            await increment_quota(user_id)
        if not status_msg_override:
            try:
                await status.delete()
            except Exception:
                pass
        log_download(user_id, _username, link, _ltype, True)
        return msg

    except asyncio.TimeoutError:
        await update_status(status, "❌ Transfer timed out. Please try again.")
        log_download(user_id, _username, link, _ltype, False)
        return None
    except Exception as e:
        logging.error(f"Download/upload error for user {user_id}: {e}")
        await update_status(status, f"❌ An error occurred. Please try again.")
        log_download(user_id, _username, link, _ltype, False)
        return None
    finally:
        if path and _os.path.exists(path):
            try:
                _os.remove(path)
            except Exception:
                pass
        if thumb_path and _os.path.exists(thumb_path):
            try:
                _os.remove(thumb_path)
            except Exception:
                pass


# Download handler — entry point for all t.me / telegram.me / telegram.dog links
@app.on_message(filters.regex(TG_LINK_HOST_RE) & filters.private & ~filters.regex(r"^/"))
async def download_handler(
    client,
    message,
    link_override: str = None,
    status_msg_override=None,
    processed_albums: set = None,
    skip_quota: bool = False,
    user_override=None,
):
    user_id = message.from_user.id
    link = link_override or message.text.strip()
    _username = getattr(message.from_user, "username", None)
    _ltype = "private"

    is_story = False
    is_topic = False
    is_bot_dm = False
    is_bot_start = False
    bot_start_username = None
    bot_start_payload = None
    story_parsed = _parse_story_link(link)
    if story_parsed:
        is_story = True
        chat_id, msg_id, _ = story_parsed
        is_private = True
        comment_id = None
        thread_id = None
    else:
        start_parsed = _parse_bot_start_link(link)
        if start_parsed:
            bot_start_username, bot_start_payload = start_parsed
            is_bot_start = True
            is_private = True
            chat_id = bot_start_username
            msg_id = None
            comment_id = None
            thread_id = None
        else:
            parsed = _parse_link(link)
            if not parsed:
                if not link_override:
                    await message.reply("❌ Unsupported link format.")
                return None
            chat_id, msg_id, is_private, comment_id, thread_id, is_topic = parsed

            if (
                not is_private
                and isinstance(chat_id, str)
                and chat_id.lower().endswith("bot")
            ):
                is_bot_dm = True
                is_private = True

    if is_story:
        _ltype = "story"
    elif is_bot_start:
        _ltype = "bot_start"
    elif is_bot_dm:
        _ltype = "bot_dm"
    elif is_private:
        _ltype = "private"
    else:
        _ltype = "public"

    if not link_override:
        logging.info(f"Download requested: user={user_id} type={_ltype} link={link[:80]}")

    if user_override is not None:
        user = user_override
    else:
        user = await get_user(user_id)
        if not user:
            from bot.database import create_user
            user = await create_user(
                user_id,
                message.from_user.username,
                f"{message.from_user.first_name or ''} {message.from_user.last_name or ''}".strip(),
            )

    if user.get("is_banned") or user.get("role") == "banned":
        if not link_override:
            await message.reply("❌ You are banned from using this bot.")
        return None

    # Load caption filters + append text once — free for default users (empty list / no append)
    import json as _json
    _raw_cf = user.get("caption_filters")
    try:
        _cap_filters = _json.loads(_raw_cf) if isinstance(_raw_cf, str) else (_raw_cf or [])
        if not isinstance(_cap_filters, list):
            _cap_filters = []
    except Exception:
        _cap_filters = []
    _cap_append = (user.get("caption_append") or "").strip()

    _is_premium = user.get("role") in ("premium", "admin", "owner")
    _engine = "pyrogram"
    if _is_premium:
        from bot.database import get_download_engine
        _engine = await get_download_engine(user_id)

    if is_topic and not is_private and user.get("phone_session_string"):
        is_private = True

    _telethon_can_handle = (
        _engine == "telethon"
        and bool(user.get("telethon_session_string"))
        and not is_story
    )

    if (is_private or is_story) and not user.get("phone_session_string") and not _telethon_can_handle:
        if not link_override:
            if is_story:
                await message.reply(
                    "❌ Story downloads require your Telegram account.\n"
                    "Use /login to connect your account first."
                )
            elif is_bot_start:
                await message.reply(
                    "❌ Triggering a bot with a start link requires your Telegram account.\n"
                    "Use /login to connect your account first."
                )
            elif is_bot_dm:
                await message.reply(
                    "❌ Extracting content from bot DMs requires your Telegram account.\n"
                    "Use /login to connect your account first.\n\n"
                )
            else:
                await message.reply("❌ This link is private. Use /login to connect your account first.")
        return None

    if not skip_quota and user.get("role", "free") == "free":
        can, reason = await check_and_update_quota(user_id)
        if not can:
            if not link_override:
                await message.reply(f"❌ {reason}")
            return None

    status = status_msg_override or _LazyStatus(message)

    acquired = False
    try:
        await global_download_semaphore.acquire()
        acquired = True
        if user_id in active_downloads:
            global_download_semaphore.release()
            acquired = False
            await update_status(status, "⚠️ You already have an active download. Please wait.")
            return None
        active_downloads.add(user_id)
    except Exception:
        if acquired:
            global_download_semaphore.release()
        raise

    try:
        # ---- Telethon engine branch (premium only, private links only) ----
        # Public channel links use server-side copy regardless of engine.
        if _engine == "telethon" and not is_story and is_private:
            return await _handle_telethon_download(
                client=client,
                user_id=user_id,
                user=user,
                link=link,
                chat_id=chat_id,
                msg_id=msg_id,
                is_private=is_private,
                is_bot_dm=is_bot_dm,
                is_bot_start=is_bot_start,
                comment_id=comment_id,
                thread_id=thread_id,
                _ltype=_ltype,
                _username=_username,
                _cap_filters=_cap_filters,
                _cap_append=_cap_append,
                status=status,
                skip_quota=skip_quota,
                status_msg_override=status_msg_override,
            )
        # ---- End Telethon branch ----

        if is_private or is_story:
            user_client = await get_user_client(user_id, user["phone_session_string"])
            active_sessions.add(user_id)
        else:
            user_client = client

        if is_story:
            try:
                story_obj = await user_client.get_stories(chat_id, msg_id)
            except (AuthKeyUnregistered, SessionRevoked, SessionExpired,
                    AuthKeyInvalid, AuthKeyPermEmpty, UserDeactivated) as e:
                logging.warning(f"Session expired for user {user_id}: {type(e).__name__} — session cleared")
                await _evict_user_session(user_id)
                await logout_user(user_id)
                await update_status(status, "❌ Your session expired. Please /login again.")
                return "SESSION_INVALID"
            except Exception as e:
                await update_status(status, f"❌ Could not fetch story: {e}")
                return None
            if not story_obj or not getattr(story_obj, "media", None):
                await update_status(status, "❌ Story not found, already expired, or has no downloadable media.")
                return None
            msg = story_obj
            messages = [story_obj]
        elif is_bot_start:
            try:
                await update_status(status, "🤖 Sending start command to bot...")
                sent = await user_client.send_message(bot_start_username, f"/start {bot_start_payload}")
                for _ in range(5):
                    if user_id in cancel_flags:
                        break
                    await asyncio.sleep(1)

                recent = await _bot_start_collect_media(user_client, bot_start_username, sent.id)

                if not recent:
                    await update_status(
                        status,
                        f"❌ The bot didn't send any files. It may require you to join channel(s) first.\n\n"
                        f"Open @{bot_start_username} to see which channels to join, Join those."
                        f"then resend the same link here."
                    )
                    return None

                msg = recent[0]
                msg_id = msg.id
                messages = recent
            except (AuthKeyUnregistered, SessionRevoked, SessionExpired,
                    AuthKeyInvalid, AuthKeyPermEmpty, UserDeactivated) as e:
                logging.warning(f"Session expired for user {user_id}: {type(e).__name__} — session cleared")
                await _evict_user_session(user_id)
                await logout_user(user_id)
                await update_status(status, "❌ Your session expired. Please /login again.")
                return "SESSION_INVALID"
            except Exception as e:
                await update_status(status, f"❌ Could not trigger bot start link: {e}")
                return None
        elif is_bot_dm:
            try:
                await update_status(status, "🤖 Fetching from bot DM...")
                try:
                    async for _ in user_client.get_dialogs(limit=50):
                        pass
                except Exception:
                    pass

                msg = await user_client.get_messages(chat_id, msg_id, replies=0)

                if not msg or (getattr(msg, "empty", False)):
                    try:
                        await user_client.get_chat(f"@{chat_id}")
                        msg = await user_client.get_messages(chat_id, msg_id, replies=0)
                    except Exception:
                        pass

            except (AuthKeyUnregistered, SessionRevoked, SessionExpired,
                    AuthKeyInvalid, AuthKeyPermEmpty, UserDeactivated) as e:
                logging.warning(f"Session expired for user {user_id}: {type(e).__name__} — session cleared")
                await _evict_user_session(user_id)
                await logout_user(user_id)
                await update_status(status, "❌ Your session expired. Please /login again.")
                return "SESSION_INVALID"
            except Exception as e:
                await update_status(status, f"❌ Could not fetch message from bot DM: {e}")
                return None
        else:
            try:
                msg = await user_client.get_messages(chat_id, msg_id, replies=0)
                if not is_private and not getattr(msg, "media", None) and not getattr(msg, "text", None) and user.get("phone_session_string"):
                    user_client = await get_user_client(user_id, user["phone_session_string"])
                    active_sessions.add(user_id)
                    msg = await user_client.get_messages(chat_id, msg_id, replies=0)
                    is_private = True
            except (AuthKeyUnregistered, SessionRevoked, SessionExpired,
                    AuthKeyInvalid, AuthKeyPermEmpty, UserDeactivated) as e:
                logging.warning(f"Session expired for user {user_id}: {type(e).__name__} — session cleared")
                await _evict_user_session(user_id)
                await logout_user(user_id)
                await update_status(status, "❌ Your session expired. Please /login again.")
                return "SESSION_INVALID"
            except Exception as e:
                await update_status(status, f"❌ Could not fetch message: {e}")
                return None

        if not is_story and not is_bot_dm and not is_bot_start and comment_id is not None:
            _resolve_client = user_client
            if not is_private and user.get("phone_session_string"):
                try:
                    _resolve_client = await get_user_client(user_id, user["phone_session_string"])
                    active_sessions.add(user_id)
                except Exception:
                    _resolve_client = user_client
            try:
                disc = await _resolve_client.get_discussion_message(chat_id, msg_id)
                disc_chat_id = disc.chat.id
                comment_msg = await _resolve_client.get_messages(disc_chat_id, comment_id, replies=0)
                if comment_msg and getattr(comment_msg, "media", None):
                    msg = comment_msg
                    msg_id = msg.id
                    chat_id = disc_chat_id
                    if not is_private:
                        user_client = _resolve_client
                        is_private = True
            except Exception as e:
                logging.debug(f"Comment resolution failed (comment_id={comment_id}): {e} — falling back to original post")

        if not is_story and not is_bot_dm and not is_bot_start and thread_id is not None and comment_id is None:
            _resolve_client = user_client
            if not is_private and user.get("phone_session_string"):
                try:
                    _resolve_client = await get_user_client(user_id, user["phone_session_string"])
                    active_sessions.add(user_id)
                except Exception:
                    _resolve_client = user_client
            try:
                disc = await _resolve_client.get_discussion_message(chat_id, thread_id)
                disc_chat_id = disc.chat.id
                if disc_chat_id != chat_id:
                    disc_msg = await _resolve_client.get_messages(disc_chat_id, msg_id, replies=0)
                    if disc_msg and getattr(disc_msg, "media", None):
                        msg = disc_msg
                        msg_id = msg.id
                        chat_id = disc_chat_id
                        if not is_private:
                            user_client = _resolve_client
                            is_private = True
            except Exception as e:
                logging.info(f"Thread resolution (thread={thread_id}): {e} — using direct message fetch")

        if not msg:
            await update_status(status, "❌ Message not found or not accessible.")
            return None

        has_media = bool(getattr(msg, "media", None))
        has_text = bool(getattr(msg, "text", None))

        if has_media and not is_story and isinstance(msg.media, enums.MessageMediaType):
            if msg.media not in _DOWNLOADABLE_TYPES:
                type_name = msg.media.name.replace("_", " ").title()
                await update_status(
                    status,
                    f"❌ **{type_name}** messages cannot be downloaded — there is no file to send."
                )
                return None

        if not has_media and not has_text:
            await update_status(status,
                "❌ No content found. This may be a public group — use /login to connect your account and try again."
                if not is_private and not user.get("phone_session_string")
                else "❌ No content found at this link (message is empty)."
            )
            return None

        if not is_story and not is_bot_start and msg.media_group_id:
            if processed_albums is not None:
                if msg.media_group_id in processed_albums:
                    if not status_msg_override:
                        try:
                            await status.delete()
                        except Exception:
                            pass
                    return "ALBUM_DEDUP"
                processed_albums.add(msg.media_group_id)
            try:
                messages = await user_client.get_media_group(chat_id, msg_id)
            except Exception:
                messages = [msg]
        else:
            messages = [msg]

        if not skip_quota and user.get("role", "free") == "free" and len(messages) > 1:
            from bot.database import DAILY_LIMIT, MONTHLY_LIMIT
            from datetime import datetime as _dt
            _today = _dt.now().date()
            _month_first = _today.replace(day=1)
            _dl_today = user.get("downloads_today", 0)
            _last_date = user.get("last_download_date")
            if _last_date and _dt.fromisoformat(_last_date).date() != _today:
                _dl_today = 0
            _dl_month = user.get("downloads_this_month", 0)
            _last_month = user.get("last_download_month")
            if _last_month and _dt.fromisoformat(_last_month).date() != _month_first:
                _dl_month = 0
            _needed = len(messages)
            if _dl_today + _needed > DAILY_LIMIT:
                _left = max(0, DAILY_LIMIT - _dl_today)
                await update_status(
                    status,
                    f"❌ This album has **{_needed} files** but you only have **{_left}** download(s) left today.\n\n"
                    "👉 /upgrade to Premium for unlimited downloads."
                )
                return None
            if _dl_month + _needed > MONTHLY_LIMIT:
                _left = max(0, MONTHLY_LIMIT - _dl_month)
                await update_status(
                    status,
                    f"❌ This album has **{_needed} files** but you only have **{_left}** download(s) left this month.\n\n"
                    "👉 /upgrade to Premium for unlimited downloads."
                )
                return None

        if not is_private:
            media_group_id = getattr(msg, "media_group_id", None)
            try:
                await update_status(status, "🚀 Extracting directly...")
                if media_group_id:
                    if _cap_filters or _cap_append:
                        # Only the first item carries the caption — that's how Telegram albums work.
                        # Subsequent items get an empty string so no hidden per-file captions appear.
                        first_msg = messages[0] if messages else msg
                        first_cap = apply_caption_filter(first_msg.caption, _cap_filters, _cap_append) if first_msg.caption else (
                            _cap_append if _cap_append else None
                        )
                        _captions = [first_cap] + [""] * (len(messages) - 1)
                        await client.copy_media_group(
                            chat_id=user_id, from_chat_id=chat_id, message_id=msg_id,
                            captions=_captions,
                        )
                    else:
                        await client.copy_media_group(
                            chat_id=user_id, from_chat_id=chat_id, message_id=msg_id
                        )
                else:
                    if _cap_filters or _cap_append:
                        await client.copy_message(
                            chat_id=user_id, from_chat_id=chat_id, message_id=msg_id,
                            caption=apply_caption_filter(msg.caption or "", _cap_filters, _cap_append),
                        )
                    else:
                        await client.copy_message(
                            chat_id=user_id, from_chat_id=chat_id, message_id=msg_id
                        )
                if not skip_quota and user.get("role", "free") == "free":
                    await increment_quota(user_id)
                if not status_msg_override:
                    try:
                        await status.delete()
                    except Exception:
                        pass
                active_downloads.discard(user_id)
                log_download(user_id, _username, link, "public", True)
                return msg
            except (AuthKeyUnregistered, SessionRevoked, SessionExpired,
                    AuthKeyInvalid, AuthKeyPermEmpty, UserDeactivated):
                raise
            except Exception as e:
                error_str = str(e)
                if "MEDIA_CAPTION_TOO_LONG" in error_str:
                    try:
                        if media_group_id:
                            await client.copy_media_group(
                                chat_id=user_id, from_chat_id=chat_id,
                                message_id=msg_id, captions=""
                            )
                        else:
                            await client.copy_message(
                                chat_id=user_id, from_chat_id=chat_id,
                                message_id=msg_id, caption=""
                            )
                        if not skip_quota and user.get("role", "free") == "free":
                            await increment_quota(user_id)
                        if not status_msg_override:
                            try:
                                await status.delete()
                            except Exception:
                                pass
                        active_downloads.discard(user_id)
                        log_download(user_id, _username, link, "public", True)
                        return msg
                    except Exception as retry_e:
                        logging.error(f"Direct extraction (no caption) failed: {retry_e}")
                elif "Unknown media" in error_str or "unknown media" in error_str.lower():
                    await update_status(status, "❌ This media type is not supported for direct extraction.")
                    return None
                elif "topics" in error_str and "__init__" in error_str:
                    logging.debug(f"Direct extraction skipped (pyrotgfork version mismatch): {e}")
                else:
                    logging.error(f"Direct extraction failed: {e}")
                await update_status(status, "⚠️ Direct extraction failed, trying download/upload...")

        if not has_media:
            text = getattr(msg, "text", None) or ""
            entities = getattr(msg, "entities", None) or []
            try:
                upload_bot = await get_user_bot(user_id)
            except (AccessTokenExpired, AccessTokenInvalid):
                await update_status(status, "❌ Your upload bot's token is no longer valid. Please use /setbot to register a new one.")
                return None
            sender = upload_bot if upload_bot is not None else client
            try:
                await update_status(status, "✍️ Copying text message...")
                await sender.send_message(
                    user_id, text,
                    entities=entities,
                    link_preview_options=LinkPreviewOptions(is_disabled=False),
                )
                if not status_msg_override:
                    try:
                        await status.delete()
                    except Exception:
                        pass
            except Exception as e:
                await update_status(status, f"❌ Failed to copy text message: {e}")
            active_downloads.discard(user_id)
            log_download(user_id, _username, link, _ltype, True)
            return msg

        for m in messages:
            sz = _get_msg_file_size(m)
            if sz and sz > PREMIUM_MAX_FILE_SIZE:
                readable = f"{sz / 1_000_000_000:.1f} GB"
                await update_status(status, f"❌ File too large ({readable}). Maximum supported size is 4 GB.")
                return None
            if sz and sz > BOT_MAX_FILE_SIZE and len(messages) > 1:
                readable = f"{sz / 1_000_000_000:.2f} GB"
                await update_status(status, f"❌ Album contains a file that is {readable}. Files over 2 GB cannot be part of an album download.")
                return None

        is_premium = user.get("role") in ("premium", "admin", "owner")
        try:
            user_bot = await get_user_bot(user_id)
        except (AccessTokenExpired, AccessTokenInvalid):
            await update_status(status, "❌ Your upload bot's token is no longer valid. Please use /setbot to register a new one.")
            return None

        if user_bot is None and is_premium:
            await update_status(
                status,
                "❌ **Upload bot not set up.**\n\n"
                "Premium users need to register their own upload bot.\n"
                "Use /setbot to set one up.\n\n"
                "1. Open @BotFather → `/newbot`\n"
                "2. Copy the token\n"
                "3. Run /setbot and send the token when prompted\n"
                "4. Press **Start** on your bot",
            )
            return None

        upload_client = user_bot if user_bot is not None else client

        if len(messages) > 1:
            await update_status(status, f"📥 Downloading album ({len(messages)} files)...")

            async def _album_dl_progress(current, total):
                await progress_bar(current, total, status, "📥 Downloading album")

            download_tasks = [
                download_media(user_client, m, progress=_album_dl_progress, progress_args=())
                for m in messages
            ]
            results = await asyncio.gather(*download_tasks, return_exceptions=True)

            for result in results:
                if isinstance(result, StopTransmission):
                    cancel_flags.discard(user_id)
                    await update_status(status, "🛑 Cancelled.")
                    return None
                if isinstance(result, (AuthKeyUnregistered, SessionRevoked, SessionExpired,
                                       AuthKeyInvalid, AuthKeyPermEmpty, UserDeactivated)):
                    raise result

            paths = []
            valid_pairs = []
            for m, result in zip(messages, results):
                if isinstance(result, Exception) or not result:
                    logging.warning(f"Album item failed to download: {result}")
                    continue
                paths.append(result)
                valid_pairs.append((m, result))

            if not valid_pairs:
                await update_status(status, "❌ All files in the album failed to download.")
                return None

            try:
                from pyrogram.types import (
                    InputMediaPhoto, InputMediaVideo,
                    InputMediaDocument, InputMediaAudio,
                )

                media_list = []
                for idx, (m, path) in enumerate(valid_pairs):
                    # Only the first file in an album gets the caption — matching Telegram's
                    # manual-send behaviour. Subsequent files get "" so no hidden captions appear.
                    cap = truncate_caption(apply_caption_filter(m.caption or "", _cap_filters, _cap_append)) if idx == 0 else ""
                    ext = os.path.splitext(path)[1].lower()
                    if ext in (".jpg", ".jpeg", ".png", ".webp"):
                        media_list.append(InputMediaPhoto(path, caption=cap))
                    elif ext in (".mp4", ".mkv", ".mov", ".avi", ".webm"):
                        dur = getattr(getattr(m, "video", None), "duration", 0) or 0
                        w   = getattr(getattr(m, "video", None), "width",    0) or 0
                        h   = getattr(getattr(m, "video", None), "height",   0) or 0
                        if not dur or not w or not h:
                            mi_dur, mi_w, mi_h = await get_media_info(path)
                            dur = dur or mi_dur
                            w   = w   or mi_w
                            h   = h   or mi_h
                        media_list.append(InputMediaVideo(
                            path, caption=cap,
                            duration=dur, width=w, height=h,
                            supports_streaming=True,
                        ))
                    elif ext in (".mp3", ".m4a", ".flac"):
                        dur       = getattr(getattr(m, "audio", None), "duration",  0)  or 0
                        performer = getattr(getattr(m, "audio", None), "performer", "") or ""
                        atitle    = getattr(getattr(m, "audio", None), "title",     "") or ""
                        if not performer or not atitle:
                            p2, t2    = await get_audio_tags(path)
                            performer = performer or p2
                            atitle    = atitle    or t2
                        media_list.append(InputMediaAudio(
                            path, caption=cap,
                            duration=dur,
                            performer=performer or None,
                            title=atitle or None,
                        ))
                    else:
                        fn = getattr(getattr(m, "document", None), "file_name", None)
                        media_list.append(InputMediaDocument(path, caption=cap, file_name=fn))

                await update_status(status, f"📤 Uploading album ({len(media_list)} files)...")
                try:
                    await upload_client.send_media_group(user_id, media_list)
                except Exception as grp_exc:
                    logging.warning(f"send_media_group failed ({grp_exc}), falling back to individual uploads")
                    for idx, (m, path) in enumerate(valid_pairs):
                        if user_id in cancel_flags:
                            cancel_flags.discard(user_id)
                            await update_status(status, "🛑 Cancelled.")
                            return None
                        # Caption only on the first file — same as manual Telegram album behaviour.
                        cap = truncate_caption(apply_caption_filter(m.caption or "", _cap_filters, _cap_append)) if idx == 0 else ""
                        dur = w = h = 0
                        fn = performer = atitle = ""
                        if getattr(m, "video", None):
                            dur = m.video.duration or 0
                            w   = m.video.width    or 0
                            h   = m.video.height   or 0
                            fn  = m.video.file_name or ""
                            if not dur or not w or not h:
                                mi_dur, mi_w, mi_h = await get_media_info(path)
                                dur = dur or mi_dur
                                w   = w   or mi_w
                                h   = h   or mi_h
                        elif getattr(m, "audio", None):
                            dur       = m.audio.duration  or 0
                            fn        = m.audio.file_name or ""
                            performer = m.audio.performer or ""
                            atitle    = m.audio.title     or ""
                            if not performer or not atitle:
                                p2, t2    = await get_audio_tags(path)
                                performer = performer or p2
                                atitle    = atitle    or t2
                        elif getattr(m, "document", None):
                            fn = m.document.file_name or ""
                        await upload_media(
                            upload_client,
                            chat_id=user_id, path=path, caption=cap,
                            file_name=fn, duration=dur, width=w, height=h,
                            performer=performer, title=atitle,
                            force_document=(m.media == enums.MessageMediaType.DOCUMENT),
                        )
            finally:
                for path in paths:
                    try:
                        if os.path.exists(path):
                            os.remove(path)
                    except Exception:
                        pass

        else:
            m = messages[0]

            if user_id in cancel_flags:
                cancel_flags.discard(user_id)
                await update_status(status, "🛑 Cancelled.")
                return None

            path = None
            thumb = None
            try:
                path = await asyncio.wait_for(
                    download_media(
                        user_client, m,
                        progress=progress_bar,
                        progress_args=(status, "📥 Downloading"),
                    ),
                    timeout=1800,
                )
                if not path:
                    return None

                if user_id in cancel_flags:
                    cancel_flags.discard(user_id)
                    await update_status(status, "🛑 Cancelled.")
                    return None

                caption   = truncate_caption(apply_caption_filter(m.caption or "", _cap_filters, _cap_append))
                duration  = width = height = 0
                file_name = None
                performer = title = ""

                if m.video:
                    duration  = m.video.duration or 0
                    width     = m.video.width    or 0
                    height    = m.video.height   or 0
                    file_name = m.video.file_name
                    if m.video.thumbs:
                        try:
                            thumb = await user_client.download_media(m.video.thumbs[-1], in_memory=True)
                        except Exception:
                            pass
                    if not duration or not width or not height:
                        mi_dur, mi_w, mi_h = await get_media_info(path)
                        duration = duration or mi_dur
                        width    = width    or mi_w
                        height   = height   or mi_h
                elif getattr(m, "document", None):
                    file_name = m.document.file_name
                elif getattr(m, "audio", None):
                    duration  = m.audio.duration  or 0
                    file_name = m.audio.file_name
                    performer = m.audio.performer or ""
                    title     = m.audio.title     or ""
                    if not duration:
                        mi_dur, _, _ = await get_media_info(path)
                        duration = duration or mi_dur
                    if not performer or not title:
                        p2, t2    = await get_audio_tags(path)
                        performer = performer or p2
                        title     = title     or t2

                upload_kwargs = dict(
                    chat_id=user_id, path=path,
                    caption=caption, thumb=thumb, file_name=file_name,
                    duration=duration, width=width, height=height,
                    performer=performer, title=title,
                    progress=progress_bar, progress_args=(status, "📤 Uploading"),
                    force_document=(m.media == enums.MessageMediaType.DOCUMENT),
                )

                actual_size = os.path.getsize(path)
                _large_file_handled = False

                if actual_size > BOT_MAX_FILE_SIZE:
                    has_tg_premium = await check_user_premium(user_client)

                    if has_tg_premium:
                        readable = f"{actual_size / 1_000_000_000:.2f} GB"
                        await update_status(
                            status,
                            f"📤 Uploading {readable} file via your Telegram account (Premium)...",
                        )
                        await asyncio.wait_for(
                            upload_media(user_client, **upload_kwargs),
                            timeout=3600,
                        )
                        _large_file_handled = True

                    else:
                        readable = f"{actual_size / 1_000_000_000:.2f} GB"
                        await update_status(
                            status,
                            f"⚠️ File is {readable}. Your Telegram account doesn't have Premium.\n"
                            f"📂 Splitting into parts and uploading...",
                        )
                        part_paths = []
                        try:
                            part_paths = await split_video_ffmpeg(path, PART_SAFE_SIZE)
                            total_parts = len(part_paths)
                            orig_name = file_name or os.path.basename(path)
                            base_name, ext_name = os.path.splitext(orig_name)

                            for i, part_path in enumerate(part_paths, 1):
                                if user_id in cancel_flags:
                                    cancel_flags.discard(user_id)
                                    await update_status(status, "🛑 Cancelled.")
                                    return None

                                part_fn  = f"{base_name}.part{i}of{total_parts}{ext_name}"
                                part_cap = truncate_caption(
                                    f"{caption}\n📦 Part {i}/{total_parts}" if caption
                                    else f"📦 Part {i}/{total_parts}"
                                )

                                p_dur, p_w, p_h = await get_media_info(part_path)
                                part_thumb = thumb if i == 1 else None

                                part_kw = {
                                    **upload_kwargs,
                                    "path":          part_path,
                                    "caption":       part_cap,
                                    "file_name":     part_fn,
                                    "progress_args": (status, f"📤 Uploading part {i}/{total_parts}"),
                                    "duration":      p_dur,
                                    "width":         p_w,
                                    "height":        p_h,
                                    "thumb":         part_thumb,
                                }
                                await update_status(status, f"📤 Uploading part {i}/{total_parts}...")

                                _part_up = False
                                if upload_client is not client:
                                    try:
                                        await asyncio.wait_for(
                                            upload_media(upload_client, **part_kw), timeout=1800
                                        )
                                        _part_up = True
                                    except Exception as part_exc:
                                        error_str = str(part_exc)
                                        if any(c in error_str for c in ("USER_IS_BLOCKED", "PEER_ID_INVALID", "BotStartCommandMissing")):
                                            bot_url = None
                                            try:
                                                me = await upload_client.get_me()
                                                bot_url = f"https://t.me/{me.username}?start=start" if me.username else None
                                            except Exception:
                                                pass
                                            markup = InlineKeyboardMarkup([[InlineKeyboardButton("▶️ Start My Bot", url=bot_url)]]) if bot_url else None
                                            await update_status(
                                                status,
                                                "❌ **Your bot couldn't send the file part.**\n\n"
                                                "You haven't started your bot yet. "
                                                "Tap the button below, press **Start**, then resend the link.",
                                                reply_markup=markup,
                                            )
                                            return None
                                        logging.warning(f"User bot part {i} upload failed for {user_id}, falling back: {part_exc!r}")

                                if not _part_up:
                                    await asyncio.wait_for(
                                        upload_media(client, **part_kw), timeout=1800
                                    )

                        finally:
                            for pp in part_paths:
                                try:
                                    if os.path.exists(pp):
                                        os.remove(pp)
                                except Exception:
                                    pass
                        _large_file_handled = True

                if not _large_file_handled:
                    _uploaded = False
                    if upload_client is not client:
                        try:
                            await asyncio.wait_for(upload_media(upload_client, **upload_kwargs), timeout=1800)
                            _uploaded = True
                        except Exception as bot_exc:
                            error_str = str(bot_exc)
                            if any(c in error_str for c in ("USER_IS_BLOCKED", "PEER_ID_INVALID", "BotStartCommandMissing")):
                                bot_url = None
                                try:
                                    me = await upload_client.get_me()
                                    bot_url = f"https://t.me/{me.username}?start=start" if me.username else None
                                except Exception:
                                    pass
                                markup = InlineKeyboardMarkup([[InlineKeyboardButton("▶️ Start My Bot", url=bot_url)]]) if bot_url else None
                                await update_status(
                                    status,
                                    "❌ **Your bot couldn't send the file.**\n\n"
                                    "You haven't started your bot yet. "
                                    "Tap the button below, press **Start**, then resend the link.",
                                    reply_markup=markup,
                                )
                                return None
                            logging.warning(f"User bot upload failed for {user_id}, falling back to main bot: {bot_exc!r}")
                            await update_status(status, "⚠️ Your bot failed, retrying with main bot...")

                    if not _uploaded:
                        await asyncio.wait_for(upload_media(client, **upload_kwargs), timeout=1800)

            except (AuthKeyUnregistered, SessionRevoked, SessionExpired,
                    AuthKeyInvalid, AuthKeyPermEmpty, UserDeactivated):
                raise
            except asyncio.TimeoutError:
                await update_status(status, "❌ Transfer timed out. The file may be too large or the connection too slow. Please try again.")
                log_download(user_id, _username, link, _ltype, False)
                return None
            except Exception as e:
                logging.error(f"Download/upload error for user {user_id}: {e}")
                log_download(user_id, _username, link, _ltype, False)
                return None
            finally:
                if path and os.path.exists(path):
                    os.remove(path)

        if not skip_quota and user.get("role", "free") == "free":
            await increment_quota(user_id, count=len(messages))

        if not status_msg_override:
            try:
                await status.delete()
            except Exception:
                pass

        log_download(user_id, _username, link, _ltype, True)
        return msg

    except (AuthKeyUnregistered, SessionRevoked, SessionExpired,
            AuthKeyInvalid, AuthKeyPermEmpty, UserDeactivated) as e:
        logging.warning(f"Session error for user {user_id}: {type(e).__name__} — session cleared")
        await _evict_user_session(user_id)
        await logout_user(user_id)
        try:
            await update_status(status, "❌ Your Telegram session has expired or was revoked. Please use /login to reconnect.")
        except Exception:
            pass
        log_download(user_id, _username, link, _ltype, False)
        return "SESSION_INVALID"
    except Exception as e:
        logging.error(f"Handler error for user {user_id}: {e}")
        try:
            await update_status(status, f"❌ Error: {e}")
        except Exception:
            pass
        log_download(user_id, _username, link, _ltype, False)
        return None
    finally:
        active_downloads.discard(user_id)
        active_sessions.discard(user_id)
        cancel_flags.discard(user_id)
        if acquired:
            global_download_semaphore.release()
        if hasattr(progress_bar, "_data") and status:
            progress_bar._data.pop(status.id, None)


# /cancel
@app.on_message(filters.command("cancel") & filters.private)
async def cancel_handler(client, message):
    user_id = message.from_user.id
    if user_id in active_downloads or user_id in active_sessions:
        cancel_flags.add(user_id)
        await message.reply("🛑 Cancelling current download...")
    else:
        await message.reply("ℹ️ No active download to cancel.")


# /help and /upgrade
@app.on_message(filters.command("help") & filters.private)
async def help_command(client, message):
    user_id = message.from_user.id
    user = await get_user(user_id)
    role = (user.get("role", "free") if user else "free")
    is_premium = role in ("premium", "admin", "owner")

    if is_premium:
        text = (
            "📖 **Help — Premium**\n\n"

            "🔗 **Links**\n"
            "• Public `t.me` links — send and receive instantly, no setup needed\n"
            "• Private / restricted links — requires /login + /setbot\n"
            "• Bot DM links — `t.me/BotName/123` or `t.me/BotName?start=XXX` (requires /login)\n\n"

            "👤 **Account**\n"
            "/login — connect your Telegram account (needed for private links)\n"
            "/logout — disconnect your account\n\n"

            "🤖 **Upload Bot**\n"
            "/setbot `<token>` — register your own upload bot\n"
            "/rembot — remove your upload bot\n\n"

            "⚡ **Download Engine** _(Premium only)_\n"
            "/tlogin — connect a Telethon session for faster private-link downloads\n"
            "/tlogout — disconnect your Telethon session\n"
            "/setengine `pyrogram` — switch to standard engine\n"
            "/setengine `telethon` — switch to fast engine _(requires /tlogin + /setbot)_\n\n"

            "📦 **Batch**\n"
            "/batch `start_link end_link` — download a range of messages\n"
            "/batch `start_link 50` — download next 50 from a link\n"
            "/cancelbatch — stop an active batch\n\n"

            "🔗 **Multi-link**\n"
            "/mlinks — paste up to 50 links at once\n\n"

            "✏️ **Caption Tools**\n"
            "/capadd `set <text>` — append text to every caption\n"
            "/capadd `del` — remove appended text\n"
            "/caprem `set <text>` — remove words/phrases from captions\n"
            "/caprem `del <text>` — remove a specific filter\n"
            "/caprem `reset` — clear all caption filters\n"
            "_(use `\\n` in text for line breaks)_\n\n"

            "/cancel — stop an active download"
        )
    else:
        text = (
            "📖 **Help**\n\n"

            "🔗 **Links**\n"
            "• Public `t.me` links — send and receive instantly, no setup needed\n"
            "• Private / restricted links — requires /login\n"
            "• Bot DM links — `t.me/BotName/123` or `t.me/BotName?start=XXX` (requires /login)\n\n"

            "👤 **Account**\n"
            "/login — connect your Telegram account\n"
            "/logout — disconnect your account\n\n"

            "📊 **Quota**\n"
            "Free plan: 2 files/day · 5 files/month\n\n"

            "/cancel — stop an active download\n\n"

            "💎 **Want unlimited downloads + batch + fast engine?** → /upgrade"
        )

    await message.reply(
        text,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Owner", url="https://t.me/Owner_Wolfy")],
            [InlineKeyboardButton("Support", url=SUPPORT_CHAT_LINK)],
        ]),
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )


@app.on_message(filters.command("upgrade") & filters.private)
async def upgrade_command(client, message):
    from bot.config import UPI_ID, PAYPAL_LINK, APPLE_PAY_ID, CRYPTO_ADDRESS, CARD_PAYMENT_LINK
    await message.reply(
        "💎 **Premium Plans**\n\n"
        "⚡ **Standard**\n"
        "🔸 10 days — $3\n"
        "🔸 30 days — $4\n"
        "🔸 60 days — $8\n"
        "🔸 90 days — $12\n\n"
        "• Unlimited downloads\n"
        "• Batch up to 50 files\n"
        "• Multi-link up to 50\n"
        "• Fast speed\n"
        "• Caption tools (/capadd · /caprem)\n\n"
        "🔥 **1 Year — $45**\n"
        "• All features + priority support\n\n"
        "💳 **Payment**\n"
        f"🪙 [Crypto / Binance]({CRYPTO_ADDRESS})\n"
        f"🇮🇳 [UPI]({UPI_ID})\n"
        f"💲 [PayPal]({PAYPAL_LINK})\n"
        f"🍎 [Apple Pay]({APPLE_PAY_ID})\n"
        f"💳 [Credit/Debit Card]({CARD_PAYMENT_LINK})\n\n"
        "After payment send screenshot to **@Owner_Wolfy**.",
        link_preview_options=LinkPreviewOptions(is_disabled=True),
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Owner", url="https://t.me/Owner_Wolfy")],
            [InlineKeyboardButton("Support", url=SUPPORT_CHAT_LINK)],
        ]),
    )
