import asyncio
import os
import time
import io
import aiofiles
import re
import logging
from hydrogram import filters, Client
from hydrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, Message
from bot.config import (
    app, API_ID, API_HASH, active_downloads, global_download_semaphore, 
    OWNER_ID, global_upload_semaphore, cancel_flags
)

# Session caching dictionary: {user_id: {"client": Client, "last_used": timestamp}}
user_clients = {}
_cleanup_task_started = False

async def get_user_client(user_id, session_str):
    global _cleanup_task_started
    now = time.time()
    if user_id in user_clients:
        user_clients[user_id]["last_used"] = now
        return user_clients[user_id]["client"]
    
    client = Client(
        f"user_{user_id}",
        session_string=session_str,
        api_id=API_ID,
        api_hash=API_HASH,
        in_memory=True
    )
    await client.start()
    user_clients[user_id] = {"client": client, "last_used": now}
    
    if not _cleanup_task_started:
        asyncio.create_task(cleanup_user_clients())
        _cleanup_task_started = True
    return client

async def cleanup_user_clients():
    while True:
        await asyncio.sleep(60)
        now = time.time()
        to_remove = []
        for user_id, data in user_clients.items():
            if now - data["last_used"] > 600: # 10 minutes
                to_remove.append(user_id)
        
        for user_id in to_remove:
            client = user_clients.pop(user_id)["client"]
            try:
                await client.stop()
            except Exception:
                pass

from bot.database import get_user, check_and_update_quota, increment_quota, get_setting, get_remaining_quota
from bot.ads import show_ad
from bot.transfer import download_media_fast, upload_media_fast

async def progress_bar(current, total, message, type_msg):
    if total == 0:
        return
    
    now = time.time()
    if not hasattr(progress_bar, "data"):
        setattr(progress_bar, "data", {})
    
    msg_id = message.id
    if msg_id not in progress_bar.data:
        progress_bar.data[msg_id] = {
            "last_val": 0,
            "last_time": now,
            "start_time": now,
            "last_edit": 0
        }
    
    data = progress_bar.data[msg_id]
    percentage = current * 100 / total
    
    if current != total and (now - data["last_edit"]) < 2:
        return
    
    elapsed_time = now - data["start_time"]
    if elapsed_time > 0:
        speed = current / elapsed_time
    else:
        speed = 0
        
    if speed > 0:
        remaining_bytes = total - current
        eta = remaining_bytes / speed
    else:
        eta = 0

    def format_size(size):
        for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
            if size < 1024.0:
                return f"{size:.2f} {unit}"
            size /= 1024.0
        return f"{size:.2f} TB"

    def format_time(seconds):
        if seconds <= 0: return "0s"
        minutes, seconds = divmod(int(seconds), 60)
        hours, minutes = divmod(minutes, 60)
        if hours > 0: return f"{hours}h {minutes}m {seconds}s"
        if minutes > 0: return f"{minutes}m {seconds}s"
        return f"{seconds}s"

    speed_str = format_size(speed) + "/s"
    eta_str = format_time(eta)
    
    completed = int(percentage / 10)
    bar = "█" * completed + "░" * (10 - completed)
    
    text = (
        f"**{type_msg}**\n"
        f"[{bar}] {percentage:.1f}%\n"
        f"🚀 **Speed:** `{speed_str}`\n"
        f"⏳ **ETA:** `{eta_str}`\n"
        f"📦 **Size:** `{format_size(current)} / {format_size(total)}`"
    )

    if current == total:
        progress_bar.data.pop(msg_id, None)
        try:
            await message.edit_text(f"**{type_msg} Completed!**\n📦 **Total Size:** `{format_size(total)}`")
        except Exception:
            pass
    else:
        data["last_edit"] = now
        try:
            await message.edit_text(text)
        except Exception:
            pass

