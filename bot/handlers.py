import asyncio
import os
import time
import io
import aiofiles
import re
from pyrogram import filters, Client
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, Message
from bot.config import app, API_ID, API_HASH, active_downloads, global_download_semaphore, MEMORY_BUFFER_LIMIT
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
        setattr(progress_bar, "last_update", {})
    
    msg_id = message.id
    last_val = getattr(progress_bar, "last_update").get(msg_id, 0)
    
    if abs(percentage - last_val) >= 10 or current == total:
        getattr(progress_bar, "last_update")[msg_id] = percentage
        try:
            await message.edit_text(
                f"**{type_msg}... {percentage:.1f}%**"
            )
        except:
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

import bot.info
from bot.ads import show_ad

@app.on_message(filters.command("start") & filters.private)
async def start_command(client, message):
    user_id = message.from_user.id
    user = await get_user(user_id)
    
    # Store user info in DB
    user_info = {
        "first_name": message.from_user.first_name,
        "last_name": message.from_user.last_name,
        "username": message.from_user.username
    }
    
    if not user:
        await create_user(user_id)
        # We need to update with name info since create_user only sets defaults
        from bot.database import users_collection
        await users_collection.update_one(
            {"telegram_id": str(user_id)},
            {"$set": user_info}
        )
        user = await get_user(user_id)
    else:
        # Update existing user info
        from bot.database import users_collection
        await users_collection.update_one(
            {"telegram_id": str(user_id)},
            {"$set": user_info}
        )
        
    if not user.get("is_agreed_terms"):
        # Show ad before terms for new user
        await show_ad(client, user_id)
        
        terms_text = (
            "👋 **Welcome to the Bot!**\n\n"
            "By using this bot, you agree to our Terms and Conditions.\n\n"
            "✅ You will not use this bot for illegal activities.\n"
            "✅ You respect copyright and ownership.\n\n"
            "Click the button below to agree and start using the bot."
        )
        await message.reply(
            terms_text,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("I Agree ✅", callback_data="agree_terms")]
            ])
        )
    else:
        # Show ad for existing user
        await show_ad(client, user_id)
        await bot.info.myinfo(client, message)

