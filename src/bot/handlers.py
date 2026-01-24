import asyncio
import os
import time
from hydrogram import filters, Client
from hydrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, Message
from src.bot.config import app, API_ID, API_HASH, active_downloads, global_download_semaphore
from src.bot.database import get_user, check_and_update_quota, increment_quota, get_setting

async def progress_bar(current, total, message, type_msg):
    if total == 0:
        return
    percentage = current * 100 / total
    finished_blocks = int(percentage / 10)
    remaining_blocks = 10 - finished_blocks
    bar = "✅" * finished_blocks + "⬜" * remaining_blocks
    
    if not hasattr(progress_bar, "last_update"):
        progress_bar.last_update = {}
    
    msg_id = message.id
    last_val = progress_bar.last_update.get(msg_id, 0)
    
    if abs(percentage - last_val) >= 5 or current == total:
        progress_bar.last_update[msg_id] = percentage
        try:
            await message.edit_text(
                f"**{type_msg}...**\n\n"
                f"|{bar}| {percentage:.1f}%\n"
                f"📦 {current / (1024*1024):.1f}MB / {total / (1024*1024):.1f}MB"
            )
        except:
            pass

async def verify_force_sub(client, user_id):
    setting = await get_setting("force_sub_channel")
    if not setting or not setting.get('value'):
        return True, None
        
    channel = setting['value']
    try:
        from hydrogram.errors import UserNotParticipant
        member = await client.get_chat_member(channel, user_id)
        if member.status in ["left", "kicked"]:
             return False, channel
        return True, None
    except Exception as e:
        if "USER_NOT_PARTICIPANT" in str(e):
             return False, channel
        return True, None

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
        
        await status_msg.edit_text("📥 Downloading...")
        
        if chat_id and message_id:
            try:
                msg = await user_client.get_messages(chat_id, message_id)
                if not msg:
                    print(f"[DEBUG] get_messages returned None for chat_id={chat_id}, message_id={message_id}")
                    await status_msg.edit_text("❌ Could not find message. Link might be invalid or expired.")
                elif msg.media:
                    print(f"[DEBUG] Found media type: {type(msg.media)}")
                    if user_client == client and isinstance(chat_id, str):
                        try:
                            sent = await user_client.copy_message(
                                chat_id=user_id,
                                from_chat_id=chat_id,
                                message_id=message_id,
                                caption=f"Original Link: {link}"
                            )
                            path = "COPIED"
                            sent_msg = sent
                        except Exception as e:
                            print(f"[DEBUG] copy_message failed: {e}, falling back to download")
                            path = await asyncio.wait_for(
                                user_client.download_media(
                                    msg, 
                                    progress=progress_bar, 
                                    progress_args=(status_msg, "📥 Downloading")
                                ), 
                                timeout=600
                            )
                    else:
                        path = await asyncio.wait_for(
                            user_client.download_media(
                                msg, 
                                progress=progress_bar, 
                                progress_args=(status_msg, "📥 Downloading")
                            ), 
                            timeout=600
                        )
                    print(f"[DEBUG] Download result path: {path}")
                else:
                    print(f"[DEBUG] Message found but has no media. Content: {msg.text[:50] if msg.text else 'No text'}")
                    path = None
                    await status_msg.edit_text("❌ No media found in this link.")
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
        
        if not path and not status_msg.text.startswith("❌"):
             raise Exception("Download failed or empty.")

        if path != "COPIED":
            await status_msg.edit_text("📤 Uploading...")
            sent_msg = await client.send_document(
                user_id, 
                path, 
                caption=f"Original Link: {link}",
                progress=progress_bar,
                progress_args=(status_msg, "📤 Uploading")
            )
        
        dump_id = os.environ.get("DUMP_CHANNEL_ID")
        db_dump = await get_setting("dump_channel_id")
        if db_dump and db_dump.get('value'):
             dump_id = db_dump['value']

        if dump_id:
            try:
                await sent_msg.copy(dump_id, caption=f"From User: `{user_id}`\nLink: {link}")
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
