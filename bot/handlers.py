import asyncio
import gc
import os
import time
import io
import sqlite3
import aiofiles
import re
import logging
from collections import deque
from urllib.parse import urlparse, parse_qs
import pyrogram
from pyrogram import filters, Client
from pyrogram.client import Client as ClientObject
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, Message, LinkPreviewOptions, WebPage
from pyrogram.errors import AuthKeyUnregistered, FloodWait, FloodPremiumWait, SessionRevoked
from pyrogram.errors.exceptions.unauthorized_401 import AuthKeyUnregistered as AuthKeyUnregistered401
from pyrogram.errors.exceptions.bad_request_400 import (
    FileReferenceExpired,
    FileReferenceInvalid,
    AuthBytesInvalid,
)
from bot.config import (
    app, API_ID, API_HASH, active_downloads, global_download_semaphore,
    OWNER_ID, cancel_flags, batch_cancel_flags, login_states,
    SUPPORT_CHAT_LINK, OWNER_USERNAME
)

MAX_FLOODWAIT_TOLERATE = 60

# Rate-limit get_messages calls made through the shared bot client (public links).
# All users share the same bot account → one burst can trigger a server-side
# FloodWait that blocks ALL users simultaneously. The lock serialises these
# calls and the sleep keeps the rate well under Telegram's threshold.
_bot_client_lock = asyncio.Lock()
_BOT_FETCH_DELAY = 1.2  # seconds to sleep after each bot-client get_messages call

async def _get_messages_rate_limited(client_to_use, bot_client, chat_id, message_id):
    """Wrap get_messages so bot-client calls are serialised and rate-limited.
    User-client calls pass through with no delay — each user has their own
    Telegram account with its own independent rate-limit quota."""
    if client_to_use is bot_client:
        async with _bot_client_lock:
            result = await client_to_use.get_messages(chat_id, message_id, replies=0)
            await asyncio.sleep(_BOT_FETCH_DELAY)
            return result
    return await client_to_use.get_messages(chat_id, message_id, replies=0)

async def safe_reply(message, text, **kwargs):
    """Reply with automatic retry on short FloodWaits. Returns None on long waits.
    Also records per-user FloodWait cooldown so handlers can skip future requests."""
    user_id = getattr(getattr(message, "from_user", None), "id", None)
    for attempt in range(3):
        try:
            return await message.reply(text, **kwargs)
        except (FloodWait, FloodPremiumWait) as e:
            wait = e.value
            logging.warning(f"FloodWait {wait}s on reply (attempt {attempt+1})")
            if wait <= MAX_FLOODWAIT_TOLERATE and attempt < 2:
                await asyncio.sleep(wait)
            else:
                logging.error(f"FloodWait too long ({wait}s) — skipping reply")
                if user_id:
                    _user_floodwait_until[user_id] = time.time() + wait
                    logging.error(f"Could not send processing message to user {user_id} — FloodWait too long")
                return None
        except Exception as e:
            logging.error(f"safe_reply error: {e}")
            return None

# Dump channel 
async def send_to_dump(client, user_id, link, msg):
    """Fetches dump channel from database and sends a copy"""
    return #Remove this line for dump channel activation.
    # 1. Fetch the setting from Database
    from bot.database import get_setting
    res = await get_setting("dump_channel_id")
    dump_id = res.get("value") if res else None

    if not dump_id:
        logging.warning("Dump channel not set.")
        return

    try:
        # Ensure the ID is an integer for Telegram
        try:
            dump_id = int(dump_id)
        except ValueError:
            logging.error(f"Invalid dump_id format in database: {dump_id}")
            return
        
        # Verify bot access to dump channel
        try:
            # We use get_chat to "resolve" the peer. 
            # If this fails with CHANNEL_INVALID, it means the bot doesn't know this ID yet
            await client.get_chat(dump_id)
        except (pyrogram.errors.ChannelInvalid, pyrogram.errors.PeerIdInvalid):
            logging.error(f"Bot cannot access dump channel {dump_id}. Ensure the bot is an ADMIN in that channel and has been added to it.")
            return
        except Exception as e:
            logging.warning(f"Error resolving dump channel {dump_id}: {e}")
        
        # 2. Create the Header
        header = f"👤 **User:** `{user_id}`\n🔗 **Link:** {link}\n\n"
        original_caption = msg.text or msg.caption or ""
        
        # Check if message is actually empty (can happen with deleted/restricted messages)
        if not original_caption and not msg.media and type(msg).__name__ != "Story":
            logging.warning(f"Empty message {msg.id} cannot be dumped.")
            return
            
        # If it's a story, text/caption might be None
        if not original_caption and type(msg).__name__ == "Story":
             original_caption = "Story Media"
        
        full_caption = truncate_caption(header + original_caption)

        # Ensure bot has access by trying to resolve the peer first if needed, 
        # but copy() usually works if the ID is known and valid.
        
        if msg.media_group_id:
            # For albums
            try:
                await client.copy_media_group(dump_id, msg.chat.id, msg.id)
            except Exception as e:
                logging.error(f"Main bot copy_media_group failed: {e}")
                # Fallback: if main bot can't copy (e.g. not admin), try with user_client if session exists
                user_client = user_clients.get(user_id, {}).get("client")
                if user_client:
                    logging.info(f"Trying copy_media_group with user client for user {user_id}")
                    await user_client.copy_media_group(dump_id, msg.chat.id, msg.id)
                else:
                    raise
            try:
                await client.send_message(dump_id, header + "⚠️ Album above)")
            except Exception:
                user_client = user_clients.get(user_id, {}).get("client")
                if user_client:
                    await user_client.send_message(dump_id, header + "⚠️ Album above)")
        else:
            # For single files — prefer send_cached_media (zero re-upload) when a file_id is available
            file_id = None
            if msg.media:
                for attr in ("document", "video", "audio", "voice", "video_note", "animation", "sticker", "photo"):
                    media_obj = getattr(msg, attr, None)
                    if media_obj:
                        fid = getattr(media_obj, "file_id", None)
                        if not fid and attr == "photo" and isinstance(media_obj, list) and media_obj:
                            fid = getattr(media_obj[-1], "file_id", None)
                        if fid:
                            file_id = fid
                            break

            try:
                if file_id:
                    await client.send_cached_media(dump_id, file_id, caption=full_caption)
                elif msg.text:
                    await client.send_message(dump_id, full_caption)
                elif type(msg).__name__ == "Story":
                    await client.copy_message(dump_id, msg.chat.id, msg.id, caption=full_caption)
                else:
                    logging.warning(f"Skipping dump for message {msg.id} as it has no media or text")
            except Exception as e:
                logging.error(f"Main bot dump failed: {e}")
                if "MESSAGE_ID_INVALID" in str(e) or "EMPTY" in str(e) or "MESSAGE_EMPTY" in str(e):
                    return
                user_client = user_clients.get(user_id, {}).get("client")
                if user_client:
                    logging.info(f"Trying dump with user client for user {user_id}")
                    if file_id:
                        await user_client.send_cached_media(dump_id, file_id, caption=full_caption)
                    elif msg.text:
                        await user_client.send_message(dump_id, full_caption)
                    else:
                        await user_client.copy_message(dump_id, msg.chat.id, msg.id, caption=full_caption)
                else:
                    raise
            
    except pyrogram.errors.exceptions.bad_request_400.PeerIdInvalid:
        logging.error(f"Dump failed: PeerIdInvalid. Make sure the bot is an admin in the dump channel (ID: {dump_id})")
    except Exception as e:
        logging.error(f"Dump failed: {e}")
        
# Session caching dictionary: {user_id: {"client": Client, "last_used": timestamp}}
user_clients = {}
active_sessions = set() # Track sessions currently in use (per-item level)
_batch_sessions = set() # Track users mid-batch — held for the entire batch duration
_batch_session_error_flags = set() # Set when a fatal session error occurs mid-batch; signals the batch loop to abort
_cleanup_task_started = False
_cleanup_cycle = 0  # Counts cleanup iterations; used to schedule infrequent sub-tasks

# Cache for get_chat results keyed by chat_id to avoid repeated API calls
# e.g. during a batch of 50 items from the same channel
_chat_type_cache: dict = {}

# Cache for resolved upload destinations keyed by user_id.
# Stores (destination_id, using_user_session) so channel verification
# (get_chat × 2 + get_user) only happens once per session, not once per batch item.
_dest_channel_cache: dict = {}

# Per-user FloodWait cooldown: {user_id: unix_timestamp_when_cooldown_expires}
# When a user triggers a large FloodWait we stop processing their messages
# until Telegram lifts the ban, preventing a storm of repeated failed replies.
_user_floodwait_until: dict = {}

# Per-user rate limiting: {user_id: last_request_timestamp}
_user_last_request: dict = {}
RATE_LIMIT_SECONDS = 120

async def get_user_client(user_id, session_str):
    global _cleanup_task_started
    now = time.time()
    
    if user_id in user_clients:
        cached = user_clients[user_id]
        client = cached["client"]
        idle_secs = now - cached["last_used"]

        # Telegram silently closes idle TCP sockets after ~60-120s.
        # is_connected only checks an internal flag — it cannot detect
        # a server-side close. Evict any client idle for over 90s so the
        # next call always starts on a freshly opened socket.
        if idle_secs <= 90 and client.is_connected:
            cached["last_used"] = now
            return client

        # Never evict a session that has active parallel workers running —
        # stopping the client closes its SQLite storage, causing
        # sqlite3.ProgrammingError in any in-flight get_file() calls.
        if user_id in active_sessions or user_id in _batch_sessions:
            cached["last_used"] = now
            return client

        # Idle too long or already disconnected — tear down and rebuild.
        try:
            await client.stop()
        except Exception:
            pass
        del user_clients[user_id]

    # Evict oldest idle session if cache is full (cap at 50 concurrent sessions)
    MAX_USER_SESSIONS = 50
    if len(user_clients) >= MAX_USER_SESSIONS:
        idle = [(uid, d["last_used"]) for uid, d in user_clients.items() if uid not in active_sessions]
        if idle:
            oldest_uid = min(idle, key=lambda x: x[1])[0]
            old = user_clients.pop(oldest_uid, None)
            if old:
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
        no_joined_notifications=True,
        max_message_cache_size=100,
        max_concurrent_transmissions=4
    )
    await client.start()
    user_clients[user_id] = {"client": client, "last_used": now}

    if not _cleanup_task_started:
        asyncio.create_task(cleanup_user_clients())
        _cleanup_task_started = True
    return client

