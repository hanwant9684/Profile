import asyncio
import os
import time
import io
import aiofiles
from pyrogram import filters, Client
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, Message
from bot.config import app, API_ID, API_HASH, active_downloads, global_download_semaphore
from bot.database import get_user, check_and_update_quota, increment_quota, get_setting, get_remaining_quota

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
    
    # Throttle updates: Update at most every 4 seconds to avoid bottleneck
    if current != total and (now - data["last_edit"]) < 4:
        return

    # Calculate speed (bytes per second)
    elapsed_time = now - data["start_time"]
    if elapsed_time > 0:
        speed = current / elapsed_time
    else:
        speed = 0
        
    # Calculate ETA
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
    
    # Progress bar visual
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
        except:
            pass
    else:
        data["last_edit"] = now
        try:
            await message.edit_text(text)
        except Exception:
            pass

async def verify_force_sub(client, user_id):
    from bot.config import OWNER_ID
    
    # Check database setting for force sub channel
    setting = await get_setting("force_sub_channel")
    if not setting or not setting.get('value'):
        return True, None
        
    channel = setting['value']
    # Ensure channel starts with @ for compatibility
    if not channel.startswith("@") and not channel.startswith("-100"):
        channel = f"@{channel}"
        
    try:
        member = await client.get_chat_member(channel, user_id)
        if member.status in ["left", "kicked"]:
             return False, channel
        return True, None
    except Exception as e:
        # If user is not in the channel, pyrogram raises an error
        # We catch it and return False to trigger the join prompt
        return False, channel

@app.on_message(filters.command("help") & filters.private)
async def help_command(client, message):
    help_text = (
        "📖 **Help Menu**\n\n"
        "🔗 **Downloads**\n"
        "Just send any Telegram link (public or private) to download.\n"
        "For private links, you must /login first.\n\n"
        "⚡ **Commands**\n"
        "• /start - Start the bot\n"
        "• /login - Connect your Telegram account\n"
        "• /logout - Disconnect your account\n"
        "• /myinfo - Check your account stats\n"
        "• /batch - Download multiple messages\n"
        "• /upgrade - View premium plans\n"
        "• /help - Show this menu\n"
    )
    await message.reply(help_text)

@app.on_message(filters.command("batch") & filters.private)
async def batch_command(client, message):
    user_id = message.from_user.id
    user = await get_user(user_id)
    
    if not user or user.get('role') == 'free':
        await message.reply("⛔ Batch download is for **Premium** users only. Use /upgrade to level up!")
        return
        
    try:
        parts = message.text.split()
        if len(parts) < 3:
             await message.reply("Usage: `/batch <start_link> <end_link>`")
             return
             
        start_link = parts[1]
        end_link = parts[2]
        
        import re
        start_match = re.search(r"t\.me/([^/]+)/(\d+)", start_link) or re.search(r"t\.me/c/(\d+)/(\d+)", start_link)
        end_match = re.search(r"t\.me/([^/]+)/(\d+)", end_link) or re.search(r"t\.me/c/(\d+)/(\d+)", end_link)
        
        if not start_match or not end_match:
            await message.reply("❌ Invalid links provided.")
            return
            
        chat_id = start_match.group(1)
        if "t.me/c/" in start_link:
            chat_id = int("-100" + chat_id)
            
        start_id = int(start_match.group(2))
        end_id = int(end_match.group(2))
        
        if start_id > end_id:
            start_id, end_id = end_id, start_id
            
        count = end_id - start_id + 1
        if count > 50:
            await message.reply("⚠️ You can only batch up to 50 messages at a time.")
            return
            
        await message.reply(f"🚀 Starting batch download of {count} messages...")
        
        for msg_id in range(start_id, end_id + 1):
            # Create a mock message to reuse download_handler logic
            mock_message = message
            mock_message.text = f"https://t.me/{start_match.group(1)}/{msg_id}"
            if "t.me/c/" in start_link:
                 mock_message.text = f"https://t.me/c/{start_match.group(1)}/{msg_id}"
            
            await download_handler(client, mock_message)
            # Increased delay to 10 seconds to prevent floodwait from Telegram
            await asyncio.sleep(10) 
            
    except Exception as e:
        await message.reply(f"❌ Batch error: {str(e)}")

