import asyncio
import os
import re
import time
import logging
from collections import deque

import pyrogram
from pyrogram import filters, Client, enums
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.errors import FloodWait, FloodPremiumWait, AuthKeyUnregistered, SessionRevoked

from bot.config import (
    app, API_ID, API_HASH,
    active_downloads, global_download_semaphore,
    cancel_flags, batch_sessions, login_states,
    SUPPORT_CHAT_LINK,
)
from bot.database import get_user, check_and_update_quota, get_setting, increment_quota
from bot.transfer import download_media, upload_media, truncate_caption, get_user_bot


# --- User session cache ---

user_clients: dict = {}
active_sessions: set = set()
_cleanup_started = False

MAX_FLOODWAIT_TOLERATE = 60
_user_floodwait_until: dict = {}

FREE_DOWNLOAD_COOLDOWN = 15 * 60  # 15 minutes in seconds
_user_last_download_time: dict = {}


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


# --- Utilities ---

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

    if user.get("role") not in ("admin", "owner"):
        mm = await get_setting("maintenance_mode")
        if mm and mm.get("value") == "on":
            if not link_override:
                await message.reply(
                    "🔧 **Bot is under maintenance.**\n\n"
                    "We'll be back shortly. Please try again later."
                )
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

        last_dl = _user_last_download_time.get(user_id, 0)
        elapsed = time.time() - last_dl
        if elapsed < FREE_DOWNLOAD_COOLDOWN:
            wait_left = int(FREE_DOWNLOAD_COOLDOWN - elapsed)
            mins, secs = divmod(wait_left, 60)
            wait_str = f"{mins}m {secs}s" if mins else f"{secs}s"
            if not link_override:
                await message.reply(
                    f"⏳ **Please wait {wait_str}** before your next download.\n\n"
                    f"Free users must wait **15 minutes** between downloads.\n\n"
                    f"💎 Upgrade to **Premium** for unlimited downloads with no waiting time.\n"
                    f"👉 /upgrade"
                )
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
            except (AuthKeyUnregistered, SessionRevoked):
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

        if not msg:
            await update_status(status, "❌ Message not found or not accessible.")
            return None

        has_media = bool(getattr(msg, "media", None))
        has_text = bool(getattr(msg, "text", None))

        # Bots can't read public groups — fall back to user session if available
        if not has_media and not has_text and not is_private and user.get("phone_session_string"):
            try:
                user_client = await get_user_client(user_id, user["phone_session_string"])
                active_sessions.add(user_id)
                msg = await user_client.get_messages(chat_id, msg_id, replies=0)
                is_private = True
                has_media = bool(getattr(msg, "media", None))
                has_text = bool(getattr(msg, "text", None))
            except (AuthKeyUnregistered, SessionRevoked):
                from bot.database import logout_user
                await logout_user(user_id)
                await update_status(status, "❌ Your session expired. Please /login again.")
                return None
            except Exception as e:
                logging.warning(f"Public group fallback failed: {e}")

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

        # --- Public content: server-side copy via user's own bot to their DM ---
        if not is_private:
            upload_bot = await get_user_bot(user_id)
            if upload_bot is None:
                await update_status(
                    status,
                    "❌ You need a personal upload bot to receive files.\n\n"
                    "1. Open @BotFather → `/newbot`\n"
                    "2. copy the bot_token (e.g. `123456789:AABbCc...`)\n"
                    "3. Run `/setbot bot_token` here\n"
                    "4. Press **Start** on your bot\n\n"
                    "All files are delivered directly to your bot's DM.",
                )
                active_downloads.discard(user_id)
                return None

            media_group_id = getattr(msg, "media_group_id", None)
            try:
                await update_status(status, "🚀 Extracting directly...")
                if media_group_id:
                    await upload_bot.copy_media_group(
                        chat_id=user_id, from_chat_id=chat_id, message_id=msg_id
                    )
                else:
                    await upload_bot.copy_message(
                        chat_id=user_id, from_chat_id=chat_id, message_id=msg_id
                    )
                if not skip_quota and user.get("role", "free") == "free":
                    await increment_quota(user_id)
                    _user_last_download_time[user_id] = time.time()
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
                if "USER_IS_BLOCKED" in error_str or "BotStartCommandMissing" in error_str:
                    bot_url = None
                    try:
                        me = await upload_bot.get_me()
                        if me.username:
                            bot_url = f"https://t.me/{me.username}?start=start"
                    except Exception:
                        pass
                    markup = None
                    if bot_url:
                        markup = InlineKeyboardMarkup([
                            [InlineKeyboardButton("▶️ Start My Bot", url=bot_url)]
                        ])
                    await update_status(
                        status,
                        "❌ **Your bot couldn't send the file to you.**\n\n"
                        "You haven't started your bot yet. "
                        "Tap the button below, press **Start**, then resend the link.",
                        reply_markup=markup,
                    )
                    active_downloads.discard(user_id)
                    return None
                if "MEDIA_CAPTION_TOO_LONG" in error_str:
                    try:
                        if media_group_id:
                            await upload_bot.copy_media_group(
                                chat_id=user_id, from_chat_id=chat_id,
                                message_id=msg_id, captions=""
                            )
                        else:
                            await upload_bot.copy_message(
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
                logging.error(f"Direct extraction failed: {e}")
                await update_status(status, "⚠️ Direct extraction failed, trying download/upload...")

        # --- Text-only private message: send via user's own bot DM ---
        if not has_media:
            text = getattr(msg, "text", None) or ""
            entities = getattr(msg, "entities", None) or []
            upload_bot = await get_user_bot(user_id)
            sender = upload_bot if upload_bot is not None else client
            try:
                await update_status(status, "✍️ Copying text message...")
                await sender.send_message(
                    user_id, text,
                    entities=entities,
                    disable_web_page_preview=False,
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

                upload_client = await get_user_bot(user_id)
                if upload_client is None:
                    await update_status(
                        status,
                        "❌ You haven't registered an upload bot yet.\n\n"
                        "1. Open @BotFather → `/newbot`\n"
                        "2. copy the bot_token (e.g. `123456789:AABbCc...`)\n"
                        "3. Run `/setbot bot_token` here\n"
                        "4. Press **Start** on your bot",
                    )
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

                await upload_media(
                    upload_client, user_id, path,
                    caption=caption,
                    thumb=thumb,
                    file_name=file_name,
                    duration=duration,
                    width=width,
                    height=height,
                    progress=progress_bar,
                    progress_args=(status, "📤 Uploading"),
                )

            except Exception as e:
                if str(e) == "StopProcess":
                    cancel_flags.discard(user_id)
                    await update_status(status, "🛑 Cancelled.")
                    return None
                error_str = str(e)
                if "USER_IS_BLOCKED" in error_str or "BotStartCommandMissing" in error_str:
                    bot_url = None
                    try:
                        me = await upload_client.get_me()
                        if me.username:
                            bot_url = f"https://t.me/{me.username}?start=start"
                    except Exception:
                        pass
                    markup = None
                    if bot_url:
                        markup = InlineKeyboardMarkup([
                            [InlineKeyboardButton("▶️ Start My Bot", url=bot_url)]
                        ])
                    await update_status(
                        status,
                        "❌ **Your bot couldn't send the file to you.**\n\n"
                        "You haven't started your bot yet. "
                        "Tap the button below, press **Start**, then resend the link.",
                        reply_markup=markup,
                    )
                    return None
                logging.error(f"Download/upload error for user {user_id}: {e}")
            finally:
                if path and os.path.exists(path):
                    os.remove(path)

        if not skip_quota and user.get("role", "free") == "free":
            await increment_quota(user_id)
            _user_last_download_time[user_id] = time.time()

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
    await message.reply(
        "📖 **Help**\n\n"
        "🤖 **First-time setup**\n"
        "📹 [Watch the full setup guide](https://t.me/Wolfy004/155) to get started quickly.\n\n"
        "1. `/setbot bot_token` — register your own @BotFather bot. "
        "Your bot delivers files directly to your DM.\n"
        "2. `/login` — connect your Telegram account (only needed for "
        "private/restricted links).\n\n"
        "🔗 **Download**\n"
        "Send any t.me link. Both public and private links are sent "
        "straight to your bot's DM.\n\n"
        "🤖 **Bot management**\n"
        "`/setbot bot_token` — set or replace your upload bot\n"
        "`/rembot` — remove your upload bot\n\n"
        "📦 **Batch** _(Premium)_\n"
        "`/batch start_link end_link` — range\n"
        "`/batch start_link 50` — count mode\n"
        "Max 50 files per batch.\n\n"
        "🔗 **Multi-link** _(Premium)_\n"
        "`/mlinks` then paste up to 50 links, one per line.\n\n"
        "💰 **Quota**\n"
        "Free: 2 files/day · 5 files/month\n"
        "Premium: **unlimited**.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Owner", url="https://t.me/Owner_Wolfy")],
            [InlineKeyboardButton("Support", url=SUPPORT_CHAT_LINK)],
        ]),
        disable_web_page_preview=True,
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
        disable_web_page_preview=True,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Owner", url="https://t.me/Owner_Wolfy")],
            [InlineKeyboardButton("Support", url=SUPPORT_CHAT_LINK)],
        ]),
    )