async def cleanup_user_clients():
    global _cleanup_cycle
    while True:
        await asyncio.sleep(120)
        now = time.time()
        _cleanup_cycle += 1

        # ── 1. User client session eviction (every 120s) ─────────────────────
        to_remove = []
        for user_id, data in user_clients.items():
            if user_id in active_sessions or user_id in _batch_sessions:
                data["last_used"] = now
                continue
            if now - data["last_used"] > 600:
                to_remove.append(user_id)

        for user_id in to_remove:
            if user_id in active_sessions or user_id in _batch_sessions:
                continue
            data = user_clients.pop(user_id, None)
            if data:
                try:
                    await data["client"].stop()
                except Exception:
                    pass

        # ── 2. Rate-limit / FloodWait dict TTL eviction (every 120s) ─────────
        _TTL = 3600
        for uid in [u for u, ts in _user_last_request.items() if now - ts > _TTL]:
            _user_last_request.pop(uid, None)
        for uid in [u for u, dl in _user_floodwait_until.items() if now > dl]:
            _user_floodwait_until.pop(uid, None)

        # ── 3. Cache size caps (every 120s) ───────────────────────────────────
        if len(_dest_channel_cache) > 500:
            for uid in list(_dest_channel_cache.keys())[:250]:
                _dest_channel_cache.pop(uid, None)
        if len(_chat_type_cache) > 500:
            for key in list(_chat_type_cache.keys())[:250]:
                _chat_type_cache.pop(key, None)

        # ── 4. Login session expiry (every 120s) ──────────────────────────────
        expired_logins = [
            uid for uid, state in login_states.items()
            if now - state.get("timestamp", 0) > 300
        ]
        for uid in expired_logins:
            state = login_states.pop(uid, None)
            if state and "client" in state:
                try:
                    await state["client"].stop()
                except Exception:
                    try:
                        await state["client"].disconnect()
                    except Exception:
                        pass
            try:
                await app.send_message(uid, "⚠️ Login session expired due to inactivity.")
            except Exception:
                pass

        # ── 5. Python GC + orphan-file sweep (every 15 cycles = ~30 min) ────────
        if _cleanup_cycle % 15 == 0:
            gc.collect()
            try:
                import psutil
                mem = psutil.Process().memory_info().rss / 1024 / 1024
                logging.info(f"Scheduled GC: current RSS {mem:.1f} MB")
            except Exception:
                pass

            # Remove any file in downloads/ that is older than 2 h — these are
            # partial chunks left behind when a download was cancelled or crashed
            # mid-transfer (CancelledError bypasses the normal finally cleanup).
            _dl_dir = "downloads"
            _stale_cutoff = now - 7200  # 2 hours
            try:
                _removed = 0
                for _fname in os.listdir(_dl_dir):
                    _fpath = os.path.join(_dl_dir, _fname)
                    try:
                        if os.path.isfile(_fpath) and os.path.getmtime(_fpath) < _stale_cutoff:
                            os.remove(_fpath)
                            _removed += 1
                    except Exception:
                        pass
                if _removed:
                    logging.info(f"Orphan-file sweep: removed {_removed} stale file(s) from {_dl_dir}/")
            except Exception:
                pass

from bot.database import get_user, check_and_update_quota, increment_quota, get_setting, get_remaining_quota, update_user_channel
from bot.transfer import download_media_fast, download_media_parallel, upload_media_fast, truncate_caption, _FileRefSwallowedByPyrogram

async def update_status(msg, text):
    """
    Edit a status message with three key optimisations:
      1. Skip the API call entirely when text is identical to what was last sent
         (avoids wasted RTT and MESSAGE_NOT_MODIFIED errors).
      2. On FloodWait, back off and retry once instead of dropping the update.
      3. Silently ignore MESSAGE_NOT_MODIFIED even if dedup misses it.
    """
    if not msg:
        return

    cache = getattr(update_status, "_cache", None)
    if cache is None:
        update_status._cache = {}
        cache = update_status._cache

    if cache.get(msg.id) == text:
        return  # identical — no API call needed

    try:
        await msg.edit_text(text)
        cache[msg.id] = text
    except (FloodWait, FloodPremiumWait) as e:
        wait = min(e.value, 8)
        logging.debug(f"edit_text FloodWait {e.value}s — backing off {wait}s")
        await asyncio.sleep(wait)
        try:
            await msg.edit_text(text)
            cache[msg.id] = text
        except Exception:
            pass
    except Exception as e:
        if "MESSAGE_NOT_MODIFIED" not in str(e):
            logging.debug(f"Status update failed: {e}")


def _format_size(size):
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if size < 1024.0:
            return f"{size:.2f} {unit}"
        size /= 1024.0
    return f"{size:.2f} TB"


def _format_time(seconds):
    if seconds <= 0:
        return "0s"
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours > 0:
        return f"{hours}h {minutes}m {secs}s"
    if minutes > 0:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


async def progress_bar(current, total, message, type_msg, show_complete=True):
    """
    Progress bar with three improvements over the original:

    1. Rolling speed window (last 8 seconds of samples) instead of
       average-since-start.  Gives accurate real-time speed/ETA even
       when the transfer ramps up mid-way (e.g. parallel downloader).

    2. Adaptive update interval:
         • ≤ 20 MB  → update every 1.5 s  (fast files — users see movement)
         • > 20 MB  → update every 2.5 s  (large files — reduce API spam)
       Combined with update_status deduplication, this generates the
       minimum number of actual Telegram API calls.

    3. 20-step progress bar instead of 10 — each block = 5% instead of 10%.
    """
    if not hasattr(progress_bar, "data"):
        progress_bar.data = {}
        progress_bar.last_cleanup = time.time()

    now_cleanup = time.time()
    if now_cleanup - getattr(progress_bar, "last_cleanup", 0) > 300:
        cutoff = now_cleanup - 600
        stale = [mid for mid, d in progress_bar.data.items() if d.get("start_time", now_cleanup) < cutoff]
        for mid in stale:
            progress_bar.data.pop(mid, None)
        progress_bar.last_cleanup = now_cleanup

    user_id = message.chat.id
    if user_id in cancel_flags:
        progress_bar.data.pop(message.id, None)
        raise Exception("StopProcess")

    if total == 0:
        return

    now = time.time()
    msg_id = message.id

    if msg_id not in progress_bar.data:
        progress_bar.data[msg_id] = {
            "start_time": now,
            "last_edit": 0,
            "samples": deque(maxlen=40),  # (timestamp, bytes_received)
        }

    data = progress_bar.data[msg_id]

    # Record sample every call — used for rolling-window speed
    data["samples"].append((now, current))

    # Adaptive throttle
    update_interval = 1.5 if total <= 20 * 1024 * 1024 else 2.5
    if now - data["last_edit"] < update_interval and current < total:
        return

    percentage = current * 100 / total

    # Rolling speed: bytes transferred in the last 8 seconds
    rolling_speed = 0.0
    samples = data["samples"]
    if len(samples) >= 2:
        window_start = now - 8.0
        old = None
        for s in samples:
            if s[0] >= window_start:
                old = s
                break
        if old and (now - old[0]) > 0.3:
            dt = now - old[0]
            db = current - old[1]
            if db > 0:
                rolling_speed = db / dt

    # Fallback to overall average when rolling window is too thin
    if rolling_speed <= 0:
        elapsed = now - data["start_time"]
        rolling_speed = current / elapsed if elapsed > 0 else 0

    eta = (total - current) / rolling_speed if rolling_speed > 0 else 0

    bar_len = 20
    filled = int(percentage / 100 * bar_len)
    bar = "█" * filled + "░" * (bar_len - filled)

    text = (
        f"**{type_msg}**\n"
        f"`[{bar}]` **{percentage:.1f}%**\n"
        f"⚡ **Speed:** `{_format_size(rolling_speed)}/s`\n"
        f"⏳ **ETA:** `{_format_time(eta)}`\n"
        f"📦 `{_format_size(current)} / {_format_size(total)}`"
    )

    if current >= total:
        progress_bar.data.pop(msg_id, None)
        if show_complete:
            await update_status(message, f"✅ **{type_msg} complete** — `{_format_size(total)}`")
    else:
        data["last_edit"] = now
        await update_status(message, text)

async def verify_force_sub(client, user_id):
    setting = await get_setting("force_sub_channel")
    if not setting or not setting.get('value'):
        return True, None

    channel = setting['value']
    if not channel.startswith("@") and not channel.startswith("-100"):
        channel = f"@{channel}"

    try:
        from pyrogram import enums as _enums
        member = await client.get_chat_member(channel, user_id)
        if member.status in (_enums.ChatMemberStatus.LEFT, _enums.ChatMemberStatus.BANNED):
            return False, channel
        return True, None
    except (pyrogram.errors.exceptions.forbidden_403.ChatWriteForbidden, pyrogram.errors.exceptions.bad_request_400.ChatAdminRequired):
        logging.error(f"User {user_id} is spamreported or bot lacks permissions to check force sub.")
        return False, None
    except pyrogram.errors.UserNotParticipant:
        # This is expected if they haven't joined yet, no need to log as error
        return False, channel
    except Exception as e:
        logging.error(f"Force sub verification failed: {e}")
        return False, channel

@app.on_message(filters.command("help") & filters.private)
async def help_command(client, message):
    help_text = (
        "📖 **Help Menu**\n\n"
        "🔗 **Downloads**\n"
        "Just send any Telegram link (public or private) to download.\n"
        "For private links, you must /login first.\n\n"
        "📦 **Batch**\n"
        "Format 1: `/batch start_link end_link` (download from start to end)\n"
        "Format 2: `/batch start_link 50` (download 50 files from start link)\n"
        "Max 50 files per batch · Premium only\n\n"
        "🔗 **Multi-link**\n"
        "Format: `/mlinks` then paste up to 50 links, one per line\n"
        "Links can be from **different channels** — mix any public or private channels freely\n\n"
        "💰 **Quota**\n"
        "Free users: 5 files/day · 15 files/month\n"
        "Premium users: Unlimited — no cooldown, no limits"
    )
    await message.reply(help_text, reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Owner", url=f"https://t.me/Wolfy0046")],
            [InlineKeyboardButton("Support Chat", url=f"https://t.me/Wolfy004chatbot")]
        ])
                       )

