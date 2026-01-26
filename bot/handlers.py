import asyncio
import os
import time
import io
import aiofiles
from pyrogram import filters, Client, enums
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, Message
from bot.config import app, API_ID, API_HASH, active_downloads, global_download_semaphore, MEMORY_BUFFER_LIMIT
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
            await asyncio.sleep(1) # Small delay to avoid flood
            
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

    allowed, msg = await check_and_update_quota(user_id)
    if not allowed:
        await message.reply(
            f"⛔ {msg}",
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

    if global_download_semaphore.locked():
         await message.reply("⚠️ Server busy. Please try again in a few seconds.")
         return

    active_downloads.add(user_id)
    status_msg = await message.reply("🔍 Checking link...")
    
    user_client_to_use = None
    path = None
    
    await global_download_semaphore.acquire()
    
    try:
        link = message.text.strip()
        
        import re
        chat_id = None
        message_id = None
        
        public_match = re.search(r"t\.me/([^/]+)/(\d+)", link)
        private_match = re.search(r"t\.me/c/(\d+)/(\d+)", link)
        
        is_private = False
        if private_match:
            chat_id = int("-100" + private_match.group(1))
            message_id = int(private_match.group(2))
            is_private = True
        elif public_match:
            chat_id = public_match.group(1)
            message_id = int(public_match.group(2))

        user = await get_user(user_id)
        
        if is_private and (not user or not user.get('phone_session_string') or len(user.get('phone_session_string', '')) < 10):
            await status_msg.edit_text("❌ Login is mandatory for private channel links. Use /login to connect your account.")
            return

        # Use the bot client for public links, but we'll need the user_client 
        # specifically if the chat is a private group/channel.
        user_client_to_use = client
        if is_private:
            session_str = user.get('phone_session_string') if user else None
            if session_str and len(session_str) > 10:
                 try:
                     user_client_to_use = Client(
                         f"user_{user_id}", 
                         session_string=session_str, 
                         in_memory=True, 
                         api_id=API_ID, 
                         api_hash=API_HASH
                     )
                     await user_client_to_use.connect()
                 except Exception as e:
                     print(f"User client connection error: {e}")
                     user_client_to_use = client
        
        await status_msg.edit_text("📥 Checking chat type...")
        
        # Determine chat type (Group vs Channel)
        is_group = False
        try:
            chat_info = await user_client_to_use.get_chat(chat_id)
            if chat_info.type in [enums.ChatType.GROUP, enums.ChatType.SUPERGROUP]:
                is_group = True
                print(f"[DEBUG] Chat {chat_id} is a GROUP. Forcing download/upload strategy.")
            else:
                print(f"[DEBUG] Chat {chat_id} is a CHANNEL. Using copy if possible.")
        except Exception as e:
            print(f"[DEBUG] get_chat failed: {e}. Defaulting to standard logic.")

        await status_msg.edit_text("📥 Checking media...")
        
        if chat_id and message_id:
            try:
                msg = await user_client_to_use.get_messages(chat_id, message_id)
                if not msg:
                    await status_msg.edit_text("❌ Could not find message. Link might be invalid or expired.")
                    return
                
                messages_to_process = [msg]
                is_media_group = False
                
                if msg.media_group_id:
                    is_media_group = True
                    try:
                        media_group = await user_client_to_use.get_media_group(chat_id, message_id)
                        messages_to_process = media_group
                    except Exception as e:
                        print(f"[DEBUG] get_media_group failed: {e}, processing single message")
                        messages_to_process = [msg]
                
                remaining_quota, is_unlimited = await get_remaining_quota(user_id)
                total_files = len(messages_to_process)
                files_to_download = min(total_files, remaining_quota) if not is_unlimited else total_files
                quota_limited = files_to_download < total_files and not is_unlimited
                
                if files_to_download == 0:
                    await status_msg.edit_text(
                        "⛔ Daily limit reached. Upgrade to Premium for unlimited downloads.",
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💎 Upgrade to Premium", callback_data="upgrade_prompt")]])
                    )
                    return
                
                if is_media_group and total_files > 1:
                    await status_msg.edit_text(f"📥 Found {total_files} files in media group. Downloading {files_to_download}...")
                else:
                    await status_msg.edit_text("📥 Downloading...")
                
                downloaded_count = 0
                for idx, media_msg in enumerate(messages_to_process[:files_to_download]):
                    from bot.config import cancel_flags
                    if user_id in cancel_flags:
                        await status_msg.edit_text("❌ Download cancelled by user.")
                        cancel_flags.discard(user_id)
                        return

                    if not media_msg.media:
                        if media_msg.text:
                            try:
                                await client.send_message(user_id, media_msg.text, entities=media_msg.entities)
                                downloaded_count += 1
                            except Exception as e:
                                print(f"Error sending text message: {e}")
                        continue
                    
                    current_status = f"📥 Downloading file {idx + 1}/{files_to_download}..." if files_to_download > 1 else "📥 Downloading..."
                    try:
                        await status_msg.edit_text(current_status)
                    except: pass
                    
                    path = None
                    sent_msg = None
                    file_size = 0
                    
                    if media_msg.document: file_size = media_msg.document.file_size
                    elif media_msg.video: file_size = media_msg.video.file_size
                    elif media_msg.audio: file_size = media_msg.audio.file_size
                    elif media_msg.photo: file_size = media_msg.photo.file_size

                    use_memory = file_size > 0 and file_size <= MEMORY_BUFFER_LIMIT

                    # Apply strategy: ONLY use copy for CHANNELS. 
                    # For GROUPS and SUPERGROUPS, we MUST use download/upload.
                    if not is_group and user_client_to_use == client:
                        try:
                            print(f"[DEBUG] Attempting COPY method for CHANNEL message {media_msg.id}")
                            sent = await client.copy_message(chat_id=user_id, from_chat_id=chat_id, message_id=media_msg.id)
                            if sent:
                                path = "COPIED"
                                sent_msg = sent
                                downloaded_count += 1
                                print(f"[DEBUG] COPY method SUCCESSFUL for message {media_msg.id}")
                        except Exception as e:
                            print(f"[DEBUG] copy_message failed: {e}, falling back to download")
                    
                    if not path:
                        print(f"[DEBUG] Using DOWNLOAD/UPLOAD strategy for message {media_msg.id} (Chat ID: {chat_id}, Is Group: {is_group})")
                        from bot.transfer import download_media_fast
                        if use_memory:
                            path = await user_client_to_use.download_media(media_msg, in_memory=True)
                        else:
                            path = await asyncio.wait_for(
                                download_media_fast(
                                    user_client_to_use,
                                    media_msg,
                                    f"downloads/{user_id}_{media_msg.id}",
                                    progress_callback=progress_bar,
                                    progress_args=(status_msg, f"📥 Downloading {idx + 1}/{files_to_download}")
                                ),
                                timeout=600
                            )
                    
                    if path and path != "COPIED":
                        caption = media_msg.caption if media_msg.caption else None
                        try:
                            await status_msg.edit_text(f"📤 Uploading file {idx + 1}/{files_to_download}...")
                        except: pass
                        
                        if media_msg.photo:
                            sent_msg = await client.send_photo(user_id, path, caption=caption)
                        elif media_msg.audio:
                            sent_msg = await client.send_audio(user_id, path, caption=caption)
                        elif media_msg.video:
                            thumb_path = None
                            try:
                                if media_msg.video.thumbs:
                                    thumb_path = await user_client_to_use.download_media(media_msg.video.thumbs[0].file_id)
                            except: pass
                            
                            sent_msg = await client.send_video(
                                user_id, path, caption=caption, 
                                duration=media_msg.video.duration or 0,
                                width=media_msg.video.width or 0,
                                height=media_msg.video.height or 0,
                                thumb=thumb_path, supports_streaming=True
                            )
                            if thumb_path and os.path.exists(thumb_path):
                                try: os.remove(thumb_path)
                                except: pass
                        else:
                            sent_msg = await client.send_document(user_id, path, caption=caption)
                        
                        downloaded_count += 1
                        if not use_memory and path and os.path.exists(path):
                            try: os.remove(path)
                            except: pass
                    
                    # Dumping
                    dump_id = os.environ.get("DUMP_CHANNEL_ID")
                    db_dump = await get_setting("dump_channel_id")
                    if db_dump and db_dump.get('value'): dump_id = db_dump['value']
                    
                    if dump_id and sent_msg:
                        try:
                            dump_id_int = int(dump_id)
                            original_caption = media_msg.caption or ""
                            dump_caption = f"From User: `{user_id}`\nLink: {link}\n\n{original_caption}".strip()
                            await sent_msg.copy(dump_id_int, caption=dump_caption)
                        except: pass
                
                await increment_quota(user_id, downloaded_count)
                if quota_limited:
                    skipped = total_files - files_to_download
                    await status_msg.edit_text(
                        f"✅ Downloaded {downloaded_count}/{total_files} files.\n"
                        f"⚠️ {skipped} file(s) skipped due to daily limit.\n"
                        f"💎 Upgrade to Premium for unlimited downloads!",
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💎 Upgrade to Premium", callback_data="upgrade_prompt")]])
                    )
                else:
                    await status_msg.delete()
                
            except Exception as e:
                print(f"[DEBUG] Download error: {str(e)}")
                await status_msg.edit_text(f"❌ Error: {str(e)}")
        else:
            await status_msg.edit_text("❌ Could not parse link.")

    except Exception as e:
        print(f"Handler error: {e}")
        try:
            await status_msg.edit_text(f"❌ Error: {str(e)}")
        except: pass
    finally:
        active_downloads.discard(user_id)
        global_download_semaphore.release()
        if user_client_to_use and user_client_to_use != client:
            try:
                await user_client_to_use.stop()
            except: pass

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
        "•——————————————————————•\n"
        "🔸 **7** days - **$1**\n"
        "🔸 **15** days - **$1.5**\n"
        "🔸 **30** days - **$2**\n"
        "•——————————————————————•\n"
        "• Unlimited Downloads\n"
        "• Batch Download upto (50)\n"
        "• Fast Speed\n\n"
        "🔥 **Lifetime** - $25\n"
        "• All Premium Features\n"
        "• Priority Support\n\n"
        "💳 **Payment Details**\n"
        f"• **PayPal**:\n ╰{PAYPAL_LINK}\n"
        f"• **UPI**:\n ╰`{UPI_ID}`\n"
        f"• **Apple Pay**:\n ╰{APPLE_PAY_ID}\n"
        f"• **Crypto**:\n ╰`{CRYPTO_ADDRESS}`\n"
        f"• **Card**:\n ╰{CARD_PAYMENT_LINK}\n\n"
        f"🚀 After payment, send a screenshot to: @{OWNER_USERNAME}"
    )
    await message.reply(
        text,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("💬 Support Chat", url=SUPPORT_CHAT_LINK)],
            [InlineKeyboardButton("👤 Contact Owner", url=f"https://t.me/{OWNER_USERNAME}")]
        ])
    )
