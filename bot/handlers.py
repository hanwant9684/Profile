import asyncio
import os
import time
import io
import aiofiles
import re
import logging
import pyrogram
from pyrogram import filters, Client
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, Message, LinkPreviewOptions
from pyrogram.errors import AuthKeyUnregistered, FloodWait, FloodPremiumWait
from bot.config import (
    app, API_ID, API_HASH, active_downloads, global_download_semaphore, 
    OWNER_ID, global_upload_semaphore, cancel_flags
)

# Dump channel 
async def send_to_dump(client, user_id, link, msg):
    """Fetches dump channel from database and sends a copy"""
    # 1. Fetch the setting from the database
    setting = await get_setting("dump_channel_id")
    dump_id = setting.get('value') if setting else None

    if not dump_id:
        logging.warning("Dump channel not set in database.")
        return

    try:
        # Ensure the ID is an integer for Telegram
        dump_id = int(dump_id)
        
        # 2. Create the Header
        header = f"👤 **User:** `{user_id}`\n🔗 **Link:** {link}\n\n"
        original_caption = msg.caption or ""
        full_caption = (header + original_caption)[:1020]

        if msg.media_group_id:
            # For albums
            await client.copy_media_group(dump_id, msg.chat.id, msg.id)
            await client.send_message(dump_id, header + "⚠️ Album above)")
        else:
            # For single files
            await msg.copy(dump_id, caption=full_caption)
            
    except ValueError:
        logging.error(f"Invalid Dump ID format in database: {dump_id}")
    except Exception as e:
        logging.error(f"Dump failed: {e}")
        
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
        in_memory=True,
        sleep_threshold=60,
        takeout=True,
        no_updates=True
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
    if not hasattr(progress_bar, "data"):
        progress_bar.data = {}

    user_id = message.chat.id
    if user_id in cancel_flags:
        # DO NOT discard here, just raise. 
        # The handler will discard it when it catches StopProcess.
        raise Exception("StopProcess")

    if total == 0:
        return

    now = time.time()

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

    # Simple timer and percentage threshold
    time_diff = now - data["last_edit"]
    if time_diff < 2:
        return

    last_percentage = data.get("last_percentage", 0)
    percentage_diff = percentage - last_percentage

    data["last_percentage"] = percentage
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

    start_match = re.search(r"t\.me/c/(\d+)/(\d+)", start_link) or re.search(r"t\.me/(?!c/)([^/]+)/(\d+)", start_link)
    end_match = re.search(r"t\.me/c/(\d+)/(\d+)", end_link) or re.search(r"t\.me/(?!c/)([^/]+)/(\d+)", end_link)

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
        
        # Random delay between messages in batch to further reduce FloodWait risk
        import random
        await asyncio.sleep(random.uniform(2, 5))

        try:
            result = await download_handler(client, message, link_override=link, processed_albums=processed_albums)
            # Add a safety delay between messages in batch to avoid FloodWait
            if result:
                await asyncio.sleep(3) 
        except Exception as e:
            logging.error(f"Batch loop error for link {link}: {e}")
            continue