@app.on_message(filters.command("batch") & filters.private)
async def batch_handler(client, message):
    parts = message.text.split()
    if len(parts) < 3:
        await message.reply(
            "❌ **Usage:**\n"
            "`/batch start_link end_link` — download from start to end\n"
            "`/batch start_link 50` — download 50 files starting from start link"
        )
        return

    user_id = message.from_user.id
    user = await get_user(user_id)
    if not user or user.get('role', 'free') == 'free':
        await message.reply(
            "❌ **Batch download is for Premium users only.**\n\n"
            "💎 With Premium you can batch up to **50 files at once** from any channel — no cooldown, no daily or monthly limits.\n\n"
            "👉 Use /upgrade to see plans and get Premium."
        )
        return

    # FloodWait cooldown guard
    _fw_deadline = _user_floodwait_until.get(user_id, 0)
    if time.time() < _fw_deadline:
        remaining = int(_fw_deadline - time.time())
        logging.info(f"Dropping batch request from user {user_id} — FloodWait cooldown ({remaining}s left)")
        return

    start_link = parts[1]
    second_arg = parts[2]

    # Detect if second argument is a count (number) or an end link
    count_mode = second_arg.isdigit()
    if count_mode:
        requested_count = int(second_arg)
        if requested_count < 1 or requested_count > 50:
            await message.reply("⚠️ Count must be between 1 and 50.")
            return
        end_link = start_link  # placeholder — end_id computed from count below
    else:
        end_link = second_arg
        requested_count = None

    # Story links: t.me/c/CHANNEL/s/ID  or  t.me/USERNAME/s/ID
    start_private_story_match = re.search(r"t\.me/c/(\d+)/s/(\d+)", start_link)
    start_public_story_match  = re.search(r"t\.me/(?!c/)([^/]+)/s/(\d+)", start_link)

    # Topic links: t.me/c/CHANNEL/TOPIC/MSG
    start_topic_match     = re.search(r"t\.me/c/(\d+)/(\d+)/(\d+)", start_link)
    start_pub_topic_match = re.search(r"t\.me/(?!c/)([^/]+)/(\d+)/(\d+)", start_link)

    # Plain private/public links (no topic)
    start_match = re.search(r"t\.me/c/(\d+)/(\d+)", start_link) or re.search(r"t\.me/(?!c/)([^/]+)/(\d+)", start_link)

    # End-link matches — only needed when not in count_mode
    if not count_mode:
        end_private_story_match = re.search(r"t\.me/c/(\d+)/s/(\d+)", end_link)
        end_public_story_match  = re.search(r"t\.me/(?!c/)([^/]+)/s/(\d+)", end_link)
        end_topic_match         = re.search(r"t\.me/c/(\d+)/(\d+)/(\d+)", end_link)
        end_pub_topic_match     = re.search(r"t\.me/(?!c/)([^/]+)/(\d+)/(\d+)", end_link)
        end_match               = re.search(r"t\.me/c/(\d+)/(\d+)", end_link) or re.search(r"t\.me/(?!c/)([^/]+)/(\d+)", end_link)
    else:
        end_private_story_match = end_public_story_match = None
        end_topic_match = end_pub_topic_match = end_match = None

    # Determine link type, extract start_id, compute end_id
    if start_private_story_match and (count_mode or end_private_story_match):
        link_type    = "private_story"
        channel_part = start_private_story_match.group(1)
        topic_part   = None
        start_id = int(start_private_story_match.group(2))
        end_id   = start_id + requested_count - 1 if count_mode else int(end_private_story_match.group(2))
    elif start_public_story_match and (count_mode or end_public_story_match):
        link_type    = "public_story"
        channel_part = start_public_story_match.group(1)
        topic_part   = None
        start_id = int(start_public_story_match.group(2))
        end_id   = start_id + requested_count - 1 if count_mode else int(end_public_story_match.group(2))
    elif start_topic_match and (count_mode or end_topic_match):
        link_type    = "private_topic"
        channel_part = start_topic_match.group(1)
        topic_part   = start_topic_match.group(2)
        start_id = int(start_topic_match.group(3))
        end_id   = start_id + requested_count - 1 if count_mode else int(end_topic_match.group(3))
    elif start_pub_topic_match and (count_mode or end_pub_topic_match):
        link_type    = "public_topic"
        channel_part = start_pub_topic_match.group(1)
        topic_part   = start_pub_topic_match.group(2)
        start_id = int(start_pub_topic_match.group(3))
        end_id   = start_id + requested_count - 1 if count_mode else int(end_pub_topic_match.group(3))
    elif start_match and (count_mode or end_match):
        link_type    = "private" if "t.me/c/" in start_link else "public"
        channel_part = start_match.group(1)
        topic_part   = None
        start_id = int(start_match.group(2))
        end_id   = start_id + requested_count - 1 if count_mode else int(end_match.group(2))
    else:
        await message.reply("❌ Invalid link or format provided.")
        return

    # In end_link mode allow reversed order; count_mode always goes forward
    if not count_mode and start_id > end_id:
        start_id, end_id = end_id, start_id

    count = end_id - start_id + 1
    if count > 50:
        await message.reply("⚠️ You can only batch up to 50 messages at a time.")
        return

    import random

    batch_status = await safe_reply(
        message,
        f"🚀 **Batch started** — {count} item(s)\n\n"
        f"⏳ Progress: 0/{count}\n"
        f"✅ Done: 0 | ❌ Skipped: 0\n\n"
        f"ℹ️ Rate-limits auto-pause and resume. /cancel = stop current item only · /cancelbatch = stop entire batch"
    )
    if batch_status is None:
        logging.error(f"Could not send batch status message to user {user_id} — FloodWait too long")
        return

    processed_albums = set()
    done = 0
    skipped = 0

    # Pre-fetch deliberately disabled: a single bulk get_messages([id×50]) call
    # triggers Telegram's IP-level rate limiter (~10 000s FloodWait) which then
    # blocks every user on the same VPS for ~3 hours.  Each item is fetched
    # individually inside download_handler instead.
    prefetched_msgs: dict = {}

    # Hold the session guard for the entire batch so the cleanup loop
    # never evicts this user's Pyrogram client during inter-item sleeps
    # or FloodWait pauses (where active_sessions would be temporarily clear).
    _batch_sessions.add(user_id)
    try:
        for idx, msg_id in enumerate(range(start_id, end_id + 1), start=1):
            if user_id in batch_cancel_flags:
                batch_cancel_flags.discard(user_id)
                await batch_status.edit_text(
                    f"🛑 **Batch cancelled**\n\n"
                    f"✅ Done: {done} | ❌ Skipped: {skipped} | 📋 Total attempted: {idx - 1}"
                )
                return

            if link_type == "private_story":
                link = f"https://t.me/c/{channel_part}/s/{msg_id}"
            elif link_type == "public_story":
                link = f"https://t.me/{channel_part}/s/{msg_id}"
            elif link_type == "private_topic":
                link = f"https://t.me/c/{channel_part}/{topic_part}/{msg_id}"
            elif link_type == "public_topic":
                link = f"https://t.me/{channel_part}/{topic_part}/{msg_id}"
            elif link_type == "private":
                link = f"https://t.me/c/{channel_part}/{msg_id}"
            else:
                link = f"https://t.me/{channel_part}/{msg_id}"

            # Update live status
            try:
                await batch_status.edit_text(
                    f"📥 **Batch in progress** — item {idx}/{count}\n\n"
                    f"✅ Done: {done} | ❌ Skipped: {skipped}\n"
                    f"🔗 Processing: `{link}`"
                )
            except Exception:
                pass

            # Retry this item up to 3 times (handles FloodWait inside download_handler)
            item_done = False
            item_had_media = False
            for attempt in range(3):
                try:
                    result = await download_handler(
                        client, message,
                        link_override=link,
                        processed_albums=processed_albums,
                        status_msg_override=batch_status,
                        prefetched_msgs=prefetched_msgs,
                        skip_quota_check=True,
                    )
                    if result is not None:
                        done += 1
                        item_had_media = True
                    else:
                        skipped += 1
                    item_done = True
                    break
                except (FloodWait, FloodPremiumWait) as e:
                    wait_secs = e.value
                    logging.warning(f"Batch outer FloodWait: {wait_secs}s for user {user_id}, item {msg_id}, attempt {attempt + 1}")
                    try:
                        await batch_status.edit_text(
                            f"⏳ **Rate limit hit — auto-pausing**\n\n"
                            f"Waiting {wait_secs}s before retrying item {idx}/{count}...\n"
                            f"✅ Done: {done} | ❌ Skipped: {skipped}"
                        )
                    except Exception:
                        pass
                    await asyncio.sleep(wait_secs + 3)
                except Exception as e:
                    logging.error(f"Batch item error (link={link}, attempt={attempt + 1}): {e}")
                    if attempt == 2:
                        skipped += 1
                        item_done = True
                    else:
                        await asyncio.sleep(3)

            if not item_done:
                skipped += 1

            # Abort the whole batch immediately on fatal session error — every remaining
            # item would hit the same "please login" failure, so there's no point continuing.
            if user_id in _batch_session_error_flags:
                _batch_session_error_flags.discard(user_id)
                try:
                    await batch_status.edit_text(
                        f"🔐 **Session Error — Batch Stopped**\n\n"
                        f"Your Telegram session was removed or expired mid-batch.\n"
                        f"✅ Done: {done} | ❌ Skipped: {skipped} | 📋 Remaining: {count - idx}\n\n"
                        f"Please log in again with /login and retry the batch."
                    )
                except Exception:
                    pass
                return

            # Inter-item delay: only when the current item actually had media and was
            # fully downloaded + uploaded. Items with no media are skipped instantly.
            if idx < count and item_had_media:
                try:
                    await batch_status.edit_text(
                        f"⏸️ **Item {idx}/{count} done** — waiting 5s before next...\n\n"
                        f"✅ Done: {done} | ❌ Skipped: {skipped}"
                    )
                except Exception:
                    pass
                await asyncio.sleep(5)

        try:
            await batch_status.edit_text(
                f"✅ **Batch complete!**\n\n"
                f"📋 Total: {count}\n"
                f"✅ Done: {done}\n"
                f"❌ Skipped: {skipped}"
            )
        except (FloodWait, FloodPremiumWait) as e:
            logging.warning(f"FloodWait {e.value}s on batch completion edit for user {user_id} — skipping final status update")
        except Exception:
            pass
    finally:
        _batch_sessions.discard(user_id)
        batch_cancel_flags.discard(user_id)