@app.on_message(filters.regex(r"https://t\.me/") & filters.private)
async def download_handler(client, message):
    user_id = message.from_user.id
    
    # Check force sub before starting download
    is_subbed, channel = await verify_force_sub(client, user_id)
    if not is_subbed and channel:
        await message.reply(
            f"⛔ You must join our channel to use this bot.\n\n👉 {channel}",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Join Channel", url=f"https://t.me/{channel.replace('@', '')}")]
            ])
        )
        return

    allowed, msg_quota = await check_and_update_quota(user_id)
    if not allowed:
        await message.reply(
            f"⛔ {msg_quota}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💎 Upgrade to Premium", callback_data="upgrade_prompt")]])
        )
        return

    # Show RichAds for free users
    try:
        from bot.ads import show_ad
        await show_ad(client, user_id)
    except Exception as e:
        print(f"Error showing RichAds: {e}")

    if user_id in active_downloads:
        await message.reply("⚠️ You already have a download in progress. Please wait.")
        return

    active_downloads.add(user_id)
    status_msg = await message.reply("🔍 Checking link...")
    
    user_client = None
    path = None
    
    if global_download_semaphore.locked():
         await status_msg.edit_text("⚠️ Server busy. You are in the queue, please wait...")

    await global_download_semaphore.acquire()
    
    try:
        link = message.text.strip()
        
        import re
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
            # We treat the comment as the target message
            chat_id = int("-100" + private_comment_match.group(1))
            message_id = int(private_comment_match.group(3))
            is_private = True
        elif comment_match:
            chat_id = comment_match.group(1)
            message_id = int(comment_match.group(3))
        elif private_thread_match:
            chat_id = int("-100" + private_thread_match.group(1))
            message_id = int(private_thread_match.group(2)) # Thread link usually points to main post or thread id
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
            # Programmatically check if it's a channel or group
            try:
                # Use a small timeout for get_chat to avoid hanging
                chat = await asyncio.wait_for(client.get_chat(chat_id), timeout=10)
                # In Pyrogram/Hydrogram, ChatType.CHANNEL is for broadcast channels
                # ChatType.GROUP and ChatType.SUPERGROUP are for groups
                chat_type_str = str(chat.type).lower()
                if "group" in chat_type_str or chat.type in ["group", "supergroup"]:
                    is_group = True
                    print(f"[DEBUG] Detected as GROUP via type: {chat.type} for {chat_id}")
                else:
                    # Double check via broadcast attribute if available
                    if hasattr(chat, "broadcast") and chat.broadcast is False:
                         is_group = True
                         print(f"[DEBUG] Detected as GROUP via broadcast=False for {chat_id}")
                    else:
                         print(f"[DEBUG] Detected as CHANNEL via type: {chat.type} for {chat_id}")
            except Exception as e:
                print(f"Error checking chat type for {chat_id}: {e}")

        user = await get_user(user_id)
        
        if (is_private or is_group) and (not user or not user.get('phone_session_string') or len(user.get('phone_session_string', '')) < 10):
            msg_text = "❌ Login is mandatory for private channel links. Use /login to connect your account."
            if is_group:
                msg_text = "❌ Login is mandatory for public group links to download media. Use /login to connect your account."
            await status_msg.edit_text(msg_text)
            active_downloads.discard(user_id)
            global_download_semaphore.release()
            return

        # Handle user client session correctly with retry logic
        if is_private or is_group or is_story:
            session_str = user.get('phone_session_string') if user else None
            if session_str and len(session_str) > 10:
                max_retries = 3
                for attempt in range(max_retries):
                    try:
                        user_client = Client(
                            f"user_{user_id}_{int(time.time())}", 
                            session_string=session_str, 
                            in_memory=True, 
                            api_id=API_ID, 
                            api_hash=API_HASH,
                            no_updates=True
                        )
                        await user_client.start()
                        break
                    except Exception as e:
                        print(f"User client connection error (attempt {attempt + 1}/{max_retries}): {e}")
                        if user_client:
                            try:
                                await user_client.stop()
                            except:
                                pass
                        user_client = None
                        if attempt < max_retries - 1:
                            await asyncio.sleep(2)
            
            if not user_client:
                await status_msg.edit_text("❌ User session failed or not found. Please /login again.")
                active_downloads.discard(user_id)
                global_download_semaphore.release()
                return
        else:
            user_client = client
        
        await status_msg.edit_text("📥 Checking media...")
        
        if chat_id and message_id:
            try:
                if is_story:
                    # Bots cannot call get_stories directly (400 BOT_METHOD_INVALID)
                    # We must use the user_client (logged in user session) to fetch stories
                    if not user_client or user_client == client:
                         await status_msg.edit_text("❌ Login is mandatory for downloading stories. Use /login to connect your account.")
                         active_downloads.discard(user_id)
                         global_download_semaphore.release()
                         return
                    msg = await user_client.get_stories(chat_id, message_id)
                else:
                    msg = await user_client.get_messages(chat_id, message_id)
                
                if not msg:
                    print(f"[DEBUG] get_messages returned None for chat_id={chat_id}, message_id={message_id}")
                    await status_msg.edit_text("❌ Could not find message. Link might be invalid or expired.")
                    active_downloads.discard(user_id)
                    global_download_semaphore.release()
                    return
                
                messages_to_process = [msg]
                is_media_group = False
                
                # Story objects don't have media_group_id
                if not is_story and msg.media_group_id:
                    is_media_group = True
                    try:
                        media_group = await user_client.get_media_group(chat_id, message_id)
                        messages_to_process = media_group
                        print(f"[DEBUG] Found media group with {len(messages_to_process)} items")
                    except Exception as e:
                        print(f"[DEBUG] get_media_group failed: {e}, processing single message")
                        messages_to_process = [msg]
                
                remaining_quota, is_unlimited = await get_remaining_quota(user_id)
                total_files = len(messages_to_process)
                files_to_download = min(total_files, remaining_quota) if not is_unlimited else total_files
                quota_limited = files_to_download < total_files and not is_unlimited
                
                if files_to_download == 0:
                    await status_msg.edit_text(
                        "⛔ Daily limit reached (5/5). Upgrade to Premium for unlimited downloads.",
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💎 Upgrade to Premium", callback_data="upgrade_prompt")]])
                    )
                    active_downloads.discard(user_id)
                    global_download_semaphore.release()
                    return
                
                downloaded_count = 0
                for idx, media_msg in enumerate(messages_to_process[:files_to_download]):
                    from bot.config import cancel_flags
                    if user_id in cancel_flags:
                        await status_msg.edit_text("❌ Download cancelled by user.")
                        cancel_flags.discard(user_id)
                        active_downloads.discard(user_id)
                        global_download_semaphore.release()
                        return

                    if not media_msg.media:
                        if media_msg.text:
                            # Handle text-only messages
                            try:
                                sent_msg = await client.send_message(
                                    user_id,
                                    media_msg.text,
                                    entities=media_msg.entities
                                )
                                downloaded_count += 1
                                # Handle dumping for text messages
                                dump_id = os.environ.get("DUMP_CHANNEL_ID")
                                db_dump = await get_setting("dump_channel_id")
                                if db_dump and db_dump.get('value'):
                                    dump_id = db_dump['value']
                                if dump_id and sent_msg:
                                    try:
                                        dump_id_int = int(dump_id)
                                        original_text = media_msg.text or ""
                                        dump_caption = f"From User: `{user_id}`\nLink: {link}\n\n{original_text}".strip()
                                        await sent_msg.copy(dump_id_int, caption=dump_caption)
                                    except:
                                        pass
                            except Exception as e:
                                print(f"Error sending text message: {e}")
                        continue
                    
                    current_status = f"📥 Downloading file {idx + 1}/{files_to_download}..." if files_to_download > 1 else "📥 Downloading..."
                    try:
                        await status_msg.edit_text(current_status)
                    except:
                        pass
                    
                    path = None
                    sent_msg = None
                    file_size = 0
                    
                    if is_story:
                        # Story objects in Pyrogram/Hydrogram have direct media attributes
                        if media_msg.video: file_size = media_msg.video.file_size
                        elif media_msg.photo: file_size = media_msg.photo.file_size
                    else:
                        if media_msg.document: file_size = media_msg.document.file_size
                        elif media_msg.video: file_size = media_msg.video.file_size
                        elif media_msg.audio: file_size = media_msg.audio.file_size
                        elif media_msg.photo: file_size = media_msg.photo.file_size

                    # Force disk download for everything to save RAM on 1.5GB VPS
                    use_memory = False

                    if not is_group and user_client == client and isinstance(chat_id, (str, int)):
                        try:
                            # Direct copy is fastest for public links (channels)
                            sent = await client.copy_message(
                                chat_id=user_id,
                                from_chat_id=chat_id,
                                message_id=media_msg.id
                            )
                            if sent:
                                path = "COPIED"
                                sent_msg = sent
                                downloaded_count += 1
                        except Exception as e:
                            print(f"[DEBUG] copy_message failed: {e}, falling back to download")
                    
                    if not path:
                        if use_memory:
                            # Use default Pyrogram download for small files
                            path = await user_client.download_media(media_msg, in_memory=True)
                        else:
                            from bot.transfer import download_media_fast, upload_media_fast
                            # Get proper file extension from document or other media
                            ext = ""
                            if not is_story:
                                if media_msg.document:
                                    if media_msg.document.file_name:
                                        _, ext = os.path.splitext(media_msg.document.file_name)
                                elif media_msg.audio:
                                    if media_msg.audio.file_name:
                                        _, ext = os.path.splitext(media_msg.audio.file_name)
                                    if not ext:
                                        ext = ".mp3"
                                elif media_msg.photo:
                                    ext = ".jpg"
                                elif media_msg.voice:
                                    ext = ".ogg"
                                elif media_msg.video:
                                    if media_msg.video.file_name:
                                        _, ext = os.path.splitext(media_msg.video.file_name)
                                    else:
                                        ext = ".mp4"
                            
                            # Fallback to default if no extension found
                            file_suffix = f"_{media_msg.id}{ext}"
                            
                            # Optimized fast transfer for larger files
                            path = await asyncio.wait_for(
                                download_media_fast(
                                    user_client,
                                    media_msg,
                                    f"downloads/{user_id}{file_suffix}",
                                    progress_callback=progress_bar,
                                    progress_args=(status_msg, f"📥 Downloading {idx + 1}/{files_to_download}")
                                ),
                                timeout=1200
                            )
                    
                    if path and path != "COPIED":
                        caption = media_msg.caption if media_msg.caption else None
                        
                        try:
                            from bot.config import global_upload_semaphore
                            await global_upload_semaphore.acquire()
                            await status_msg.edit_text(f"📤 Uploading file {idx + 1}/{files_to_download}...")
                        except:
                            pass
                        
                        try:
                            if is_story:
                                if msg.photo:
                                    sent_msg = await client.send_photo(
                                        user_id,
                                        path,
                                        caption=caption
                                    )
                                elif msg.video:
                                    thumb_path = None
                                    try:
                                        if msg.video.thumbs:
                                            thumb_path = await user_client.download_media(msg.video.thumbs[0].file_id)
                                    except: pass
                                    
                                    sent_msg = await client.send_video(
                                        user_id,
                                        path,
                                        caption=caption,
                                        duration=msg.video.duration or 0,
                                        width=msg.video.width or 0,
                                        height=msg.video.height or 0,
                                        thumb=thumb_path,
                                        supports_streaming=True
                                    )
                                    if thumb_path and os.path.exists(thumb_path):
                                        try: os.remove(thumb_path)
                                        except: pass
                            elif media_msg.photo:
                                if use_memory:
                                    sent_msg = await client.send_photo(user_id, path, caption=caption)
                                else:
                                    # Photos can be uploaded via send_document for fast logic if needed, 
                                    # but standard photo upload is usually fast enough. 
                                    # For consistency, we'll keep standard for tiny files and use fast for big ones.
                                    sent_msg = await client.send_photo(
                                        user_id,
                                        path,
                                        caption=caption,
                                        progress=progress_bar,
                                        progress_args=(status_msg, f"📤 Uploading {idx + 1}/{files_to_download}")
                                    )
                            elif media_msg.audio:
                                if use_memory:
                                    sent_msg = await client.send_audio(user_id, path, caption=caption)
                                else:
                                    loop = asyncio.get_event_loop()
                                    sent_msg = await upload_media_fast(
                                        client, user_id, path, caption=caption, 
                                        progress_callback=lambda c, t: loop.create_task(progress_bar(c, t, status_msg, f"📤 Uploading {idx + 1}/{files_to_download}"))
                                    )
                            elif media_msg.video:
                                thumb_path = None
                                try:
                                    if media_msg.video.thumbs:
                                        thumb_path = await user_client.download_media(media_msg.video.thumbs[0].file_id)
                                except Exception as e:
                                    print(f"[DEBUG] Thumbnail download failed: {e}")
                                
                                # Use fast upload even for videos, passing video-specific metadata
                                loop = asyncio.get_event_loop()
                                sent_msg = await upload_media_fast(
                                    client, user_id, path, 
                                    caption=caption,
                                    duration=media_msg.video.duration or 0,
                                    width=media_msg.video.width or 0,
                                    height=media_msg.video.height or 0,
                                    thumb=thumb_path,
                                    supports_streaming=True,
                                    progress_callback=lambda c, t: loop.create_task(progress_bar(c, t, status_msg, f"📤 Uploading {idx + 1}/{files_to_download}"))
                                )
                                
                                if thumb_path and isinstance(thumb_path, str) and os.path.exists(thumb_path):
                                    try: os.remove(thumb_path)
                                    except: pass
                            else:
                                if use_memory:
                                    sent_msg = await client.send_document(user_id, path, caption=caption)
                                else:
                                    loop = asyncio.get_event_loop()
                                    sent_msg = await upload_media_fast(
                                        client, user_id, path, caption=caption,
                                        progress_callback=lambda c, t: loop.create_task(progress_bar(c, t, status_msg, f"📤 Uploading {idx + 1}/{files_to_download}"))
                                    )
                        finally:
                            from bot.config import global_upload_semaphore
                            global_upload_semaphore.release()
                        
                        downloaded_count += 1
                        
                        # Clean up file only if it's a file path, not BytesIO
                        if not use_memory and path and isinstance(path, str) and os.path.exists(path):
                            try:
                                os.remove(path)
                            except:
                                pass
                    
                    dump_id = os.environ.get("DUMP_CHANNEL_ID")
                    db_dump = await get_setting("dump_channel_id")
                    if db_dump and db_dump.get('value'):
                        dump_id = db_dump['value']
                    
                    if dump_id and sent_msg:
                        try:
                            dump_id_int = int(dump_id)
                            original_caption = media_msg.caption or ""
                            dump_caption = f"From User: `{user_id}`\nLink: {link}\n\n{original_caption}".strip()
                            await sent_msg.copy(dump_id_int, caption=dump_caption)
                        except Exception as e:
                            print(f"Dump failed: {e}")
                
                await increment_quota(user_id, downloaded_count)
                
                if quota_limited:
                    skipped = total_files - files_to_download
                    await status_msg.edit_text(
                        f"✅ Downloaded {downloaded_count}/{total_files} files.\n\n"
                        f"⚠️ {skipped} file(s) skipped due to daily limit.\n"
                        f"💎 Upgrade to Premium for unlimited downloads!",
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💎 Upgrade to Premium", callback_data="upgrade_prompt")]])
                    )
                else:
                    await status_msg.delete()
                
            except Exception as e:
                print(f"[DEBUG] Media processing failed: {str(e)}")
                try:
                    await status_msg.edit_text(f"❌ Error: {str(e)}")
                except:
                    pass
        else:
            await status_msg.edit_text("❌ Invalid link format. Could not extract chat ID or message ID.")
            
    except Exception as e:
        print(f"Global download handler error: {e}")
        try:
            if "Error:" not in status_msg.text:
                await status_msg.edit_text(f"❌ Error: {str(e)}")
        except:
            pass
    finally:
        active_downloads.discard(user_id)
        global_download_semaphore.release()
        if user_client and user_client != client:
            try:
                await user_client.stop()
            except:
                pass