@app.on_message(filters.regex(r"https://t\.me/") & filters.private)
async def download_handler(client, message, link_override=None, processed_albums=None):
    user_id = message.from_user.id
    link = link_override or message.text.strip()

    user = await get_user(user_id)
    if user and user.get("role") == "banned":
        await message.reply("❌ **You are banned from using this bot.**")
        return

    chat_id = None
    message_id = None

    private_match = re.search(r"t\.me/c/(\d+)/(\d+)", link)
    public_match = re.search(r"t\.me/(?!c/)([^/]+)/(\d+)", link)
    public_topic_match = re.search(r"t\.me/(?!c/)([^/]+)/(\d+)/(\d+)", link)
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
        is_private = True
        is_group = True
    elif private_comment_match:
        temp_channel_id = int("-100" + private_comment_match.group(1))
        comment_id = int(private_comment_match.group(3))
        is_private = True
        is_group = True
        try:
            chat_info = await client.get_chat(temp_channel_id)
            if chat_info.linked_chat:
                chat_id = chat_info.linked_chat.id # Now targets the correct Private Group
                message_id = comment_id
            else:
                chat_id = temp_channel_id
                message_id = comment_id
        except Exception:
            chat_id = temp_channel_id
            message_id = comment_id
    elif comment_match:
        temp_channel = comment_match.group(1)
        comment_id = int(comment_match.group(3))
        is_private = True
        is_group = True
        try:
            chat_info = await client.get_chat(temp_channel)
            if chat_info.linked_chat:
                chat_id = chat_info.linked_chat.id # Use the GROUP ID instead
                message_id = comment_id
            else:
                chat_id = temp_channel
                message_id = comment_id
        except Exception:
            chat_id = temp_channel
            message_id = comment_id
    elif private_thread_match:
        chat_id = int("-100" + private_thread_match.group(1))
        message_id = int(private_thread_match.group(2))
        is_private = True
        is_group = True
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
        is_group = True
    elif public_topic_match:
        chat_id = public_topic_match.group(1)
        message_id = int(public_topic_match.group(3))
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

            processed_count = 0
            
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
                    if is_story:
                        msg = await user_client.get_stories(chat_id, message_id)
                    else:
                        msg = await user_client.get_messages(chat_id, message_id)
                except AuthKeyUnregistered:
                    from bot.database import update_user
                    await update_user(user_id, {"phone_session_string": None})
                    await status_msg.edit_text("❌ Your session has expired. Please /login again.")
                    return None
                except (FloodWait, FloodPremiumWait) as e:
                    logging.warning(f"FloodWait on get_messages: {e.value}s")
                    await asyncio.sleep(e.value)
                    msg = await user_client.get_messages(chat_id, message_id)
                except Exception as e:
                    await status_msg.edit_text(f"❌ Error fetching message: {str(e)}")
                    return None

                if not msg or (not getattr(msg, "media", None) and not getattr(msg, "text", None) and type(msg).__name__ != "Story"):
                    await status_msg.edit_text("❌ No content found in link.")
                    return None

                media_group_id = getattr(msg, "media_group_id", None)
                if processed_albums is not None and media_group_id:
                    if media_group_id in processed_albums:
                        await status_msg.delete()
                        return msg.id
                    processed_albums.add(media_group_id)

                can_download, quota_status = await check_and_update_quota(user_id)

                if not can_download:
                    await status_msg.edit_text(f"❌ {quota_status}")
                    return None
                if not is_story and getattr(msg, "media_group_id", None):
                    target_messages = await user_client.get_media_group(chat_id, message_id)
                else:
                    target_messages = [msg]

                user_data = await get_user(user_id)
                if user_data.get("role") == "free":
                    await increment_quota(user_id, len(target_messages))

                if not is_private and not is_group and not is_story:
                    try:
                        await status_msg.edit_text("🚀 Extracting directly...")
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
                        await status_msg.delete()
                        return msg 
                    except Exception as e:
                        logging.error(f"Direct extraction failed: {e}")
                        await status_msg.edit_text("⚠️ Direct extraction failed, falling back to download/upload...")

                for current_msg in target_messages:
                    path = None
                    thumb_path = None
                    safe_caption = ""
                    if current_msg.caption:
                        safe_caption = current_msg.caption
                    elif hasattr(current_msg, "text") and current_msg.text:
                        safe_caption = current_msg.text

                    if not getattr(current_msg, "media", None) and type(current_msg).__name__ != "Story":
                        try:
                            await client.send_message(user_id, safe_caption)
                            await send_to_dump(client, user_id, link, current_msg)
                            processed_count += 1
                            continue
                        except Exception as e:
                            logging.error(f"Error sending text-only message: {e}")
                            continue

                    try:
                        if hasattr(current_msg, "video") and current_msg.video and getattr(current_msg.video, "thumbs", None):
                            try:
                                thumb_path = await user_client.download_media(current_msg.video.thumbs[-1])
                            except Exception as e:
                                logging.debug(f"Thumb download error: {e}")
                        elif hasattr(current_msg, "document") and current_msg.document and getattr(current_msg.document, "thumbs", None):
                            try:
                                thumb_path = await user_client.download_media(current_msg.document.thumbs[-1])
                            except Exception as e:
                                logging.debug(f"Thumb download error: {e}")

                        duration = 0
                        width = 0
                        height = 0

                        if hasattr(current_msg, "video") and current_msg.video:
                            duration = getattr(current_msg.video, "duration", 0) or 0
                            width = getattr(current_msg.video, "width", 0) or 0
                            height = getattr(current_msg.video, "height", 0) or 0
                        elif hasattr(current_msg, "document") and current_msg.document and current_msg.document.mime_type and current_msg.document.mime_type.startswith("video/"):
                            duration = getattr(current_msg.document, "duration", 0) or 0
                            width = getattr(current_msg.document, "width", 0) or 0
                            height = getattr(current_msg.document, "height", 0) or 0

                        try:
                            path = await download_media_fast(
                                user_client,
                                current_msg,
                                None,
                                progress_callback=progress_bar,
                                progress_args=(status_msg, "📥 Downloading")
                            )
                        except (FloodWait, FloodPremiumWait) as e:
                            logging.warning(f"FloodWait on download: {e.value}s")
                            await asyncio.sleep(e.value)
                            path = await download_media_fast(
                                user_client,
                                current_msg,
                                None,
                                progress_callback=progress_bar,
                                progress_args=(status_msg, "📥 Downloading")
                            )
                        except Exception as e:
                            if str(e) == "StopProcess":
                                raise e
                            logging.error(f"Download crash: {e}")
                            path = None

                        if not path or not os.path.exists(path):
                            logging.error(f"Download failed or file missing: {path}")
                            continue

                        if user_id in cancel_flags:
                            raise Exception("StopProcess")

                        await status_msg.edit_text("📤 Uploading...")

                        sent_msg = await upload_media_fast(
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
                        if sent_msg:
                            await send_to_dump(client, user_id, link, sent_msg)

                        processed_count += 1
                    except Exception as e:
                        if str(e) == "StopProcess":
                            cancel_flags.discard(user_id)
                            if path and os.path.exists(path):
                                os.remove(path)
                            if thumb_path and os.path.exists(thumb_path):
                                os.remove(thumb_path)
                            try:
                                await status_msg.edit_text("🛑 Process cancelled.")
                            except Exception:
                                pass
                            return None
                        logging.error(f"Download/Upload error: {e}")
                        continue
                    finally:
                        if path and os.path.exists(path):
                            os.remove(path)
                        if thumb_path and os.path.exists(thumb_path):
                            os.remove(thumb_path)

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
    from bot.config import OWNER_USERNAME, SUPPORT_CHAT_LINK, UPI_ID, PAYPAL_LINK, APPLE_PAY_ID, CRYPTO_ADDRESS, CARD_PAYMENT_LINK
    text = (
        "💎 **Premium Plans**\n\n"
        "⚡ **Standard**\n"
        "•———————————————•\n"
        "🔸 **7** days - **$1**\n"
        "🔸 **14** days - **$1.5**\n"
        "🔸 **30** days - **$2**\n"
        "•———————————————•\n"
        "• Unlimited Downloads\n"
        "• Batch Download upto (50)\n"
        "• Fast Speed\n\n"
        "🔥 **Lifetime** - $25\n"
        "• All Premium Features\n"
        "• Priority Support\n\n"
        "💳 **Payment Details**\n"
        f"🪙 **Crypto(Binance)**: `{CRYPTO_ADDRESS}`\n\n"
        f"🇮🇳 **UPI**: [UPI QrCode]({UPI_ID})\n\n"
        f"💲 **PayPal**: **[Click Here for PayPal]({PAYPAL_LINK})**\n\n"
        f"🍎 **Apple Pay**: **[Click Here for Apple Pay]({APPLE_PAY_ID})**\n\n"
        f"💳 **Card**: **[Click Here for Card]({CARD_PAYMENT_LINK})**\n\n"
        f"🚀 After payment, send a screenshot to: @{OWNER_USERNAME}"
    )
    await message.reply(
        text,
        link_preview_options=LinkPreviewOptions(is_disabled=True),
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Owner", url=f"https://t.me/Wolfy0046")],
            [InlineKeyboardButton("Support Chat", url=SUPPORT_CHAT_LINK)]
        ])
    )