@app.on_message(filters.command("mlinks") & filters.private)
async def mlinks_handler(client, message):
    """Download up to 50 individual Telegram links sent one per line.
    Usage: /mlinks
    https://t.me/...
    https://t.me/...
    """
    # Show usage if no links were included in the command message
    if not re.search(r"https?://(?:t|telegram)\.me/\S+|tg://resolve\S+", message.text or ""):
        await message.reply(
            "❌ Usage: `/mlinks` followed by up to 50 links, one per line:\n\n"
            "`/mlinks`\n"
            "`https://t.me/channel/123`\n"
            "`https://t.me/channel/456`\n\n"
            "Links can be from different channels — mix any public or private channels freely."
        )
        return

    user_id = message.from_user.id
    user = await get_user(user_id)

    if user and user.get("role") == "banned":
        await message.reply("❌ **You are banned from using this bot.**")
        return

    # Premium / admin / owner only
    is_owner = OWNER_ID and user_id == int(OWNER_ID)
    is_privileged = user and user.get("role") in ("premium", "admin", "owner")
    if not is_owner and not is_privileged:
        await message.reply(
            "❌ **Multi-link download is for Premium users only.**\n\n"
            "💎 With Premium you can download up to **50 links at once** from any mix of channels — no cooldown, no daily or monthly limits.\n\n"
            "👉 Use /upgrade to see plans and get Premium."
        )
        return

    # FloodWait cooldown guard
    _fw_deadline = _user_floodwait_until.get(user_id, 0)
    if time.time() < _fw_deadline:
        remaining = int(_fw_deadline - time.time())
        logging.info(f"Dropping mlinks request from user {user_id} — FloodWait cooldown ({remaining}s left)")
        return

    # Extract all supported Telegram links from the message
    raw_links = re.findall(r"https?://(?:t|telegram)\.me/\S+|tg://resolve\S+", message.text)
    # Strip ?single and any trailing punctuation from each link
    links = []
    for raw in raw_links:
        clean = re.sub(r"\?single$", "", raw).rstrip(".,;)")
        if clean:
            links.append(clean)

    if not links:
        await message.reply(
            "❌ **No links found.**\n\n"
            "📖 **Usage** — send `/mlinks` with your links on the lines below:\n\n"
            "`/mlinks`\n"
            "`https://t.me/channelA/123`\n"
            "`https://t.me/channelB/456`\n"
            "`https://t.me/c/1234567890/789`\n\n"
            "• Up to **50 links** per command\n"
            "• Links can be from **different channels** — mix public, private, and restricted channels freely\n"
            "• Supports public & private links\n"
            "• `?single` is handled automatically"
        )
        return

    if len(links) > 50:
        await message.reply("⚠️ Maximum 50 links allowed. Only the first 50 will be processed.")
        links = links[:50]

    count = len(links)

    # Prevent overlapping batch runs for the same user
    if user_id in _batch_sessions:
        await message.reply("⚠️ You already have an active batch running. Use /cancelbatch to stop it first.")
        return

    batch_status = await safe_reply(
        message,
        f"🚀 **Multi-link download started** — {count} link(s)\n\n"
        f"⏳ Progress: 0/{count}\n"
        f"✅ Done: 0 | ❌ Skipped: 0\n\n"
        f"ℹ️ /cancel = stop current item · /cancelbatch = stop all"
    )
    if batch_status is None:
        logging.error(f"Could not send mlinks status message to user {user_id} — FloodWait too long")
        return

    processed_albums = set()
    done = 0
    skipped = 0

    import random
    _batch_sessions.add(user_id)
    try:
        for idx, link in enumerate(links, start=1):
            if user_id in batch_cancel_flags:
                batch_cancel_flags.discard(user_id)
                try:
                    await batch_status.edit_text(
                        f"🛑 **Cancelled**\n\n"
                        f"✅ Done: {done} | ❌ Skipped: {skipped} | 📋 Total attempted: {idx - 1}"
                    )
                except Exception:
                    pass
                return

            try:
                await batch_status.edit_text(
                    f"📥 **Downloading** — link {idx}/{count}\n\n"
                    f"✅ Done: {done} | ❌ Skipped: {skipped}\n"
                    f"🔗 `{link}`"
                )
            except Exception:
                pass

            item_done = False
            item_had_media = False
            for attempt in range(3):
                try:
                    result = await download_handler(
                        client, message,
                        link_override=link,
                        processed_albums=processed_albums,
                        status_msg_override=batch_status,
                        skip_quota_check=True,
                    )
                    if result is not None:
                        done += 1
                        item_had_media = True
                    else:
                        skipped += 1
                    item_done = True
                    break
                except (FloodWait, FloodPremiumWait) as e:
                    wait_secs = e.value
                    logging.warning(f"mlinks FloodWait: {wait_secs}s for user {user_id}, link {idx}, attempt {attempt + 1}")
                    try:
                        await batch_status.edit_text(
                            f"⏳ **Rate limit — auto-pausing {wait_secs}s**\n\n"
                            f"Retrying link {idx}/{count}...\n"
                            f"✅ Done: {done} | ❌ Skipped: {skipped}"
                        )
                    except Exception:
                        pass
                    await asyncio.sleep(wait_secs + 3)
                except Exception as e:
                    logging.error(f"mlinks item error (link={link}, attempt={attempt + 1}): {e}")
                    if attempt == 2:
                        skipped += 1
                        item_done = True
                    else:
                        await asyncio.sleep(3)

            if not item_done:
                skipped += 1

            # Abort on fatal session error
            if user_id in _batch_session_error_flags:
                _batch_session_error_flags.discard(user_id)
                try:
                    await batch_status.edit_text(
                        f"🔐 **Session Error — Stopped**\n\n"
                        f"Your Telegram session expired mid-download.\n"
                        f"✅ Done: {done} | ❌ Skipped: {skipped} | 📋 Remaining: {count - idx}\n\n"
                        f"Please /login again and retry."
                    )
                except Exception:
                    pass
                return

            # Inter-item delay: only when the current item actually had media and was
            # fully downloaded + uploaded. Items with no media are skipped instantly.
            if idx < count and item_had_media:
                try:
                    await batch_status.edit_text(
                        f"⏸️ **Link {idx}/{count} done** — waiting 5s before next...\n\n"
                        f"✅ Done: {done} | ❌ Skipped: {skipped}"
                    )
                except Exception:
                    pass
                await asyncio.sleep(5)

        try:
            await batch_status.edit_text(
                f"✅ **All done!**\n\n"
                f"📋 Total: {count}\n"
                f"✅ Done: {done}\n"
                f"❌ Skipped: {skipped}"
            )
        except (FloodWait, FloodPremiumWait) as e:
            logging.warning(f"FloodWait {e.value}s on mlinks completion edit for user {user_id}")
        except Exception:
            pass
    finally:
        _batch_sessions.discard(user_id)
        batch_cancel_flags.discard(user_id)

@app.on_message(filters.command("cancel") & filters.private)
async def cancel_handler(client, message):
    """Cancel the current single download item only.
    Works whether the download is standalone or part of a batch.
    The batch itself continues with the next item after cancellation.
    Use /cancelbatch to stop the entire batch."""
    user_id = message.from_user.id
    if user_id in active_downloads or user_id in active_sessions:
        cancel_flags.add(user_id)
        await message.reply("🛑 Cancelling current download... Please wait.")
    else:
        await message.reply("ℹ️ No active download to cancel.")

@app.on_message(filters.command("cancelbatch") & filters.private)
async def cancelbatch_handler(client, message):
    """Cancel the entire batch — stops the loop after the current item finishes."""
    user_id = message.from_user.id
    if user_id in _batch_sessions:
        batch_cancel_flags.add(user_id)
        await message.reply("🛑 Batch cancellation requested. The current item will finish, then the batch will stop.")
    else:
        await message.reply("ℹ️ No active batch to cancel.")

