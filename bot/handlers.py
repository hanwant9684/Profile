import asyncio
import os
import time
from pyrogram import filters, Client
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, Message
from bot.config import app, API_ID, API_HASH, active_downloads, global_download_semaphore
from bot.database import get_user, check_and_update_quota, increment_quota, get_setting, get_remaining_quota

async def progress_bar(current, total, message, type_msg):
    if total == 0:
        return
    percentage = current * 100 / total
    
    # Minimal progress bar (standard 10 blocks)
    finished_blocks = int(percentage / 10)
    remaining_blocks = 10 - finished_blocks
    bar = "✅" * finished_blocks + "⬜" * remaining_blocks
    
    if not hasattr(progress_bar, "last_update"):
        progress_bar.last_update = {}
    
    msg_id = message.id
    last_val = progress_bar.last_update.get(msg_id, 0)
    
    if abs(percentage - last_val) >= 10 or current == total:
        progress_bar.last_update[msg_id] = percentage
        try:
            await message.edit_text(
                f"**{type_msg}... {percentage:.1f}%**"
            )
        except:
            pass

async def verify_force_sub(client, user_id):
    from bot.config import OWNER_ID
    user = await get_user(user_id)
    if user and user.get('role') in ['owner', 'admin']:
        return True, None

    setting = await get_setting("force_sub_channel")
    if not setting or not setting.get('value'):
        return True, None
        
    channel = setting['value']
    try:
        from pyrogram.errors import UserNotParticipant
        member = await client.get_chat_member(channel, user_id)
        if member.status in ["left", "kicked"]:
             return False, channel
        return True, None
    except Exception as e:
        if "USER_NOT_PARTICIPANT" in str(e):
             return False, channel
        return True, None

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
    
    is_subbed, channel = await verify_force_sub(client, user_id)
    if not is_subbed:
        await message.reply(
            f"⛔ You must join our channel to use this bot.\n\n👉 {channel}",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Join Channel", url=f"https://t.me/{channel.replace('@', '')}")]
            ])
        )
        return

    allowed, msg = await check_and_update_quota(user_id)
    if not allowed:
        await message.reply(f"⛔ {msg}")
        return

    ad_setting = await get_setting("ad_config")
    user = await get_user(user_id)
    if user and user.get('role') == 'free' and ad_setting and ad_setting.get('json_value'):
         await message.reply("📢 [Ad] Join @RichAds for best crypto signals!")

    if user_id in active_downloads:
        await message.reply("⚠️ You already have a download in progress. Please wait.")
        return

    if global_download_semaphore.locked():
         await message.reply("⚠️ Server busy. Please try again in a few seconds.")
         return

    active_downloads.add(user_id)
    status_msg = await message.reply("🔍 Checking link...")
    
    user_client = None
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
            active_downloads.discard(user_id)
            global_download_semaphore.release()
            return

        # If it's a public link, we prefer using the main bot client (client)
        # even if the user is logged in, as it's more stable for public links.
        # User client is only strictly necessary for private links.
        if is_private:
            user_client = client
            session_str = user.get('phone_session_string') if user else None
            if session_str and len(session_str) > 10:
                 try:
                     user_client = Client(
                         f"user_{user_id}", 
                         session_string=session_str, 
                         in_memory=True, 
                         api_id=API_ID, 
                         api_hash=API_HASH
                     )
                     await user_client.connect()
                 except Exception as e:
                     print(f"User client connection error: {e}")
                     user_client = client
        else:
            user_client = client
        
        await status_msg.edit_text("📥 Checking media...")
        
        if chat_id and message_id:
            try:
                msg = await user_client.get_messages(chat_id, message_id)
                if not msg:
                    print(f"[DEBUG] get_messages returned None for chat_id={chat_id}, message_id={message_id}")
                    await status_msg.edit_text("❌ Could not find message. Link might be invalid or expired.")
                    active_downloads.discard(user_id)
                    global_download_semaphore.release()
                    return
                
                messages_to_process = [msg]
                is_media_group = False
                
                if msg.media_group_id:
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
                    await status_msg.edit_text("⛔ Daily limit reached (5/5). Upgrade to Premium for unlimited downloads using /upgrade")
                    active_downloads.discard(user_id)
                    global_download_semaphore.release()
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
                    
                    if user_client == client and isinstance(chat_id, str):
                        try:
                            sent = await user_client.copy_message(
                                chat_id=user_id,
                                from_chat_id=chat_id,
                                message_id=media_msg.id
                            )
                            path = "COPIED"
                            sent_msg = sent
                            downloaded_count += 1
                        except Exception as e:
                            print(f"[DEBUG] copy_message failed for msg {media_msg.id}: {e}, falling back to download")
                            path = await asyncio.wait_for(
                                user_client.download_media(
                                    media_msg, 
                                    progress=progress_bar, 
                                    progress_args=(status_msg, f"📥 Downloading {idx + 1}/{files_to_download}")
                                ), 
                                timeout=600
                            )
                    else:
                        path = await asyncio.wait_for(
                            user_client.download_media(
                                media_msg, 
                                progress=progress_bar, 
                                progress_args=(status_msg, f"📥 Downloading {idx + 1}/{files_to_download}")
                            ), 
                            timeout=600
                        )
                    
                    if path and path != "COPIED":
                        caption = media_msg.caption if media_msg.caption else None
                        
                        try:
                            await status_msg.edit_text(f"📤 Uploading file {idx + 1}/{files_to_download}...")
                        except:
                            pass
                        
                        if media_msg.photo:
                            sent_msg = await client.send_photo(
                                user_id,
                                path,
                                caption=caption,
                                progress=progress_bar,
                                progress_args=(status_msg, f"📤 Uploading {idx + 1}/{files_to_download}")
                            )
                        elif media_msg.audio:
                            sent_msg = await client.send_audio(
                                user_id,
                                path,
                                caption=caption,
                                progress=progress_bar,
                                progress_args=(status_msg, f"📤 Uploading {idx + 1}/{files_to_download}")
                            )
                        elif media_msg.video:
                            thumb_path = None
                            try:
                                if media_msg.video.thumbs:
                                    thumb_path = await user_client.download_media(media_msg.video.thumbs[0].file_id)
                            except Exception as e:
                                print(f"[DEBUG] Thumbnail download failed: {e}")
                            
                            sent_msg = await client.send_video(
                                user_id,
                                path,
                                caption=caption,
                                duration=media_msg.video.duration or 0,
                                width=media_msg.video.width or 0,
                                height=media_msg.video.height or 0,
                                thumb=thumb_path,
                                supports_streaming=True,
                                progress=progress_bar,
                                progress_args=(status_msg, f"📤 Uploading {idx + 1}/{files_to_download}")
                            )
                            
                            if thumb_path and os.path.exists(thumb_path):
                                try:
                                    os.remove(thumb_path)
                                except:
                                    pass
                        else:
                            sent_msg = await client.send_document(
                                user_id, 
                                path, 
                                caption=caption,
                                progress=progress_bar,
                                progress_args=(status_msg, f"📤 Uploading {idx + 1}/{files_to_download}")
                            )
                        
                        downloaded_count += 1
                        
                        if os.path.exists(path):
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
                        f"💎 Upgrade to Premium for unlimited downloads! Use /upgrade"
                    )
                else:
                    await status_msg.delete()
                
                path = "PROCESSED"
                
            except Exception as e:
                print(f"[DEBUG] Direct extraction failed: {str(e)}")
                print(f"Direct extraction failed, trying fallback: {e}")
                path = await asyncio.wait_for(
                    user_client.download_media(
                        link, 
                        progress=progress_bar, 
                        progress_args=(status_msg, "📥 Downloading")
                    ), 
                    timeout=600
                )
                print(f"[DEBUG] Fallback download result path: {path}")
        else:
            print(f"[DEBUG] No specific chat_id/message_id parsed from link: {link}")
            path = await asyncio.wait_for(
                user_client.download_media(
                    link, 
                    progress=progress_bar, 
                    progress_args=(status_msg, "📥 Downloading")
                ), 
                timeout=600
            )
            print(f"[DEBUG] General download result path: {path}")
        
        if path == "PROCESSED":
            pass
        elif not path and not status_msg.text.startswith("❌"):
             raise Exception("Download failed or empty.")
        elif path and path not in ["COPIED", "PROCESSED"]:
            caption = msg.caption if (msg and msg.caption) else None
            
            if msg.photo:
                sent_msg = await client.send_photo(
                    user_id,
                    path,
                    caption=caption,
                    progress=progress_bar,
                    progress_args=(status_msg, "📤 Uploading")
                )
            elif msg.audio:
                sent_msg = await client.send_audio(
                    user_id,
                    path,
                    caption=caption,
                    progress=progress_bar,
                    progress_args=(status_msg, "📤 Uploading")
                )
            elif msg.video:
                 thumb_path = None
                 try:
                     if msg.video.thumbs:
                         thumb_path = await user_client.download_media(msg.video.thumbs[0].file_id)
                 except Exception as e:
                     print(f"[DEBUG] Thumbnail download failed: {e}")
                 
                 sent_msg = await client.send_video(
                    user_id,
                    path,
                    caption=caption,
                    duration=msg.video.duration or 0,
                    width=msg.video.width or 0,
                    height=msg.video.height or 0,
                    thumb=thumb_path,
                    supports_streaming=True,
                    progress=progress_bar,
                    progress_args=(status_msg, "📤 Uploading")
                )
                 
                 if thumb_path and os.path.exists(thumb_path):
                     try:
                         os.remove(thumb_path)
                     except:
                         pass
            else:
                sent_msg = await client.send_document(
                    user_id, 
                    path, 
                    caption=caption,
                    progress=progress_bar,
                    progress_args=(status_msg, "📤 Uploading")
                )
        
            dump_id = os.environ.get("DUMP_CHANNEL_ID")
            db_dump = await get_setting("dump_channel_id")
            if db_dump and db_dump.get('value'):
                 dump_id = db_dump['value']

            if dump_id:
                try:
                    dump_id_int = int(dump_id)
                    original_caption = msg.caption if msg else ""
                    original_caption = original_caption or ""
                    dump_caption = f"From User: `{user_id}`\nLink: {link}\n\n{original_caption}".strip()
                    await sent_msg.copy(dump_id_int, caption=dump_caption)
                except Exception as e:
                    print(f"Dump failed: {e}")

            await increment_quota(user_id)
            await status_msg.delete()
        
    except asyncio.TimeoutError:
        await status_msg.edit_text("❌ Download timed out (limit: 10 mins).")
    except Exception as e:
        await status_msg.edit_text(f"❌ Error: {str(e)}")
    finally:
        global_download_semaphore.release()
        active_downloads.discard(user_id)
        
        if path and os.path.exists(path):
            try:
                os.remove(path)
            except:
                pass
                
        if user_client and user_client != client:
            try:
                await user_client.disconnect()
            except:
                pass

@app.on_message(filters.command("upgrade") & filters.private)
async def upgrade(client, message):
    text = (
        "💎 **Premium Benefits**\n"
        "• Unlimited Downloads\n"
        "• Priority Support\n"
        "• No Ads\n\n"
        "💰 **Pricing**\n"
        "• 1 Month: $5\n"
        "• Lifetime: $25\n\n"
        "To upgrade, please contact the owner: @OwnerUsername\n"
        "(Payment methods: PayPal, Crypto, UPI)"
    )
    await message.reply(text)
