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

from bot.config import (
    app, API_ID, API_HASH,
    active_downloads, global_download_semaphore,
    cancel_flags, batch_sessions, login_states,
    SUPPORT_CHAT_LINK,
)
from bot.database import get_user, check_and_update_quota, get_setting, increment_quota
from bot.transfer import download_media, upload_media, truncate_caption, get_user_bot, get_media_info, get_audio_tags


MAX_FILE_SIZE = 2_000_000_000  # 2 GB — bot upload hard limit

# Media types that have an actual file to download.
# Everything else (Poll, Game, Location, Contact, Dice, Story-embed, etc.) has no file_id.
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


# --- User session cache ---

user_clients: dict = {}
active_sessions: set = set()
_cleanup_started = False



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
            and now - last > 3600
        ]
        for uid in stale_bots:
            user_bots_last_used.pop(uid, None)
            await stop_user_bot(uid)
            logging.info(f"Evicted idle user bot for user {uid}")


# --- Utilities ---

class _LazyStatus:
    """
    For single downloads (no status_msg_override): sends no message at all on
    fast/happy paths. The first call to edit_text() fires a reply; subsequent
    calls edit that message. delete() is a no-op if nothing was ever sent.
    """
    def __init__(self, message):
        self._message = message
        self._sent = None

    @property
    def chat(self):
        # Delegate to the original triggering message so progress_bar's
        # cancel-flag check (msg.chat.id in cancel_flags) works correctly.
        return self._message.chat

    @property
    def id(self):
        # Use the sent message id once available; fall back to the triggering
        # message id so progress_bar can use it as a stable dedup key.
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


# --- Force subscribe ---

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


# --- Link parsing ---

def _parse_story_link(link: str):
    """
    Parse a t.me story link.
    Returns (chat_id, story_id, is_private) or None.
    """
    link_clean = re.sub(r"\?.*$", "", link).rstrip("/")

    m = re.fullmatch(r"https://t\.me/c/(\d+)/s/(\d+)", link_clean)
    if m:
        return int("-100" + m.group(1)), int(m.group(2)), True

    m = re.fullmatch(r"https://t\.me/([^/+][^/]+)/s/(\d+)", link_clean)
    if m:
        return m.group(1), int(m.group(2)), False

    return None


def _parse_link(link: str):
    """
    Parse a t.me message link into (chat_id, message_id, is_private, comment_id, thread_id, is_topic).
    Returns None if the link format is not recognised.
    """
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


# --- Core download handler ---