@app.on_callback_query(filters.regex("agree_terms"))
async def agree_terms_callback(client, callback_query):
    user_id = callback_query.from_user.id
    from bot.database import update_user_terms
    await update_user_terms(user_id, True)
    await callback_query.answer("Terms accepted! 🎉")
    await callback_query.message.edit_text("✅ You have agreed to the terms. You can now use the bot!")
    await bot.info.myinfo(client, callback_query.message)

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

    allowed, quota_msg = await check_and_update_quota(user_id)
    if not allowed:
        await message.reply(
            f"⛔ {quota_msg}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💎 Upgrade to Premium", callback_data="upgrade_prompt")]])
        )
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
    
    user_client = client
    path = None
    msg = None
    
    await global_download_semaphore.acquire()
    
    try:
        link = message.text.strip()
        
        # Improved parsing for links with ?single or other queries
        link_no_query = link.split('?')[0]

        public_match = re.search(r"t\.me/([^/]+)/(\d+)", link_no_query)
        private_match = re.search(r"t\.me/c/(\d+)/(\d+)", link_no_query)
        
        is_private = False
        chat_id = None
        message_id = None

        if private_match:
            chat_id = int("-100" + private_match.group(1))
            message_id = int(private_match.group(2))
            is_private = True
        elif public_match:
            chat_id = public_match.group(1)
            if chat_id.isdigit():
                 chat_id = int("-100" + chat_id)
            message_id = int(public_match.group(2))

        if is_private and (not user or not user.get('phone_session_string')):
            await status_msg.edit_text("❌ Login is mandatory for private channel links. Use /login to connect your account.")
            return

        if is_private:
            session_str = user.get('phone_session_string')
            try:
                user_client = Client(f"user_{user_id}", session_string=session_str, in_memory=True, api_id=API_ID, api_hash=API_HASH)
                await user_client.connect()
            except Exception as e:
                await status_msg.edit_text(f"❌ User session error: {e}")
                return

        await status_msg.edit_text("📥 Checking media...")
        
        if chat_id and message_id:
            try:
                fetch_client = user_client if is_private else app
                msg = await asyncio.wait_for(fetch_client.get_messages(chat_id, message_id), timeout=30)
                
                if not msg or msg.empty:
                    if not is_private and user and user.get('phone_session_string'):
                        try:
                            # Use the persistent user_client if available or create one
                            if not user_client or user_client == client:
                                session_str = user.get('phone_session_string')
                                user_client = Client(f"user_{user_id}", session_string=session_str, in_memory=True, api_id=API_ID, api_hash=API_HASH)
                                await user_client.connect()
                            msg = await asyncio.wait_for(user_client.get_messages(chat_id, message_id), timeout=30)
                        except Exception as e:
                            pass

                if not msg or msg.empty:
                    await status_msg.edit_text("❌ Message restricted or not found. Download message by Login, Use ⚡ /login . And Follow bot Instructions")
                    return
                
                messages_to_process = [msg]
                if msg.media_group_id:
                    try:
                        # Use the client that successfully fetched the first message
                        active_fetcher = user_client if (user_client and user_client != client) else app
                        messages_to_process = await asyncio.wait_for(active_fetcher.get_media_group(chat_id, message_id), timeout=30)
                    except:
                        pass
                
                remaining_quota, is_unlimited = await get_remaining_quota(user_id)
                files_to_download = min(len(messages_to_process), remaining_quota) if not is_unlimited else len(messages_to_process)
                
                if files_to_download == 0:
                    await status_msg.edit_text("⛔ Daily limit reached.")
                    return
                
                downloaded_count = 0
                for idx, media_msg in enumerate(messages_to_process[:files_to_download]):
                    from bot.config import cancel_flags
                    if user_id in cancel_flags:
                        await status_msg.edit_text("❌ Cancelled.")
                        cancel_flags.discard(user_id)
                        return

                    if not media_msg.media:
                        if media_msg.text:
                            sent = await app.send_message(user_id, media_msg.text, entities=media_msg.entities)
                            if sent: downloaded_count += 1
                        continue
                    
                    await status_msg.edit_text(f"📥 Processing {idx+1}/{files_to_download}...")
                    
                    # Try direct copy for all links first (it's fastest and bypasses local download)
                    path = None
                    try:
                        # Try with main bot first
                        sent = await app.copy_message(user_id, chat_id, media_msg.id)
                        if sent:
                            path = "COPIED"
                            downloaded_count += 1
                    except Exception as e:
                        # Fallback: try copy with user client if available
                        if user_client and user_client != client:
                            try:
                                # User client copy works if bot can't see the message but user can
                                sent = await user_client.copy_message(user_id, chat_id, media_msg.id)
                                if sent:
                                    path = "COPIED"
                                    downloaded_count += 1
                            except:
                                pass
                    
                    if not path:
                        # Full download fallback
                        file_size = 0
                        if media_msg.document: file_size = media_msg.document.file_size
                        elif media_msg.video: file_size = media_msg.video.file_size
                        elif media_msg.audio: file_size = media_msg.audio.file_size
                        elif media_msg.photo: file_size = media_msg.photo.file_size

                        use_memory = file_size > 0 and file_size <= MEMORY_BUFFER_LIMIT
                        
                        # Use whichever client can see the media
                        downloader = user_client if (user_client and user_client != client) else app
                        path = await downloader.download_media(media_msg, in_memory=use_memory, progress=progress_bar, progress_args=(status_msg, "📥 Downloading"))
                        
                        if path:
                            await status_msg.edit_text(f"📤 Uploading {idx+1}/{files_to_download}...")
                            caption = media_msg.caption
                            sent_msg = None
                            if media_msg.photo: sent_msg = await app.send_photo(user_id, path, caption=caption)
                            elif media_msg.audio: sent_msg = await app.send_audio(user_id, path, caption=caption)
                            elif media_msg.video:
                                thumb = await downloader.download_media(media_msg.video.thumbs[0].file_id) if (media_msg.video and media_msg.video.thumbs) else None
                                sent_msg = await app.send_video(user_id, path, caption=caption, thumb=thumb, supports_streaming=True)
                                if thumb and os.path.exists(thumb): os.remove(thumb)
                            else: sent_msg = await app.send_document(user_id, path, caption=caption)
                            
                            if sent_msg: downloaded_count += 1
                            if not use_memory and os.path.exists(path): os.remove(path)

                await increment_quota(user_id, downloaded_count)
                await status_msg.delete()
                
                # Show ad after successful download
                from bot.ads import show_ad
                await show_ad(client, user_id)
                
                path = "PROCESSED"

            except Exception as e:
                await status_msg.edit_text(f"❌ Error: {e}")
        else:
            await status_msg.edit_text("❌ Could not parse link.")

    except Exception as e:
        pass
    finally:
        active_downloads.discard(user_id)
        global_download_semaphore.release()
        if is_private and user_client and user_client != client:
            try: await user_client.disconnect()
            except: pass

@app.on_callback_query(filters.regex("upgrade_prompt"))
async def upgrade_prompt_callback(client, callback_query):
    await upgrade(client, callback_query.message)
    await callback_query.answer()

@app.on_message(filters.command("upgrade") & filters.private)
async def upgrade(client, message):
    from bot.config import OWNER_USERNAME, SUPPORT_CHAT_LINK, PAYPAL_LINK, UPI_ID, APPLE_PAY_ID, CRYPTO_ADDRESS, CARD_PAYMENT_LINK
    text = (
        "💎 **Premium Plans**\n\n"
        "⚡ **Standard**\n"
        "• Unlimited Downloads\n"
        "• Batch Download upto (50) Files\n"
        "• Fast Speed\n\n"
        "💳 **Payment Details**\n"
        f"• **PayPal**: {PAYPAL_LINK}\n"
        f"• **UPI**: `{UPI_ID}`\n"
        f"🚀 Contact: @{OWNER_USERNAME}"
    )
    await message.reply(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💬 Support Chat", url=SUPPORT_CHAT_LINK)]]))