@app.on_callback_query(filters.regex("upgrade_prompt"))
async def upgrade_prompt_callback(client, callback_query):
    await upgrade(client, callback_query.message)
    await callback_query.answer()

@app.on_message(filters.command("upgrade") & filters.private)
async def upgrade(client, message):
    from bot.config import (
        OWNER_USERNAME, SUPPORT_CHAT_LINK, PAYPAL_LINK, 
        UPI_ID, APPLE_PAY_ID, CRYPTO_ADDRESS, CARD_PAYMENT_LINK
    )
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
        f"🇮🇳 **UPI**:\n ╰`{UPI_ID}`\n"
        f"🪙 **Crypto(Binance)**:\n ╰`{CRYPTO_ADDRESS}`\n"
        f"💲 **PayPal**:\n ╰{PAYPAL_LINK}\n"
        f"🍎 **Apple Pay**:\n ╰{APPLE_PAY_ID}\n"
        f"💳 **Card**:\n ╰{CARD_PAYMENT_LINK}\n\n"
        f"🚀 After payment, send a screenshot to: @{OWNER_USERNAME}"
    )
    await message.reply(
        text,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Owner", url=f"https://t.me/{OWNER_USERNAME}")],
            [InlineKeyboardButton("Support Chat", url=SUPPORT_CHAT_LINK)]
        ])
    )
