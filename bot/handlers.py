import asyncio
import os
import re
import time
import logging
from collections import deque

import pyrogram
from pyrogram import filters, Client, enums
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ChatPrivileges
from pyrogram.errors import FloodWait, FloodPremiumWait, AuthKeyUnregistered, SessionRevoked

from bot.config import (
    app, API_ID, API_HASH,
    active_downloads, global_download_semaphore,
    cancel_flags, batch_cancel_flags, batch_sessions, login_states,
    OWNER_ID, SUPPORT_CHAT_LINK,
)
from bot.database import get_user, check_and_update_quota, get_setting, increment_quota, update_user_channel
from bot.transfer import download_media, upload_media, truncate_caption


# ---------------------------------------------------------------------------
# User session cache
# ---------------------------------------------------------------------------

user_clients: dict = {}
active_sessions: set = set()
_cleanup_started = False

MAX_FLOODWAIT_TOLERATE = 60
_user_floodwait_until: dict = {}
_dest_channel_cache: dict = {}


async def get_user_client(user_id: int, session_str: str) -> Client:
    global _cleanup_started
    now = time.time()

    if user_id in user_clients:
        entry = user_clients[user_id]
        if entry["client"].is_connected:
            entry["last_used"] = now
            return entry["client"]
        try:
            await entry["client"].stop()
        except Exception:
            pass
        del user_clients[user_id]

    if len(user_clients) >= 50:
        idle = [
            (uid, d["last_used"]) for uid, d in user_clients.items()
            if uid not in active_sessions and uid not in batch_sessions
        ]
        if idle:
            oldest = min(idle, key=lambda x: x[1])[0]
            old = user_clients.pop(oldest)
            try:
                await old["client"].stop()
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
    )
    await client.start()
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
                    await entry["client"].stop()
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
                await app.send_message(uid, "⚠️ Login session expired due to inactivity.")
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

async def update_status(msg, text: str):
    if not msg:
        return
    try:
        await msg.edit_text(text)
    except (FloodWait, FloodPremiumWait) as e:
        await asyncio.sleep(min(e.value, 10))
        try:
            await msg.edit_text(text)
        except Exception:
            pass
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
        raise Exception("StopProcess")

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


# ---------------------------------------------------------------------------
# Force subscribe
# ---------------------------------------------------------------------------

async def verify_force_sub(client: Client, user_id: int):
    setting = await get_setting("force_sub_channel")
    if not setting or not setting.get("value"):
        return True, None

    channel = setting["value"]
    if not channel.startswith("@") and not channel.startswith("-100"):
        channel = f"@{channel}"

    try:
        from pyrogram import enums
        member = await client.get_chat_member(channel, user_id)
        if member.status in (enums.ChatMemberStatus.LEFT, enums.ChatMemberStatus.BANNED):
            return False, channel
        return True, None
    except pyrogram.errors.UserNotParticipant:
        return False, channel
    except Exception as e:
        logging.error(f"Force sub check error: {e}")
        return True, None


# ---------------------------------------------------------------------------
# Dump channel
# ---------------------------------------------------------------------------

DUMP_CAPTION_LIMIT = 1024


def _build_dump_caption(original_caption: str, user_id, source_link: str) -> str:
    parts = []
    if user_id:
        parts.append(f"👤 {user_id}")
    if source_link:
        parts.append(f"🔗 {source_link}")
    header = "\n".join(parts)

    original_caption = original_caption or ""
    if not header:
        return original_caption[:DUMP_CAPTION_LIMIT]
    if not original_caption:
        return header[:DUMP_CAPTION_LIMIT]

    sep = "\n\n"
    available = DUMP_CAPTION_LIMIT - len(header) - len(sep)
    if available <= 0:
        return header[:DUMP_CAPTION_LIMIT]
    if len(original_caption) <= available:
        return header + sep + original_caption
    if available <= 3:
        return header[:DUMP_CAPTION_LIMIT]
    return header + sep + original_caption[:available - 3] + "..."