async def verify_force_sub(client, user_id):
    setting = await get_setting("force_sub_channel")
    if not setting or not setting.get('value'):
        return True, None
        
    channel = setting['value']
    if not channel.startswith("@") and not channel.startswith("-100"):
        channel = f"@{channel}"
        
    try:
        member = await client.get_chat_member(channel, user_id)
        if member.status in ["left", "kicked"]:
             return False, channel
        return True, None
    except Exception:
        return False, channel

@app.on_message(filters.command("help") & filters.private)
async def help_command(client, message):
    help_text = (
        "📖 **Help Menu**\n\n"
        "🔗 **Downloads**\n"
        "Just send any Telegram link (public or private) to download.\n"
        "For private links, you must /login first.\n\n"
        "📦 **Batch**\n"
        "Format: `/batch start_link end_link` (Max 50)\n\n"
        "💰 **Quota**\n"
        "Free users: 5 files/day\n"
        "Premium users: Unlimited"
    )
    await message.reply(help_text)

@app.on_message(filters.command("batch") & filters.private)
async def batch_handler(client, message):
    parts = message.text.split()
    if len(parts) < 3:
        await message.reply("❌ Usage: `/batch start_link end_link`")
        return

    user_id = message.from_user.id
    user = await get_user(user_id)
    if user.get('role', 'free') == 'free':
        await message.reply("❌ Batch command is for Premium users only.")
        return

    start_link = parts[1]
    end_link = parts[2]
    
    start_match = re.search(r"t\.me/([^/]+)/(\d+)", start_link) or re.search(r"t\.me/c/(\d+)/(\d+)", start_link)
    end_match = re.search(r"t\.me/([^/]+)/(\d+)", end_link) or re.search(r"t\.me/c/(\d+)/(\d+)", end_link)
    
    if not start_match or not end_match:
        await message.reply("❌ Invalid links provided.")
        return
        
    start_id = int(start_match.group(2))
    end_id = int(end_match.group(2))
    
    if start_id > end_id:
        start_id, end_id = end_id, start_id
        
    count = end_id - start_id + 1
    if count > 50:
        await message.reply("⚠️ You can only batch up to 50 messages at a time.")
        return
        
    await message.reply(f"🚀 Starting batch download of {count} messages...")
    
    processed_albums = set()
    for msg_id in range(start_id, end_id + 1):
        if user_id in cancel_flags:
            cancel_flags.discard(user_id)
            await message.reply("🛑 Batch cancelled by user.")
            return

        if "t.me/c/" in start_link:
            link = f"https://t.me/c/{start_match.group(1)}/{msg_id}"
        else:
            link = f"https://t.me/{start_match.group(1)}/{msg_id}"
        
        try:
            result = await download_handler(client, message, link_override=link, processed_albums=processed_albums)
            if result and not isinstance(result, int):
                 await asyncio.sleep(4)
            elif result:
                 pass
            else:
                 await asyncio.sleep(2)
        except Exception as e:
            logging.error(f"Batch loop error for link {link}: {e}")
            continue