@app.on_message(filters.regex(r"https?://t\.me/") & filters.private & ~filters.regex(r"^/"))
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

    is_story = False
    is_topic = False
    story_parsed = _parse_story_link(link)
    if story_parsed:
        is_story = True
        chat_id, msg_id, _ = story_parsed
        is_private = True
        comment_id = None
        thread_id = None
    else:
        parsed = _parse_link(link)
        if not parsed:
            if not link_override:
                await message.reply("❌ Unsupported link format.")
            return None
        chat_id, msg_id, is_private, comment_id, thread_id, is_topic = parsed

    if not link_override:
        _ltype = "story" if is_story else ("private" if is_private else "public")
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

    if user.get("role") == "banned":
        if not link_override:
            await message.reply("❌ You are banned from using this bot.")
        return None

    if is_topic and not is_private and user.get("phone_session_string"):
        is_private = True

    if (is_private or is_story) and not user.get("phone_session_string"):
        if not link_override:
            if is_story:
                await message.reply(
                    "❌ Story downloads require your Telegram account.\n"
                    "Use /login to connect your account first."
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

    async with global_download_semaphore:
        if user_id in active_downloads:
            await update_status(status, "⚠️ You already have an active download. Please wait.")
            return None
        active_downloads.add(user_id)

    try:
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
                from bot.database import logout_user
                logging.warning(f"Session expired for user {user_id}: {type(e).__name__} — session cleared")
                await logout_user(user_id)
                await update_status(status, "❌ Your session expired. Please /login again.")
                return None
            except Exception as e:
                await update_status(status, f"❌ Could not fetch story: {e}")
                return None
            if not story_obj or not getattr(story_obj, "media", None):
                await update_status(status, "❌ Story not found, already expired, or has no downloadable media.")
                return None
            msg = story_obj
            messages = [story_obj]
        else:
            try:
                msg = await user_client.get_messages(chat_id, msg_id, replies=0)
                # Bots can't read public groups — retry with user session if message is empty
                if not is_private and not getattr(msg, "media", None) and not getattr(msg, "text", None) and user.get("phone_session_string"):
                    user_client = await get_user_client(user_id, user["phone_session_string"])
                    active_sessions.add(user_id)
                    msg = await user_client.get_messages(chat_id, msg_id, replies=0)
                    is_private = True
            except (AuthKeyUnregistered, SessionRevoked, SessionExpired,
                    AuthKeyInvalid, AuthKeyPermEmpty, UserDeactivated) as e:
                from bot.database import logout_user
                logging.warning(f"Session expired for user {user_id}: {type(e).__name__} — session cleared")
                await logout_user(user_id)
                await update_status(status, "❌ Your session expired. Please /login again.")
                return None
            except Exception as e:
                await update_status(status, f"❌ Could not fetch message: {e}")
                return None

        if not is_story and comment_id is not None:
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

        if not is_story and thread_id is not None and comment_id is None:
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

        # Guard: reject non-downloadable media types (Poll, Game, Location, Contact, Dice, etc.)
        # Story URLs set is_story=True and msg is a Story object whose .media is a raw Photo/Video,
        # not a MessageMediaType enum — skip the enum check for that path.
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

        if not is_story and msg.media_group_id:
            if processed_albums is not None:
                if msg.media_group_id in processed_albums:
                    if not status_msg_override:
                        try:
                            await status.delete()
                        except Exception:
                            pass
                    return msg.id
                processed_albums.add(msg.media_group_id)
            try:
                messages = await user_client.get_media_group(chat_id, msg_id)
            except Exception:
                messages = [msg]
        else:
            messages = [msg]

        # Album quota check — free users must have enough quota for every file in the group
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

        # --- Public content: server-side copy via main bot to user's DM ---
        if not is_private:
            media_group_id = getattr(msg, "media_group_id", None)
            try:
                await update_status(status, "🚀 Extracting directly...")
                if media_group_id:
                    await client.copy_media_group(
                        chat_id=user_id, from_chat_id=chat_id, message_id=msg_id
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
                        return msg
                    except Exception as retry_e:
                        logging.error(f"Direct extraction (no caption) failed: {retry_e}")
                elif "Unknown media" in error_str or "unknown media" in error_str.lower():
                    await update_status(status, "❌ This media type is not supported for direct extraction.")
                    return None
                elif "topics" in error_str and "__init__" in error_str:
                    # pyrotgfork version mismatch — Telegram added 'topics' field, older lib can't parse it
                    # falls through silently to download/upload path
                    logging.debug(f"Direct extraction skipped (pyrotgfork version mismatch): {e}")
                else:
                    logging.error(f"Direct extraction failed: {e}")
                await update_status(status, "⚠️ Direct extraction failed, trying download/upload...")

        # --- Text-only private message: send via user's own bot DM ---
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
            return msg

        # --- Private/restricted media: download then upload to user's bot DM ---

        # Pre-download file size check — reject before wasting bandwidth
        for m in messages:
            sz = _get_msg_file_size(m)
            if sz and sz > MAX_FILE_SIZE:
                readable = f"{sz / 1_000_000_000:.1f} GB"
                await update_status(status, f"❌ File too large ({readable}). Maximum supported size is 2 GB.")
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
            # --- Album path: parallel download + send_media_group ---
            await update_status(status, f"📥 Downloading album ({len(messages)} files)...")

            download_tasks = [
                download_media(user_client, m, progress=None, progress_args=())
                for m in messages
            ]
            results = await asyncio.gather(*download_tasks, return_exceptions=True)

            # If any album item failed with a dead session, propagate immediately —
            # the outer handler will clear the session and notify the user.
            for result in results:
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
                for m, path in valid_pairs:
                    cap = truncate_caption(m.caption or "")
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
                    for m, path in valid_pairs:
                        cap = truncate_caption(m.caption or "")
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
            # --- Single file: download then upload ---
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

                caption   = truncate_caption(m.caption or "")
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
                raise  # propagate to outer handler — session will be cleared and user notified
            except asyncio.TimeoutError:
                await update_status(status, "❌ Transfer timed out (30 min limit). Please try again.")
            except Exception as e:
                logging.error(f"Download/upload error for user {user_id}: {e}")
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

        return msg

    except (AuthKeyUnregistered, SessionRevoked, SessionExpired,
            AuthKeyInvalid, AuthKeyPermEmpty, UserDeactivated) as e:
        from bot.database import logout_user
        logging.warning(f"Session error for user {user_id}: {type(e).__name__} — session cleared")
        await logout_user(user_id)
        try:
            await update_status(status, "❌ Your Telegram session has expired or was revoked. Please use /login to reconnect.")
        except Exception:
            pass
        return None
    except Exception as e:
        logging.error(f"Handler error for user {user_id}: {e}")
        try:
            await update_status(status, f"❌ Error: {e}")
        except Exception:
            pass
        return None
    finally:
        active_downloads.discard(user_id)
        active_sessions.discard(user_id)
        cancel_flags.discard(user_id)
        if hasattr(progress_bar, "_data") and status:
            progress_bar._data.pop(status.id, None)


# --- Cancel ---

@app.on_message(filters.command("cancel") & filters.private)
async def cancel_handler(client, message):
    user_id = message.from_user.id
    if user_id in active_downloads or user_id in active_sessions:
        cancel_flags.add(user_id)
        await message.reply("🛑 Cancelling current download...")
    else:
        await message.reply("ℹ️ No active download to cancel.")


# --- Commands ---

@app.on_message(filters.command("help") & filters.private)
async def help_command(client, message):
    user_id = message.from_user.id
    user = await get_user(user_id)
    role = (user.get("role", "free") if user else "free")
    is_premium = role in ("premium", "admin", "owner")

    if is_premium:
        text = (
            "📖 **Help**\n\n"
            "🔗 **Public links** — send any public `t.me` link and it's delivered here instantly.\n\n"
            "🔒 **Private / restricted links** — requires two steps:\n"
            " /login — connect your Telegram account\n"
            " /setbot — register your upload bot\n\n"
            "🤖 **Bot commands**\n"
            " /setbot — set or replace your upload bot\n"
            " /rembot — remove your upload bot\n\n"
            "📦 **Batch**\n"
            "`/batch start_link end_link` — download a range\n"
            "`/batch start_link 50` — download next 50\n\n"
            "🔗 **Multi-link**\n"
            " /mlinks — paste up to 50 links at once\n\n"
            " /cancel — stop an active download"
        )
    else:
        text = (
            "📖 **Help**\n\n"
            "🔗 **Public links** — send any public `t.me` link and it's delivered here instantly.\n\n"
            "🔒 **Private / restricted links** — use `/login` to connect your Telegram account.\n\n"
            "💰 **Quota** — 2 files/day · 5 files/month\n\n"
            " /cancel — stop an active download\n\n"
            "💎 **Want unlimited downloads?** → /upgrade"
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
        "• Fast speed\n\n"
        "🔥 **1 Year — $45**\n"
        "• All features + priority support\n\n"
        "💳 **Payment**\n"
        f"🪙 [Crypto / Binance]({CRYPTO_ADDRESS})\n"
        f"🇮🇳 [UPI]({UPI_ID})\n"
        f"💲 [PayPal]({PAYPAL_LINK})\n"
        f"🍎 [Apple Pay]({APPLE_PAY_ID})\n"
        f"💳 [Card]({CARD_PAYMENT_LINK})\n\n"
        "After payment send screenshot to **@Owner_Wolfy**.",
        link_preview_options=LinkPreviewOptions(is_disabled=True),
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Owner", url="https://t.me/Owner_Wolfy")],
            [InlineKeyboardButton("Support", url=SUPPORT_CHAT_LINK)],
        ]),
    )