async def send_to_dump(client, msg, user_id=None, source_link=None):
    """Forward a sent message to the configured dump channel, if any."""
    setting = await get_setting("dump_channel_id")
    if not setting or not setting.get("value"):
        return
    dump_id_str = setting["value"]
    try:
        dump_id = int(dump_id_str)
    except (ValueError, TypeError):
        logging.warning(f"send_to_dump: invalid dump_channel_id '{dump_id_str}'")
        return

    if not source_link:
        source_link = getattr(msg, "link", None)

    new_caption = _build_dump_caption(msg.caption or "", user_id, source_link)

    try:
        await client.copy_message(
            dump_id, msg.chat.id, msg.id,
            caption=new_caption,
            parse_mode=enums.ParseMode.DISABLED,
        )
    except Exception as e:
        logging.error(f"send_to_dump error: {e}")


# ---------------------------------------------------------------------------
# Link parsing
# ---------------------------------------------------------------------------

def _parse_story_link(link: str):
    """
    Parse a t.me story link.
    Returns (chat_id, story_id, is_private) or None.

    Supported formats:
      Public story:   t.me/{username}/s/{story_id}
      Private story:  t.me/c/{chat_id}/s/{story_id}
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

    Supported formats:
      Private:            t.me/c/{chat_id}/{msg_id}
      Private topic:      t.me/c/{chat_id}/{topic_id}/{msg_id}
      Public:             t.me/{username}/{msg_id}
      Public topic:       t.me/{username}/{topic_id}/{msg_id}
      + ?comment=N        (specific comment in a linked discussion group)
      + ?thread=N         (message inside a topic thread or channel discussion)
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




# ---------------------------------------------------------------------------
# Core download handler
# ---------------------------------------------------------------------------

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

    if _user_floodwait_until.get(user_id, 0) > time.time():
        wait_left = int(_user_floodwait_until[user_id] - time.time())
        if not link_override:
            await message.reply(f"⏳ Rate limit active. Please try again in {wait_left}s.")
        return None

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

    status = status_msg_override or await message.reply("⏳ Processing...")
    if status is None:
        return None

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
            except (AuthKeyUnregistered, SessionRevoked):
                from bot.database import logout_user
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
            except (AuthKeyUnregistered, SessionRevoked) as e:
                from bot.database import logout_user
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
                    logging.info(f"Comment resolved: user={user_id} disc_chat={disc_chat_id} comment={comment_id}")
            except Exception as e:
                logging.warning(f"Comment resolution failed (comment_id={comment_id}): {e} — falling back to original post")

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
                        logging.info(f"Thread resolved: user={user_id} disc_chat={disc_chat_id} msg={msg_id}")
            except Exception as e:
                logging.info(f"Thread resolution (thread={thread_id}): {e} — using direct message fetch")

        if not msg or not getattr(msg, "media", None):
            await update_status(status, "❌ No downloadable media found at this link.")
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

        if not is_private:
            media_group_id = getattr(msg, "media_group_id", None)
            try:
                await update_status(status, "🚀 Extracting directly...")
                if media_group_id:
                    await client.copy_media_group(chat_id=user_id, from_chat_id=chat_id, message_id=msg_id)
                else:
                    await msg.copy(chat_id=user_id)
                await send_to_dump(client, msg, user_id=user_id, source_link=getattr(msg, "link", None))
                if not skip_quota and user.get("role", "free") == "free":
                    await increment_quota(user_id)
                if not status_msg_override:
                    try:
                        await status.delete()
                    except Exception:
                        pass
                active_downloads.discard(user_id)
                return msg
            except (AuthKeyUnregistered, SessionRevoked):
                raise
            except (FloodWait, FloodPremiumWait) as e:
                wait_secs = e.value
                logging.warning(f"FloodWait {wait_secs}s on direct extraction for user {user_id}")
                if wait_secs > MAX_FLOODWAIT_TOLERATE:
                    _user_floodwait_until[user_id] = time.time() + wait_secs
                    await update_status(status, f"⏳ Rate limit hit ({wait_secs}s). Please try again later.")
                    return None
                await asyncio.sleep(wait_secs + 2)
                return None
            except Exception as e:
                error_str = str(e)
                if "USER_IS_BLOCKED" in error_str:
                    return None
                if "MEDIA_CAPTION_TOO_LONG" in error_str:
                    try:
                        if media_group_id:
                            await client.copy_media_group(chat_id=user_id, from_chat_id=chat_id, message_id=msg_id, captions="")
                        else:
                            await msg.copy(chat_id=user_id, caption="")
                        await send_to_dump(client, msg, user_id=user_id, source_link=getattr(msg, "link", None))
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
                logging.error(f"Direct extraction failed: {e}")
                await update_status(status, "⚠️ Direct extraction failed, falling back to download/upload...")

        upload_client = client
        destination_id = user_id
        _using_user_channel = False

        if user_client and user_client != client:
            if user_id in _dest_channel_cache:
                destination_id, _using_user_channel = _dest_channel_cache[user_id]
                if _using_user_channel:
                    upload_client = user_client
            else:
                channel_id = user.get("download_channel_id")

                if channel_id == "saved_messages":
                    destination_id = "me"
                    _using_user_channel = True
                    upload_client = user_client

                else:
                    if channel_id and user_client.is_connected:
                        try:
                            channel_id = int(channel_id)
                            try:
                                await client.get_chat(channel_id)
                            except Exception:
                                me = await client.get_me()
                                bot_id = f"@{me.username}" if me.username else me.id
                                try:
                                    await user_client.add_chat_members(channel_id, me.id)
                                except Exception:
                                    pass
                                try:
                                    await user_client.promote_chat_member(
                                        channel_id, bot_id,
                                        privileges=ChatPrivileges(
                                            can_post_messages=True,
                                            can_delete_messages=True,
                                            can_invite_users=True,
                                            can_manage_chat=True,
                                        ),
                                    )
                                except Exception as re_e:
                                    logging.warning(f"Re-add bot to channel {channel_id} failed: {re_e}")
                                    channel_id = None
                        except Exception as e:
                            logging.warning(f"Channel check failed for user {user_id}: {e}")
                            channel_id = None
                    else:
                        channel_id = None

                    if not channel_id:
                        try:
                            new_chat = await user_client.create_channel(
                                "Cloud Storage", "My private cloud storage for downloads."
                            )
                            channel_id = new_chat.id
                            await update_user_channel(user_id, channel_id)
                            me = await client.get_me()
                            await user_client.promote_chat_member(
                                channel_id,
                                f"@{me.username}" if me.username else me.id,
                                privileges=ChatPrivileges(
                                    can_post_messages=True,
                                    can_delete_messages=True,
                                    can_invite_users=True,
                                    can_restrict_members=True,
                                    can_pin_messages=True,
                                    can_change_info=True,
                                    can_promote_members=False,
                                ),
                            )
                            logging.info(f"Created private channel {channel_id} for user {user_id}")
                        except Exception as e:
                            if "CHANNELS_TOO_MUCH" in str(e):
                                await update_user_channel(user_id, "saved_messages")
                                logging.warning(f"User {user_id} has too many channels — falling back to saved messages")
                                destination_id = "me"
                            else:
                                logging.error(f"Failed to create channel for user {user_id}: {e}")
                            upload_client = user_client
                            _using_user_channel = True
                            channel_id = None

                    if channel_id:
                        upload_client = user_client
                        destination_id = channel_id
                        _using_user_channel = True

                _dest_channel_cache[user_id] = (destination_id, _using_user_channel)

        for m in messages:
            if user_id in cancel_flags:
                cancel_flags.discard(user_id)
                await update_status(status, "🛑 Cancelled.")
                return None

            path = None
            thumb = None
            try:
                path = await download_media(
                    user_client, m,
                    progress=progress_bar,
                    progress_args=(status, "📥 Downloading"),
                )
                if not path:
                    continue

                if user_id in cancel_flags:
                    cancel_flags.discard(user_id)
                    await update_status(status, "🛑 Cancelled.")
                    return None

                caption = truncate_caption(m.caption or "")
                duration = width = height = 0
                file_name = None

                if m.video:
                    duration = m.video.duration or 0
                    width = m.video.width or 0
                    height = m.video.height or 0
                    file_name = m.video.file_name
                    if m.video.thumbs:
                        try:
                            thumb = await user_client.download_media(m.video.thumbs[-1], in_memory=True)
                        except Exception:
                            pass
                elif getattr(m, "document", None):
                    file_name = m.document.file_name
                elif getattr(m, "audio", None):
                    duration = m.audio.duration or 0
                    file_name = m.audio.file_name

                _fallback_client = client if upload_client is not client else None
                _fallback_chat = user_id if destination_id == "me" else None

                sent_msg = await upload_media(
                    upload_client, destination_id, path,
                    caption=caption,
                    thumb=thumb,
                    file_name=file_name,
                    duration=duration,
                    width=width,
                    height=height,
                    progress=progress_bar,
                    progress_args=(status, "📤 Uploading"),
                    fallback_client=_fallback_client,
                    fallback_chat_id=_fallback_chat,
                )

                if sent_msg and _using_user_channel and status_msg_override is None:
                    msg_link = getattr(sent_msg, "link", None)
                    conf = (
                        f"✅ **Saved to your channel**\n[View file]({msg_link})"
                        if msg_link else "✅ File saved to your download channel."
                    )
                    try:
                        await client.send_message(user_id, conf, disable_web_page_preview=True)
                    except Exception:
                        pass

                if sent_msg:
                    await send_to_dump(
                        client, sent_msg,
                        user_id=user_id,
                        source_link=getattr(m, "link", None),
                    )

            except Exception as e:
                if str(e) == "StopProcess":
                    cancel_flags.discard(user_id)
                    await update_status(status, "🛑 Cancelled.")
                    return None
                logging.error(f"Download/upload error for user {user_id}: {e}")
            finally:
                if path and os.path.exists(path):
                    os.remove(path)

        if not skip_quota and user.get("role", "free") == "free":
            await increment_quota(user_id)

        if not status_msg_override:
            try:
                await status.delete()
            except Exception:
                pass

        return msg

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


# ---------------------------------------------------------------------------
# Cancel
# ---------------------------------------------------------------------------

@app.on_message(filters.command("cancel") & filters.private)
async def cancel_handler(client, message):
    user_id = message.from_user.id
    if user_id in active_downloads or user_id in active_sessions:
        cancel_flags.add(user_id)
        await message.reply("🛑 Cancelling current download...")
    else:
        await message.reply("ℹ️ No active download to cancel.")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

@app.on_message(filters.command("help") & filters.private)
async def help_command(client, message):
    await message.reply(
        "📖 **Help**\n\n"
        "🔗 **Download**\n"
        "Send any t.me link to download.\n"
        "For private/restricted links, use /login first.\n\n"
        "📦 **Batch** _(Premium)_\n"
        "`/batch start_link end_link` — range\n"
        "`/batch start_link 50` — count mode\n"
        "Max 50 files per batch.\n\n"
        "🔗 **Multi-link** _(Premium)_\n"
        "`/mlinks` then paste up to 50 links, one per line.\n\n"
        "💰 **Quota** _(Free)_\n"
        "5 files/day · 15 files/month\n"
        "Premium: unlimited.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Owner", url="https://t.me/Owner_Wolfy")],
            [InlineKeyboardButton("Support", url=SUPPORT_CHAT_LINK)],
        ]),
    )


@app.on_message(filters.command("upgrade") & filters.private)
async def upgrade_command(client, message):
    from bot.config import UPI_ID, PAYPAL_LINK, APPLE_PAY_ID, CRYPTO_ADDRESS, CARD_PAYMENT_LINK
    await message.reply(
        "💎 **Premium Plans**\n\n"
        "⚡ **Standard**\n"
        "🔸 10 days — $2\n"
        "🔸 30 days — $3\n"
        "🔸 60 days — $6\n\n"
        "• Unlimited downloads\n"
        "• Batch up to 50 files\n"
        "• Multi-link up to 50\n"
        "• Fast speed\n\n"
        "🔥 **1 Year — $30**\n"
        "• All features + priority support\n\n"
        "💳 **Payment**\n"
        f"🪙 [Crypto / Binance]({CRYPTO_ADDRESS})\n"
        f"🇮🇳 [UPI]({UPI_ID})\n"
        f"💲 [PayPal]({PAYPAL_LINK})\n"
        f"🍎 [Apple Pay]({APPLE_PAY_ID})\n"
        f"💳 [Card]({CARD_PAYMENT_LINK})\n\n"
        "After payment send screenshot to **@Owner_Wolfy**.",
        disable_web_page_preview=True,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Owner", url="https://t.me/Owner_Wolfy")],
            [InlineKeyboardButton("Support", url=SUPPORT_CHAT_LINK)],
        ]),
    )