@app.on_message(filters.regex(r"https://t\.me/") & filters.private)
async def download_handler(client, message, link_override=None, processed_albums=None):
    user_id = message.from_user.id
    link = link_override or message.text.strip()
    
    chat_id = None
    message_id = None
    
    public_match = re.search(r"t\.me/([^/]+)/(\d+)", link)
    private_match = re.search(r"t\.me/c/(\d+)/(\d+)", link)
    topic_match = re.search(r"t\.me/c/(\d+)/(\d+)/(\d+)", link)
    comment_match = re.search(r"t\.me/([^/]+)/(\d+)\?comment=(\d+)", link)
    private_comment_match = re.search(r"t\.me/c/(\d+)/(\d+)\?comment=(\d+)", link)
    story_match = re.search(r"t\.me/([^/]+)/s/(\d+)", link)
    private_story_match = re.search(r"t\.me/c/(\d+)/s/(\d+)", link)
    single_match = re.search(r"t\.me/([^/]+)/(\d+)\?single", link)
    private_single_match = re.search(r"t\.me/c/(\d+)/(\d+)\?single", link)
    thread_match = re.search(r"t\.me/([^/]+)/(\d+)\?thread=(\d+)", link)
    private_thread_match = re.search(r"t\.me/c/(\d+)/(\d+)\?thread=(\d+)", link)

    is_private = False
    is_group = False
    is_story = False

    if private_story_match:
        chat_id = int("-100" + private_story_match.group(1))
        message_id = int(private_story_match.group(2))
        is_private = True
        is_story = True
    elif story_match:
        chat_id = story_match.group(1)
        message_id = int(story_match.group(2))
        is_story = True
    elif private_comment_match:
        chat_id = int("-100" + private_comment_match.group(1))
        message_id = int(private_comment_match.group(3))
        is_private = True
    elif comment_match:
        chat_id = comment_match.group(1)
        message_id = int(comment_match.group(3))
    elif private_thread_match:
        chat_id = int("-100" + private_thread_match.group(1))
        message_id = int(private_thread_match.group(2))
        is_private = True
    elif thread_match:
        chat_id = thread_match.group(1)
        message_id = int(thread_match.group(2))
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
        is_private = True
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
            chat = await asyncio.wait_for(client.get_chat(chat_id), timeout=5)
            chat_type_str = str(chat.type).lower()
            if "group" in chat_type_str or "supergroup" in chat_type_str:
                is_group = True
            elif hasattr(chat, "broadcast") and chat.broadcast is False:
                 is_group = True
        except Exception as e:
            logging.debug(f"Chat check error for {chat_id}: {e}")
            pass

    status_msg = await message.reply("⏳ Processing...")
    user = await get_user(user_id)
    
    if (is_private or is_group) and (not user or not user.get('phone_session_string')):
        await status_msg.edit_text("❌ Login is required for private links. Use /login.")
        return

    user_client = None

    try:
        async with global_download_semaphore:
            if user_id in active_downloads:
                await status_msg.edit_text("⚠️ You already have an active download. Please wait.")
                return None
            active_downloads.add(user_id)
            try:
                if is_private or is_group or is_story:
                    session_str = user.get('phone_session_string') if user else None
                    if session_str:
                        user_client = await get_user_client(user_id, session_str)
                else:
                    user_client = client

                if not user_client:
                    await status_msg.edit_text("❌ Session error. Please /login again.")
                    return None 

                try:
                    msg = await user_client.get_messages(chat_id, message_id)
                except Exception as e:
                    await status_msg.edit_text(f"❌ Error fetching message: {str(e)}")
                    return None
                
                if not msg or not msg.media:
                    await status_msg.edit_text("❌ No media found in link.")
                    return None

                if processed_albums is not None and msg.media_group_id:
                    if msg.media_group_id in processed_albums:
                        await status_msg.delete()
                        return msg.id
                    processed_albums.add(msg.media_group_id)

                remaining_quota, is_premium = await get_remaining_quota(user_id)
                processed_count = 0
                if msg.media_group_id:
                    album_messages = await user_client.get_media_group(chat_id, message_id)
                    album_size = len(album_messages)
                    if remaining_quota < album_size:
                        await status_msg.edit_text(f"❌ Not enough quota for album. Needed: {album_size}, Available: {remaining_quota}")
                        return None
                    target_messages = album_messages
                else:
                    if remaining_quota < 1:
                        await status_msg.edit_text(f"❌ Not enough quota. Available: {remaining_quota}")
                        return None
                    target_messages = [msg]

                if not is_private and not is_group and not is_story:
                    try:
                        await status_msg.edit_text("🚀 Extracting directly...")
                        if msg.media_group_id:
                            await client.copy_media_group(chat_id=user_id, from_chat_id=chat_id, message_id=message_id)
                            processed_count = len(target_messages)
                        else:
                            await msg.copy(chat_id=user_id)
                            processed_count = 1
                        await increment_quota(user_id, processed_count)
                        await status_msg.delete()
                        return msg 
                    except Exception as e:
                        logging.error(f"Direct extraction failed: {e}")
                        await status_msg.edit_text("⚠️ Direct extraction failed, falling back to download/upload...")

                for current_msg in target_messages:
                    path = None
                    thumb_path = None
                    try:
                        if hasattr(current_msg, "video") and current_msg.video and current_msg.video.thumbs:
                            try:
                                thumb_path = await user_client.download_media(current_msg.video.thumbs[-1])
                            except Exception as e:
                                logging.debug(f"Thumb download error: {e}")
                        elif hasattr(current_msg, "document") and current_msg.document and current_msg.document.thumbs:
                            try:
                                thumb_path = await user_client.download_media(current_msg.document.thumbs[-1])
                            except Exception as e:
                                logging.debug(f"Thumb download error: {e}")

                        duration = 0
                        width = 0
                        height = 0
                        
                        if current_msg.video:
                            duration = current_msg.video.duration or 0
                            width = current_msg.video.width or 0
                            height = current_msg.video.height or 0
                        elif current_msg.document and current_msg.document.mime_type and current_msg.document.mime_type.startswith("video/"):
                            if hasattr(current_msg.document, "duration"):
                                duration = current_msg.document.duration or 0
                            if hasattr(current_msg.document, "width"):
                                width = current_msg.document.width or 0
                            if hasattr(current_msg.document, "height"):
                                height = current_msg.document.height or 0

                        path = await download_media_fast(
                            user_client,
                            current_msg,
                            None,
                            progress_callback=progress_bar,
                            progress_args=(status_msg, "📥 Downloading")
                        )
                        if path is None:
                            continue

                        original_caption = current_msg.caption if current_msg and hasattr(current_msg, "caption") else ""
                        safe_caption = str(original_caption) if original_caption is not None else ""

                        await status_msg.edit_text("📤 Uploading...")
                        
                        await upload_media_fast(
                            client,
                            user_id,
                            path,
                            caption=safe_caption,
                            thumb=thumb_path,
                            duration=duration,
                            width=width,
                            height=height,
                            progress_callback=progress_bar,
                            progress_args=(status_msg, "📤 Uploading")
                        )
                        
                        processed_count += 1
                    finally:
                        if path and os.path.exists(path):
                            os.remove(path)
                        if thumb_path and os.path.exists(thumb_path):
                            os.remove(thumb_path)
                
                await increment_quota(user_id, processed_count)
                await status_msg.delete()
                return msg 
            finally:
                active_downloads.discard(user_id)
                if hasattr(progress_bar, "data"):
                    progress_bar.data.pop(status_msg.id, None)

    except Exception as e:
        logging.error(f"Download handler error: {e}")
        if 'status_msg' in locals():
            try:
                await status_msg.edit_text(f"❌ Error: {str(e)}")
            except Exception:
                pass

@app.on_callback_query(filters.regex("upgrade_prompt"))
async def upgrade_prompt_callback(client, callback_query):
    await upgrade(client, callback_query.message)
    await callback_query.answer()

@app.on_message(filters.command("upgrade") & filters.private)
async def upgrade(client, message):
    from bot.config import OWNER_USERNAME, SUPPORT_CHAT_LINK
    text = (
        "💎 **Premium Plans**\n\n"
        "⚡ **Standard**\n"
        "🔸 30 days - **$2**\n\n"
        f"🚀 Contact @{OWNER_USERNAME} to upgrade."
    )
    await message.reply(
        text,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Owner", url=f"https://t.me/{OWNER_USERNAME}")],
            [InlineKeyboardButton("Support Chat", url=SUPPORT_CHAT_LINK)]
        ])
    )