@app.on_message(filters.regex(r"https?://t\.me/|https?://telegram\.me/|tg://resolve") & filters.private)
async def download_handler(client, message, link_override=None, processed_albums=None, status_msg_override=None, prefetched_msgs: dict = None, skip_quota_check: bool = False):
    user_id = message.from_user.id
    username = message.from_user.username
    full_name = f"{message.from_user.first_name or ''} {message.from_user.last_name or ''}".strip()
    link = link_override or message.text.strip()
    # Strip ?single so the link is handled identically to its plain version.
    # Without this, ?single links skip the is_group check and lose the user session.
    link = re.sub(r"\?single$", "", link).strip()

    # --- Normalize alternative Telegram link formats to standard t.me URLs ---
    # telegram.me is an official alias for t.me — rewrite it so all patterns match.
    link = re.sub(r"https?://telegram\.me/", "https://t.me/", link)
    # tg://resolve?domain=USERNAME&post=MSGID  →  https://t.me/USERNAME/MSGID
    if link.startswith("tg://resolve"):
        _parsed = urlparse(link)
        _params = parse_qs(_parsed.query)
        _domain = (_params.get("domain") or [None])[0]
        _post   = (_params.get("post")   or [None])[0]
        if _domain and _post:
            link = f"https://t.me/{_domain}/{_post}"
        elif _domain:
            # No post ID — just a channel link, nothing to download
            await safe_reply(message, "❌ That link points to a channel but not a specific message. Send a direct message link.")
            return
    # -------------------------------------------------------------------------

    from bot.database import create_user
    user = await get_user(user_id)
    if not user:
        user = await create_user(user_id, username, full_name)
    elif user.get("username") != username or user.get("full_name") != full_name:
        await create_user(user_id, username, full_name)
        user = await get_user(user_id)

    if user and user.get("role") == "banned":
        await message.reply("❌ **You are banned from using this bot.**")
        return

    # FloodWait cooldown guard — silently drop requests while Telegram rate-limits this user
    _fw_deadline = _user_floodwait_until.get(user_id, 0)
    if time.time() < _fw_deadline:
        remaining = int(_fw_deadline - time.time())
        logging.info(f"Dropping request from user {user_id} — still in FloodWait cooldown ({remaining}s left)")
        return

    # Rate limiting — only for free users sending direct messages (not batch calls)
    if link_override is None and (not user or user.get("role", "free") == "free"):
        _now = time.time()
        _last = _user_last_request.get(user_id, 0)
        _wait = RATE_LIMIT_SECONDS - (_now - _last)
        if _wait > 0:
            _wait_mins = int(_wait // 60)
            _wait_secs = int(_wait % 60)
            _wait_str = f"{_wait_mins}m {_wait_secs}s" if _wait_mins > 0 else f"{_wait_secs}s"
            await message.reply(
                f"⏳ **Please wait {_wait_str}** before sending another request.\n\n"
                f"💎 **Premium users skip this wait entirely** — unlimited downloads with no cooldown.\n\n"
                f"👉 Use /upgrade to see plans and get Premium."
            )
            return
        _user_last_request[user_id] = _now

    chat_id = None
    message_id = None

    private_match = re.search(r"t\.me/c/(\d+)/(\d+)", link)
    public_match = re.search(r"t\.me/(?!c/)([^/]+)/(\d+)", link)
    public_topic_match = re.search(r"t\.me/(?!c/)([^/]+)/(\d+)/(\d+)", link)
    topic_match = re.search(r"t\.me/c/(\d+)/(\d+)/(\d+)", link)
    comment_match = re.search(r"t\.me/([^/]+)/(\d+)\?comment=(\d+)", link)
    private_comment_match = re.search(r"t\.me/c/(\d+)/(\d+)\?comment=(\d+)", link)
    # Forum topic comment links: t.me/c/CHANNEL/TOPIC/POST?comment=COMMENT
    #                            t.me/USERNAME/TOPIC/POST?comment=COMMENT
    # In forum supergroups, comments on topic posts live in the SAME group.
    private_topic_comment_match = re.search(r"t\.me/c/(\d+)/(\d+)/(\d+)\?comment=(\d+)", link)
    public_topic_comment_match  = re.search(r"t\.me/(?!c/)([^/]+)/(\d+)/(\d+)\?comment=(\d+)", link)
    story_match = re.search(r"t\.me/([^/]+)/s/(\d+)", link)
    private_story_match = re.search(r"t\.me/c/(\d+)/s/(\d+)", link)
    single_match = re.search(r"t\.me/([^/]+)/(\d+)\?single", link)
    private_single_match = re.search(r"t\.me/c/(\d+)/(\d+)\?single", link)
    thread_match = re.search(r"t\.me/([^/]+)/(\d+)\?thread=(\d+)", link)
    private_thread_match = re.search(r"t\.me/c/(\d+)/(\d+)\?thread=(\d+)", link)

    is_private = False
    is_group = False
    is_story = False
    _pending_comment_resolve = None  # (temp_channel, comment_id) when bot couldn't resolve linked chat
    _topic_filter = None  # set for topic links — messages not in this topic are skipped

    if private_story_match:
        chat_id = int("-100" + private_story_match.group(1))
        message_id = int(private_story_match.group(2))
        is_private = True
        is_story = True
    elif story_match:
        chat_id = story_match.group(1)
        message_id = int(story_match.group(2))
        is_story = True
        is_private = True
        is_group = True
    elif private_topic_comment_match:
        # t.me/c/CHANNEL/TOPIC/POST?comment=COMMENT — comment in a forum topic.
        # The comment lives in the SAME supergroup (not a separate linked chat).
        chat_id = int("-100" + private_topic_comment_match.group(1))
        message_id = int(private_topic_comment_match.group(4))
        is_private = True
        is_group = True
    elif public_topic_comment_match:
        # t.me/USERNAME/TOPIC/POST?comment=COMMENT — public forum topic comment.
        chat_id = public_topic_comment_match.group(1)
        message_id = int(public_topic_comment_match.group(4))
        is_group = True
        is_private = True
    elif private_comment_match:
        temp_channel_id = int("-100" + private_comment_match.group(1))
        channel_post_id = int(private_comment_match.group(2))
        comment_id = int(private_comment_match.group(3))
        is_private = True
        is_group = True
        # Same deferral as comment_match: let the user client resolve the
        # linked discussion group so its access hash lands in user client
        # storage (not just the bot client's storage).
        chat_id = temp_channel_id
        message_id = channel_post_id
        _pending_comment_resolve = (temp_channel_id, channel_post_id, comment_id)
    elif comment_match:
        temp_channel = comment_match.group(1)
        channel_post_id = int(comment_match.group(2))
        comment_id = int(comment_match.group(3))
        # Always defer resolution to the user client. The bot client's
        # access hashes are NOT shared with the user client's storage, so
        # if we resolve the linked discussion group here and hand its
        # numeric ID to the user client, the user client calls
        # channels.GetChannels with access_hash=0 → CHANNEL_INVALID.
        # By deferring, the user client resolves the channel itself and
        # populates its own storage with the correct access hash.
        chat_id = temp_channel
        message_id = channel_post_id
        is_private = True
        is_group = True
        _pending_comment_resolve = (temp_channel, channel_post_id, comment_id)
    elif private_thread_match:
        chat_id = int("-100" + private_thread_match.group(1))
        message_id = int(private_thread_match.group(2))
        is_private = True
        is_group = True
    elif thread_match:
        # t.me/GROUP/POST?thread=TOPIC — message in a forum thread (supergroup)
        chat_id = thread_match.group(1)
        message_id = int(thread_match.group(2))
        is_group = True
    elif private_single_match:
        chat_id = int("-100" + private_single_match.group(1))
        message_id = int(private_single_match.group(2))
        is_private = True
    elif single_match:
        chat_id = single_match.group(1)
        message_id = int(single_match.group(2))
    elif topic_match:
        chat_id = int("-100" + topic_match.group(1))
        message_id = int(topic_match.group(3))
        _topic_filter = int(topic_match.group(2))
        is_private = True
        is_group = True
    elif public_topic_match:
        chat_id = public_topic_match.group(1)
        message_id = int(public_topic_match.group(3))
        _topic_filter = int(public_topic_match.group(2))
        is_group = True
    elif private_match:
        chat_id = int("-100" + private_match.group(1))
        message_id = int(private_match.group(2))
        is_private = True
    elif public_match:
        chat_id = public_match.group(1)
        message_id = int(public_match.group(2))
        try:
            if chat_id.isdigit() or (chat_id.startswith("-") and chat_id[1:].isdigit()):
                chat_id = int(chat_id)
            cache_key = str(chat_id)
            if cache_key in _chat_type_cache:
                is_group = _chat_type_cache[cache_key]
            else:
                chat = await asyncio.wait_for(client.get_chat(chat_id), timeout=5)
                chat_type_str = str(chat.type).lower()
                if "group" in chat_type_str or "supergroup" in chat_type_str:
                    is_group = True
                elif hasattr(chat, "broadcast") and chat.broadcast is False:
                    is_group = True
                _chat_type_cache[cache_key] = is_group
        except Exception as e:
            logging.debug(f"Chat check error for {chat_id}: {e}")
            pass

    if chat_id is None:
        if status_msg_override is not None:
            await update_status(status_msg_override, "❌ Unsupported or unrecognised link format.")
        else:
            await safe_reply(message, "❌ Unsupported or unrecognised link format. Supported: `t.me/channel/123`, `t.me/c/ID/123`, topic, comment, thread, and story links.")
        return

    if status_msg_override is not None:
        status_msg = status_msg_override
    else:
        status_msg = await safe_reply(message, "⏳ Processing...")
        if status_msg is None:
            logging.error(f"Could not send processing message to user {user_id} — FloodWait too long")
            return None

    if (is_private or is_group) and (not user or not user.get('phone_session_string')):
        await update_status(status_msg, "❌ Login is required for private links. Use /login.")
        return

    user_client = None

    try:
        async with global_download_semaphore:
            if user_id in active_downloads:
                await update_status(status_msg, "⚠️ You already have an active download. Please wait.")
                return None
            active_downloads.add(user_id)

            processed_count = 0
            
            try:
                if is_private or is_group or is_story:
                    session_str = user.get('phone_session_string') if user else None
                    if session_str:
                        active_sessions.add(user_id)
                        user_client = await get_user_client(user_id, session_str)
                else:
                    user_client = client

                if not user_client:
                    await update_status(status_msg, "❌ Session error. Please /login again.")
                    return None 
                
                # Double check client connection
                if not user_client.is_connected:
                    try:
                        await user_client.start()
                    except Exception as e:
                        logging.error(f"Failed to restart user_client: {e}")
                        await update_status(status_msg, "❌ Session disconnected. Please try again.")
                        return None

                # Re-resolve comment links using the user client so that the
                # linked discussion group's access hash ends up in the USER
                # client's own storage (not just the bot client's storage).
                if _pending_comment_resolve:
                    _rc_channel, _rc_post_id, _rc_comment_id = _pending_comment_resolve
                    try:
                        chat_info = await user_client.get_chat(_rc_channel)
                        if chat_info.linked_chat:
                            # linked_chat.id is now cached in user_client storage
                            chat_id = chat_info.linked_chat.id
                            message_id = _rc_comment_id
                        else:
                            # No discussion group — download the channel post
                            chat_id = _rc_channel
                            message_id = _rc_post_id
                    except Exception as _e:
                        logging.debug(f"User-client comment re-resolve failed for {_rc_channel}: {_e}")
                        # Keep the username fallback set earlier; user client
                        # will attempt contacts.ResolveUsername at fetch time.

                msg = None
                # Use batch pre-fetched message if available — avoids one get_messages API call
                if not is_story and prefetched_msgs and message_id in prefetched_msgs:
                    msg = prefetched_msgs[message_id]
                for _fetch_attempt in range(4):
                    if msg is not None:
                        break
                    try:
                        if is_story:
                            msg = await user_client.get_stories(chat_id, message_id)
                        else:
                            msg = await _get_messages_rate_limited(user_client, client, chat_id, message_id)
                        break
                    except (FloodWait, FloodPremiumWait) as e:
                        wait_secs = e.value
                        logging.warning(f"FloodWait on get_messages: {wait_secs}s for user {user_id} (attempt {_fetch_attempt + 1})")
                        if wait_secs > MAX_FLOODWAIT_TOLERATE:
                            _user_floodwait_until[user_id] = time.time() + wait_secs
                            await update_status(status_msg, f"⏳ Telegram rate limit is too high ({wait_secs}s). Please try again later.")
                            return None
                        await update_status(status_msg, f"⏳ Telegram rate limit — auto-resuming in {wait_secs}s...")
                        await asyncio.sleep(wait_secs + 2)
                    except (ConnectionError, OSError, TimeoutError) as e:
                        # Stale TCP socket — evict the dead client and reconnect once.
                        logging.warning(f"TCP error on get_messages for user {user_id} (attempt {_fetch_attempt + 1}): {e}")
                        stale = user_clients.pop(user_id, None)
                        if stale:
                            try:
                                await stale["client"].stop()
                            except Exception:
                                pass
                        if _fetch_attempt < 3:
                            session_str = user.get("phone_session_string") if user else None
                            if session_str:
                                try:
                                    user_client = await get_user_client(user_id, session_str)
                                    await asyncio.sleep(1)
                                except Exception as reconnect_err:
                                    logging.error(f"Reconnect failed for user {user_id}: {reconnect_err}")
                                    await update_status(status_msg, "❌ Connection error. Please try again.")
                                    return None
                        else:
                            await update_status(status_msg, "❌ Connection error after retries. Please try again.")
                            return None
                    except Exception as e:
                        error_str = str(e)
                        if any(kw in error_str for kw in ["AUTH_KEY_UNREGISTERED", "SESSION_REVOKED", "401"]):
                            is_revoked = "SESSION_REVOKED" in error_str
                            logging.error(f"Session error for {user_id}: {error_str}")
                            if user_id in user_clients:
                                client_data = user_clients.pop(user_id, None)
                                if client_data:
                                    try:
                                        await client_data["client"].stop()
                                    except:
                                        pass
                            if is_revoked:
                                from bot.database import logout_user
                                await logout_user(user_id)
                                if user_id in _batch_sessions:
                                    _batch_session_error_flags.add(user_id)
                                await update_status(status_msg, "❌ Your Telegram session has expired or was revoked. Please log in again using /login.")
                            else:
                                logging.warning(f"AuthKeyUnregistered (possible PFS rotation) for user {user_id} — evicting client only, session preserved")
                                if user_id in _batch_sessions:
                                    _batch_session_error_flags.add(user_id)
                                await update_status(status_msg, "❌ Connection interrupted. Please send the link again.")
                            return None

                        if "TAKEOUT_INIT_DELAY" in error_str:
                            wait_time = "24 hours"
                            match = re.search(r"in (\d+) seconds", str(e))
                            if match:
                                seconds = int(match.group(1))
                                hours = seconds // 3600
                                minutes = (seconds % 3600) // 60
                                wait_time = f"{hours}h {minutes}m" if hours > 0 else f"{minutes}m"
                            await message.reply(
                                f"⚠️ **Telegram Security Notice**\n\n"
                                f"Telegram requires a wait time of **{wait_time}** before you can download content through this session.\n\n"
                                f"💡 **Action Required:**\n"
                                f"Check your other Telegram devices for a notification about an **'Account Export Request'**. Click **'Allow'** or **'Yes, it's me'** to potentially speed up this process or authorize the access."
                            )
                            return None

                        if "msg.copy" in str(e) or "copy_media_group" in str(e):
                            raise e

                        await update_status(status_msg, f"❌ Error: {str(e)}")
                        return None

                if msg is None:
                    await update_status(status_msg, "❌ Failed to fetch message after multiple attempts due to rate limits. Please try again later.")
                    return None

                # Topic filter: skip messages that don't belong to the expected topic.
                # In Telegram forum groups, message IDs are global across the whole group —
                # iterating IDs 161, 162, 163... will hit messages from OTHER topics too.
                # msg.message_thread_id tells us which topic this message actually belongs to.
                if _topic_filter is not None:
                    msg_thread = getattr(msg, "message_thread_id", None)
                    if msg_thread != _topic_filter:
                        logging.debug(f"Skipping msg {message_id}: belongs to topic {msg_thread}, expected {_topic_filter}")
                        if status_msg_override is None:
                            try:
                                await status_msg.delete()
                            except Exception:
                                pass
                        return None

                if not msg or (not getattr(msg, "media", None) and not getattr(msg, "text", None) and type(msg).__name__ != "Story"):
                    await update_status(status_msg, "❌ No content found in link.")
                    return None

                if getattr(msg, "web_page", None) is not None:
                    await update_status(status_msg, "❌ This link points to a web page preview — there is no downloadable file attached.")
                    return None

                media_group_id = getattr(msg, "media_group_id", None)
                if processed_albums is not None and media_group_id:
                    if media_group_id in processed_albums:
                        # In batch mode, never delete the shared status message
                        if status_msg_override is None:
                            await status_msg.delete()
                        return msg.id
                    processed_albums.add(media_group_id)

                if not is_story and getattr(msg, "media_group_id", None):
                    target_messages = await user_client.get_media_group(chat_id, message_id)
                    is_media_group = True
                else:
                    target_messages = [msg]
                    is_media_group = False

                if not skip_quota_check:
                    can_download, quota_status = await check_and_update_quota(user_id)

                    if not can_download:
                        await update_status(status_msg, f"❌ {quota_status}")
                        return None

                    if user and user.get("role") == "free":
                        await increment_quota(user_id, len(target_messages))

                if not is_private and not is_group and not is_story:
                    try:
                        await update_status(status_msg, "🚀 Extracting directly...")
                        media_group_id = getattr(msg, "media_group_id", None)
                        if media_group_id:
                            await client.copy_media_group(chat_id=user_id, from_chat_id=chat_id, message_id=message_id)
                            #Dumo Copy
                            await send_to_dump(client, user_id, link, msg)
                            
                            processed_count = len(target_messages)
                        else:
                            await msg.copy(chat_id=user_id)
                            #Dump Copy
                            await send_to_dump(client, user_id, link, msg)
                            
                            processed_count = 1
                        if status_msg_override is None:
                            await status_msg.delete()
                        active_downloads.discard(user_id)
                        return msg 
                    except (AuthKeyUnregistered, AuthKeyUnregistered401, SessionRevoked) as e:
                        raise e
                    except Exception as e:
                        error_str = str(e)
                        if isinstance(e, (FloodWait, FloodPremiumWait)):
                            wait_secs = e.value
                            logging.warning(f"FloodWait {wait_secs}s on direct extraction (messages.SendMedia) for user {user_id}")
                            if wait_secs > MAX_FLOODWAIT_TOLERATE:
                                _user_floodwait_until[user_id] = time.time() + wait_secs
                                await update_status(status_msg, f"⏳ Telegram rate limit hit ({wait_secs}s). Please try again later.")
                                return None
                            await asyncio.sleep(wait_secs + 2)
                            return None
                        if "USER_IS_BLOCKED" in error_str:
                            logging.warning(f"User {user_id} has blocked the bot — cannot send direct extraction.")
                            return None
                        if "MEDIA_CAPTION_TOO_LONG" in error_str:
                            # Caption exceeds Telegram's 1024-char limit — retry with blank caption
                            try:
                                if media_group_id:
                                    msgs = await client.copy_media_group(chat_id=user_id, from_chat_id=chat_id, message_id=message_id, captions="")
                                else:
                                    await msg.copy(chat_id=user_id, caption="")
                                await send_to_dump(client, user_id, link, msg)
                                processed_count = len(target_messages) if media_group_id else 1
                                if status_msg_override is None:
                                    await status_msg.delete()
                                active_downloads.discard(user_id)
                                return msg
                            except Exception as retry_e:
                                logging.error(f"Direct extraction retry (no caption) failed: {retry_e}")
                        elif "Unknown media" in error_str or "unknown media" in error_str.lower():
                            await update_status(status_msg, "❌ This media type is not supported for direct download.")
                            return None
                        logging.error(f"Direct extraction failed: {e}")
                        await status_msg.edit_text("⚠️ Direct extraction failed, falling back to download/upload...")

                # ------------------------------------------------------------------
                # Resolve upload destination ONCE before iterating over files.
                # This avoids calling get_chat / get_user on every file when
                # processing a media group or a large batch of items from the
                # same channel (which would trigger Telegram rate-limits).
                # ------------------------------------------------------------------
                upload_client = client
                destination_id = user_id
                using_user_session = False
                _resolved_channel_id = None  # track for text-only path below

                if user_client and user_client != client:
                    if user_id in _dest_channel_cache:
                        # Already verified this session — reuse without any API call
                        _resolved_channel_id, using_user_session = _dest_channel_cache[user_id]
                        destination_id = _resolved_channel_id
                        if using_user_session:
                            upload_client = user_client
                    else:
                        channel_id = user.get("download_channel_id") if user else None
                        _skip_channel_create = False

                        if channel_id == "saved_messages":
                            upload_client = user_client
                            destination_id = "me"
                            using_user_session = True
                            _skip_channel_create = True
                            channel_id = None

                        elif channel_id:
                            if not user_client.is_connected:
                                logging.warning(f"User client for {user_id} is no longer connected — skipping channel check, using bot delivery")
                                channel_id = None
                                _skip_channel_create = True

                            if channel_id:
                                try:
                                    if isinstance(channel_id, str) and (channel_id.startswith("-100") or channel_id.isdigit() or channel_id.startswith("-")):
                                        channel_id = int(channel_id)

                                    try:
                                        chat_obj = await user_client.get_chat(channel_id)
                                        c_hash = getattr(chat_obj, "access_hash", None)
                                        if c_hash:
                                            await update_user_channel(user_id, channel_id, str(c_hash))
                                    except pyrogram.errors.ChannelInvalid:
                                        logging.warning(f"Channel {channel_id} is explicitly invalid for user.")
                                        raise Exception("Channel invalid")
                                    except Exception as user_e:
                                        logging.warning(f"User client cannot see channel {channel_id}: {user_e}")

                                    try:
                                        await client.get_chat(channel_id)
                                    except Exception:
                                        try:
                                            me = await client.get_me()
                                            await user_client.add_chat_members(channel_id, me.id)
                                            from pyrogram.types import ChatPrivileges
                                            await user_client.promote_chat_member(
                                                channel_id, me.id,
                                                privileges=ChatPrivileges(
                                                    can_post_messages=True,
                                                    can_delete_messages=True,
                                                    can_invite_users=True,
                                                    can_manage_chat=True
                                                )
                                            )
                                        except Exception as re_e:
                                            logging.warning(f"Failed to re-invite bot to channel {channel_id}: {re_e}")
                                            raise Exception("Bot inaccessible")

                                except Exception as e:
                                    logging.warning(f"Existing channel {channel_id} issue for user {user_id}: {e}. Attempting to recreate.")
                                    channel_id = None

                        if not _skip_channel_create and not channel_id:
                            try:
                                new_chat = await user_client.create_channel("Cloud Storage", "My private cloud storage for downloads.")
                                channel_id = new_chat.id
                                channel_hash = getattr(new_chat, "access_hash", None)
                                await update_user_channel(user_id, channel_id, str(channel_hash) if channel_hash else None)

                                bot_info = await client.get_me()
                                bot_username = bot_info.username
                                try:
                                    from pyrogram.types import ChatPrivileges
                                    invite_id = f"@{bot_username}" if bot_username else bot_info.id
                                    await user_client.promote_chat_member(
                                        channel_id,
                                        invite_id,
                                        privileges=ChatPrivileges(
                                            can_post_messages=True,
                                            can_delete_messages=True,
                                            can_invite_users=True,
                                            can_restrict_members=True,
                                            can_pin_messages=True,
                                            can_promote_members=False,
                                            can_change_info=True
                                        )
                                    )
                                except Exception as invite_err:
                                    logging.warning(f"Failed to promote bot in new channel {channel_id}: {invite_err}")

                                logging.info(f"Created private channel {channel_id} and added bot for user {user_id}")
                            except (pyrogram.errors.UserRestricted, pyrogram.errors.PeerFlood):
                                logging.warning(f"User {user_id} is spam-reported/restricted — persisting Saved Messages fallback.")
                                await update_user_channel(user_id, "saved_messages")
                                upload_client = user_client
                                destination_id = "me"
                                using_user_session = True
                                channel_id = None
                            except Exception as e:
                                error_str = str(e)
                                if "CHANNELS_TOO_MUCH" in error_str:
                                    logging.warning(f"User {user_id} has too many channels — persisting Saved Messages fallback.")
                                    await update_user_channel(user_id, "saved_messages")
                                    await safe_reply(message,
                                        "⚠️ **Your account has joined too many channels/groups.**\n\n"
                                        "Telegram won't let us create a private download channel for you right now. "
                                        "Your files will be sent to your **Saved Messages** instead.\n\n"
                                        "You can free up space by leaving some channels, then send /start to reset."
                                    )
                                else:
                                    logging.error(f"Failed to create private channel for user {user_id}: {e}")
                                upload_client = user_client
                                destination_id = "me"
                                using_user_session = True
                                channel_id = None

                        if channel_id:
                            upload_client = user_client
                            destination_id = channel_id
                            using_user_session = True

                        # Cache the resolved destination for the rest of this batch
                        _dest_channel_cache[user_id] = (destination_id, using_user_session)
                        _resolved_channel_id = destination_id

                for _msg_idx, current_msg in enumerate(target_messages):
                    if _msg_idx > 0:
                        await asyncio.sleep(2)
                    path = None
                    thumb_path = None
                    safe_caption = ""
                    if current_msg.caption:
                        safe_caption = current_msg.caption
                    elif hasattr(current_msg, "text") and current_msg.text:
                        safe_caption = current_msg.text
                    
                    safe_caption = truncate_caption(safe_caption)

                    if getattr(current_msg, "poll", None):
                        await safe_reply(message, "⚠️ Poll messages cannot be downloaded — skipping.")
                        continue

                    if getattr(current_msg, "web_page", None) is not None:
                        await update_status(status_msg, "❌ This link points to a web preview — there is no downloadable file attached.")
                        continue

                    if not getattr(current_msg, "media", None) and type(current_msg).__name__ != "Story":
                        try:
                            await client.send_message(user_id, safe_caption)
                            if _resolved_channel_id and user_client and user_client != client:
                                try:
                                    text_dest = "me" if _resolved_channel_id == "me" else int(_resolved_channel_id) if isinstance(_resolved_channel_id, str) else _resolved_channel_id
                                    await user_client.send_message(text_dest, safe_caption)
                                except Exception as e:
                                    logging.error(f"Text upload to private channel failed: {e}")
                            await send_to_dump(client, user_id, link, current_msg)
                            processed_count += 1
                            continue
                        except Exception as e:
                            logging.error(f"Error sending text-only message: {e}")
                            continue

                    try:
                        if hasattr(current_msg, "video") and current_msg.video and getattr(current_msg.video, "thumbs", None):
                            try:
                                thumb_path = await user_client.download_media(current_msg.video.thumbs[-1], in_memory=True)
                            except Exception as e:
                                logging.debug(f"Thumb download error: {e}")
                        elif hasattr(current_msg, "document") and current_msg.document and getattr(current_msg.document, "thumbs", None):
                            try:
                                thumb_path = await user_client.download_media(current_msg.document.thumbs[-1], in_memory=True)
                            except Exception as e:
                                logging.debug(f"Thumb download error: {e}")

                        if thumb_path is not None and hasattr(thumb_path, "read"):
                            _tpos = thumb_path.tell()
                            thumb_path.seek(0, 2)
                            if thumb_path.tell() == 0:
                                thumb_path = None
                            else:
                                thumb_path.seek(_tpos)

                        duration = 0
                        width = 0
                        height = 0
                        orig_file_name = None
                        has_spoiler = None

                        if hasattr(current_msg, "video") and current_msg.video:
                            duration = getattr(current_msg.video, "duration", 0) or 0
                            width = getattr(current_msg.video, "width", 0) or 0
                            height = getattr(current_msg.video, "height", 0) or 0
                            orig_file_name = getattr(current_msg.video, "file_name", None)
                        elif hasattr(current_msg, "document") and current_msg.document:
                            orig_file_name = getattr(current_msg.document, "file_name", None)
                            if current_msg.document.mime_type and current_msg.document.mime_type.startswith("video/"):
                                duration = getattr(current_msg.document, "duration", 0) or 0
                                width = getattr(current_msg.document, "width", 0) or 0
                                height = getattr(current_msg.document, "height", 0) or 0
                        elif hasattr(current_msg, "audio") and current_msg.audio:
                            orig_file_name = getattr(current_msg.audio, "file_name", None)
                        has_spoiler = getattr(current_msg, "has_media_spoiler", None)

                        _pre_dl_media = (
                            getattr(current_msg, "document", None) or
                            getattr(current_msg, "video", None) or
                            getattr(current_msg, "audio", None) or
                            getattr(current_msg, "voice", None) or
                            getattr(current_msg, "video_note", None) or
                            getattr(current_msg, "animation", None)
                        )
                        _pre_dl_size = getattr(_pre_dl_media, "file_size", 0) or 0
                        _tg_premium = getattr(getattr(user_client, "me", None), "is_premium", False)
                        _size_limit = 4000 * 1024 * 1024 if _tg_premium else 2000 * 1024 * 1024
                        if _pre_dl_size > _size_limit:
                            _limit_mb = 4000 if _tg_premium else 2000
                            logging.warning(
                                f"Skipping download for user {user_id}: "
                                f"{_pre_dl_size / 1048576:.1f} MiB exceeds {_limit_mb} MiB Telegram limit"
                            )
                            await update_status(
                                status_msg,
                                f"❌ File is too large ({_pre_dl_size / 1048576:.0f} MB) — "
                                f"Telegram cannot receive files larger than {_limit_mb} MB."
                            )
                            continue

                        for _dl_attempt in range(2):
                            try:
                                path = await asyncio.wait_for(
                                    download_media_parallel(
                                        user_client,
                                        current_msg,
                                        num_workers=4,
                                        progress_callback=progress_bar,
                                        progress_args=(status_msg, "📥 Downloading", status_msg_override is None)
                                    ),
                                    timeout=2700  # 45 min — kills truly stuck transfers
                                )
                                break
                            except (FloodWait, FloodPremiumWait) as e:
                                logging.warning(f"FloodWait on download: {e.value}s")
                                await asyncio.sleep(e.value)
                            except asyncio.TimeoutError:
                                logging.error(f"Download stuck/timed out (45 min) for user {user_id}, msg {current_msg.id} — aborting")
                                await update_status(status_msg, "❌ Download timed out — transfer appeared stuck. Please try again.")
                                path = None
                                break
                            except (ConnectionError, OSError, TimeoutError) as e:
                                logging.warning(f"TCP error during download for user {user_id} (attempt {_dl_attempt + 1}): {e}")
                                stale = user_clients.pop(user_id, None)
                                if stale:
                                    try:
                                        await stale["client"].stop()
                                    except Exception:
                                        pass
                                if _dl_attempt < 1:
                                    session_str_dl = user.get("phone_session_string") if user else None
                                    if session_str_dl:
                                        try:
                                            user_client = await get_user_client(user_id, session_str_dl)
                                            await asyncio.sleep(1)
                                        except Exception as reconnect_dl_err:
                                            logging.error(f"Reconnect after download TCP error failed: {reconnect_dl_err}")
                                            path = None
                                            break
                                else:
                                    path = None
                                    break
                            except (FileReferenceExpired, FileReferenceInvalid, _FileRefSwallowedByPyrogram) as e:
                                if _dl_attempt < 1:
                                    logging.warning(f"File reference expired — re-fetching message {current_msg.id} for a fresh reference")
                                    try:
                                        refreshed = await user_client.get_messages(chat_id, current_msg.id, replies=0)
                                        if refreshed and getattr(refreshed, "id", None):
                                            current_msg = refreshed
                                    except Exception as _ref_err:
                                        logging.warning(f"Message re-fetch failed: {_ref_err}")
                                else:
                                    logging.error(f"File reference still expired after re-fetch: {e}")
                                    await update_status(status_msg, "❌ This file's link has expired. Please send the original Telegram link again.")
                                    path = None
                                    break
                            except (sqlite3.ProgrammingError, AuthBytesInvalid) as e:
                                # Session storage was closed or cross-DC auth is stale —
                                # evict the dead client and reconnect once before giving up.
                                logging.warning(f"Session error during download for user {user_id} ({type(e).__name__}), reconnecting")
                                stale = user_clients.pop(user_id, None)
                                if stale:
                                    try:
                                        await stale["client"].stop()
                                    except Exception:
                                        pass
                                if _dl_attempt < 1:
                                    session_str_dl = user.get("phone_session_string") if user else None
                                    if session_str_dl:
                                        try:
                                            user_client = await get_user_client(user_id, session_str_dl)
                                            await asyncio.sleep(1)
                                        except Exception as reconnect_dl_err:
                                            logging.error(f"Reconnect after session error failed: {reconnect_dl_err}")
                                            path = None
                                            break
                                else:
                                    path = None
                                    break
                            except Exception as e:
                                if str(e) == "StopProcess":
                                    raise e
                                logging.error(f"Download crash: {e}")
                                path = None
                                break

                        if not path or not os.path.exists(path):
                            logging.error(f"Download failed or file missing: {path}")
                            continue

                        if user_id in cancel_flags:
                            raise Exception("StopProcess")

                        await update_status(status_msg, "📤 Uploading...")

                        sent_msg = await upload_media_fast(
                            upload_client,
                            destination_id,
                            path,
                            caption=safe_caption,
                            thumb=thumb_path,
                            duration=duration,
                            width=width,
                            height=height,
                            file_name=orig_file_name,
                            has_spoiler=has_spoiler,
                            progress_callback=progress_bar,
                            progress_args=(status_msg, "📤 Uploading", status_msg_override is None)
                        )


                        if sent_msg:
                            await send_to_dump(client, user_id, link, sent_msg)

                        processed_count += 1
                    except Exception as e:
                        error_str = str(e)
                        if "AUTH_KEY_UNREGISTERED" in error_str or "SESSION_REVOKED" in error_str or "401" in error_str:
                            is_revoked = "SESSION_REVOKED" in error_str
                            stale = user_clients.pop(user_id, None)
                            if stale:
                                try:
                                    await stale["client"].stop()
                                except Exception:
                                    pass
                            if is_revoked:
                                from bot.database import update_user
                                await update_user(user_id, {"phone_session_string": None})
                                if user_id in _batch_sessions:
                                    _batch_session_error_flags.add(user_id)
                                await update_status(status_msg, "❌ Your session was revoked. Please /login again.")
                            else:
                                logging.warning(f"AuthKeyUnregistered (possible PFS rotation) for user {user_id} — evicting client, session preserved")
                                await update_status(status_msg, "❌ Connection interrupted. Please send the link again.")
                            return None

                        if str(e) == "StopProcess":
                            cancel_flags.discard(user_id)
                            if path:
                                if isinstance(path, list):
                                    for p in path:
                                        if os.path.exists(p): os.remove(p)
                                elif os.path.exists(path):
                                    os.remove(path)
                            if thumb_path and isinstance(thumb_path, str) and os.path.exists(thumb_path):
                                os.remove(thumb_path)
                            await update_status(status_msg, "🛑 Process cancelled.")
                            return None

                        if isinstance(e, AttributeError) or "'NoneType' object has no attribute 'write'" in error_str:
                            # Upload reference lost (usually after a cancelled/interrupted save_file)
                            logging.error(f"Upload state corrupted (skipping item): {e}")
                            continue

                        # Session torn down mid-upload (NoneType iterable / TCPTransport closed).
                        # Evict the dead client so the next request gets a fresh one.
                        _is_upload_session_err = (
                            isinstance(e, (sqlite3.ProgrammingError, AuthBytesInvalid))
                            or (isinstance(e, OSError) and (
                                ("NoneType" in error_str and "iterable" in error_str)
                                or ("TCPTransport" in error_str and "closed=True" in error_str)
                            ))
                        )
                        if _is_upload_session_err:
                            logging.warning(f"Upload session error for user {user_id} ({type(e).__name__}): {e} — evicting client")
                            stale = user_clients.pop(user_id, None)
                            if stale:
                                try:
                                    await stale["client"].stop()
                                except Exception:
                                    pass
                            continue

                        if "Unknown media" in error_str or "unknown media" in error_str.lower():
                            logging.warning(f"Unsupported media type for user {user_id}: {e}")
                            await update_status(status_msg, "❌ This media type is not supported for download.")
                            continue

                        if isinstance(e, asyncio.TimeoutError):
                            logging.error(f"Upload stuck/timed out (45 min) for user {user_id} — aborting")
                            await update_status(status_msg, "❌ Upload timed out — transfer appeared stuck. Please try again.")
                        elif isinstance(e, (FloodWait, FloodPremiumWait)):
                            logging.error(f"Download/Upload error: {e}")
                            await update_status(status_msg, f"⏳ Telegram rate limit hit. Please try again later.")
                        elif "Can't upload files bigger" in str(e) or "File size" in str(e):
                            logging.error(f"File size error: {e}")
                            await update_status(status_msg, "❌ File is too large (exceeds 2GB limit).")
                        else:
                            logging.error(f"Download/Upload error: {e}")
                        continue
                    finally:
                        if path:
                            if isinstance(path, list):
                                for p in path:
                                    if os.path.exists(p): os.remove(p)
                            elif os.path.exists(path):
                                os.remove(path)
                        if thumb_path and isinstance(thumb_path, str) and os.path.exists(thumb_path):
                            os.remove(thumb_path)

                if status_msg_override is None:
                    # Confirmation message for single / media-group (never for batch)
                    try:
                        if using_user_session:
                            if destination_id == "me":
                                conf_text = "✅ File uploaded to your **Saved Messages**!"
                            else:
                                conf_text = (
                                    f"✅ File uploaded to your private channel!\n\n"
                                    f"**Channel ID:** `{destination_id}`"
                                )
                        else:
                            conf_text = "✅ File forwarded successfully!"
                        await client.send_message(user_id, conf_text)
                    except Exception as _conf_err:
                        logging.debug(f"Confirmation message failed: {_conf_err}")
                    await status_msg.delete()
                return msg if processed_count > 0 else None
            finally:
                active_downloads.discard(user_id)
                if hasattr(progress_bar, "data"):
                    progress_bar.data.pop(status_msg.id, None)

    except Exception as e:
        error_str = str(e)
        if any(kw in error_str for kw in ["AUTH_KEY_UNREGISTERED", "SESSION_REVOKED", "401"]):
            is_revoked = "SESSION_REVOKED" in error_str
            logging.error(f"Session error for {user_id}: {error_str}")
            if is_revoked:
                from bot.database import logout_user
                await logout_user(user_id)
            else:
                logging.warning(f"AuthKeyUnregistered (possible PFS rotation) for user {user_id} — evicting client only, session preserved")
            stale = user_clients.pop(user_id, None)
            if stale:
                try:
                    await stale["client"].stop()
                except:
                    pass
            # Signal the batch loop to abort so remaining items don't all hit the same error
            if user_id in _batch_sessions:
                _batch_session_error_flags.add(user_id)
            if 'status_msg' in locals():
                try:
                    if is_revoked:
                        await update_status(status_msg, "❌ Your Telegram session was revoked. Please log in again using /login.")
                    else:
                        await update_status(status_msg, "❌ Connection interrupted. Please send the link again.")
                except:
                    pass
            return None
        
        logging.error(f"Download handler error: {e}")
        if 'status_msg' in locals():
            try:
                await update_status(status_msg, f"❌ Error: {str(e)}")
            except:
                pass
    finally:
        active_downloads.discard(user_id)
        active_sessions.discard(user_id)
        cancel_flags.discard(user_id)
        if 'status_msg' in locals() and hasattr(progress_bar, "data"):
            progress_bar.data.pop(status_msg.id, None)

@app.on_callback_query(filters.regex("upgrade_prompt"))
async def upgrade_prompt_callback(client, callback_query):
    await upgrade(client, callback_query.message)
    await callback_query.answer()

@app.on_message(filters.command("upgrade") & filters.private)
async def upgrade(client, message):
    from bot.config import OWNER_USERNAME, SUPPORT_CHAT_LINK, UPI_ID, PAYPAL_LINK, APPLE_PAY_ID, CRYPTO_ADDRESS, CARD_PAYMENT_LINK
    text = (
        "💎 **Premium Plans**\n\n"
        "⚡ **Standard**\n"
        "•———————————————•\n"
        "🔸 **10** days - **$2**\n"
        "🔸 **30** days - **$3**\n"
        "🔸 **60** days - **$6**\n"
        "•———————————————•\n"
        "• Unlimited Downloads\n"
        "• Batch Download upto (50)\n"
        "• Multiple Links upto (50)\n"
        "• Fast Speed\n\n"
        "> 🔥 **1 Year** - $30\n"
        "> • All Premium Features\n"
        "> • Priority Support\n\n"
        "> 💳 **Payment Details**\n"
        f"🪙 **Crypto(Binance)**: [Crpto Payment / Binance]({CRYPTO_ADDRESS})\n\n"
        f"🇮🇳 **UPI**: [UPI QrCode]({UPI_ID})\n\n"
        f"💲 **PayPal**: **[Click Here for PayPal]({PAYPAL_LINK})**\n\n"
        f"🍎 **Apple Pay**: **[Click Here for Apple Pay]({APPLE_PAY_ID})**\n\n"
        f"💳 **Card**: **[Click Here for Card]({CARD_PAYMENT_LINK})**\n\n"
        f"> **🚀 After payment, send a screenshot to: ♦️ @Wolfy0046**"
    )
    await message.reply(
        text,
        link_preview_options=LinkPreviewOptions(is_disabled=True),
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Owner", url=f"https://t.me/Wolfy0046")],
            [InlineKeyboardButton("Support Chat", url=SUPPORT_CHAT_LINK)]
        ])
    )
