import asyncio
import time
from pyrogram import filters, Client
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.errors import SessionPasswordNeeded, PhoneCodeInvalid, PasswordHashInvalid
from bot.config import app, login_states, API_ID, API_HASH
from bot.database import get_user, create_user, update_user_terms, save_session_string, logout_user, users_collection

@app.on_message(filters.command("start") & filters.private)
async def start(client, message):
    user_id = message.from_user.id

    from bot.handlers import verify_force_sub
    is_subbed, channel = await verify_force_sub(client, user_id)
    if not is_subbed:
        await message.reply(
            f"⛔ You must join our channel to use this bot.\n\n👉 {channel}",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Join Channel", url=f"https://t.me/{channel.replace('@', '')}")]
            ])
        )
        return

    user = await get_user(user_id)
    
    if not user:
        user = await create_user(user_id)
    
    # Show RichAds on start
    try:
        from bot.ads import show_ad
        await show_ad(client, user_id)
    except Exception as e:
        print(f"Error showing RichAds: {e}")
    
    if not user or not user.get('is_agreed_terms'):
        text = (
            "Welcome to the Downloader Bot!\n\n"
            "Before we proceed, please accept our Terms & Conditions:\n"
            "1. Do not download illegal content.\n"
            "2. We are not responsible for downloaded content.\n"
            "3. Use responsibly."
        )
        await message.reply(
            text,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ I Accept T&C", callback_data="accept_terms")]
            ])
        )
    else:
        await message.reply(f"Welcome back! Your role is: **{user.get('role', 'free')}**.\nUse /myinfo to check stats.")

@app.on_callback_query(filters.regex("accept_terms"))
async def accept_terms(client, callback_query):
    user_id = callback_query.from_user.id
    await update_user_terms(user_id, True)
    await callback_query.message.edit_text("Terms accepted! You can now use the bot.\n\nSend /login to connect your Telegram account or send a link to download.")

@app.on_message(filters.command("login") & filters.private)
async def login_start(client, message):
    user_id = message.from_user.id
    user = await get_user(user_id)
    
    if not user or not user.get('is_agreed_terms'):
        await message.reply("Please agree to the Terms & Conditions first using /start.")
        return

    if user.get('phone_session_string'):
        await message.reply("You are already logged in! Contact support if you need to re-login.")
        return

    login_states[user_id] = {"step": "PHONE", "timestamp": time.time()}
    await message.reply(
        "To download from restricted channels, you need to log in.\n\n"
        "Please send your **Phone Number** in international format (e.g., +1234567890).\n\n"
        "⏳ This session will expire in 5 minutes if no activity is detected."
    )

async def cleanup_expired_logins():
    while True:
        try:
            now = time.time()
            expired_users = [
                user_id for user_id, state in login_states.items()
                if now - state.get("timestamp", 0) > 300  # 5 minutes timeout
            ]
            for user_id in expired_users:
                state = login_states[user_id]
                if "client" in state:
                    try:
                        await state["client"].disconnect()
                    except:
                        pass
                del login_states[user_id]
                try:
                    await app.send_message(user_id, "⚠️ Login session expired due to inactivity.")
                except:
                    pass
        except Exception as e:
            print(f"Cleanup error: {e}")
        await asyncio.sleep(60)

@app.on_message(filters.command("help") & filters.private)
async def help_command(client, message):
    from bot.config import OWNER_USERNAME, SUPPORT_CHAT_LINK
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
    await message.reply(
        help_text,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("💬 Support Chat", url=SUPPORT_CHAT_LINK)],
            [InlineKeyboardButton("👤 Contact Owner", url=f"https://t.me/{OWNER_USERNAME}")]
        ])
    )

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
             await message.reply(
                 "📖 **Batch Download Example**\n\n"
                 "Usage: `/batch <start_link> <end_link>`\n\n"
                 "Example:\n"
                 "`/batch https://t.me/channel/10 https://t.me/channel/20`"
             )
             return
             
        start_link = parts[1]
        end_link = parts[2]
        
        import re
        start_link = start_link.split('?')[0]
        end_link = end_link.split('?')[0]
        start_match = re.search(r"t\.me/([^/]+)/(\d+)", start_link)
        if "t.me/c/" in start_link:
            start_match = re.search(r"t\.me/c/(\d+)/(\d+)", start_link)
        
        end_match = re.search(r"t\.me/([^/]+)/(\d+)", end_link)
        if "t.me/c/" in end_link:
            end_match = re.search(r"t\.me/c/(\d+)/(\d+)", end_link)
        
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
        
        # Import here to avoid circular import if needed
        from bot.handlers import download_handler
        
        processed_media_groups = set()
        
        for msg_id in range(start_id, end_id + 1):
            mock_message = message
            mock_message.text = f"https://t.me/{start_match.group(1)}/{msg_id}"
            if "t.me/c/" in start_link:
                 mock_message.text = f"https://t.me/c/{start_match.group(1)}/{msg_id}"
            
            # Fetch message to check for media group
            try:
                user = await get_user(user_id)
                session_str = user.get('phone_session_string')
                
                # Use user client if private link, else main client
                if "t.me/c/" in start_link and session_str:
                    temp_client = Client(
                        f"peek_{user_id}",
                        session_string=session_str,
                        in_memory=True,
                        api_id=API_ID,
                        api_hash=API_HASH
                    )
                    await temp_client.connect()
                    m = await temp_client.get_messages(chat_id, msg_id)
                    await temp_client.disconnect()
                else:
                    m = await client.get_messages(chat_id, msg_id)

                if m and m.media_group_id:
                    if m.media_group_id in processed_media_groups:
                        continue
                    processed_media_groups.add(m.media_group_id)
            except:
                pass

            await download_handler(client, mock_message)
            await asyncio.sleep(1)
            
    except Exception as e:
        await message.reply(f"❌ Batch error: {str(e)}")

@app.on_message(filters.private & filters.text & ~filters.command(["start", "login", "logout", "cancel", "cancel_login", "myinfo", "setrole", "download", "upgrade", "broadcast", "ban", "unban", "settings", "set_force_sub", "set_dump", "help", "batch", "stats", "killall"]) & ~filters.regex(r"https://t\.me/"))
async def handle_login_steps(client, message: Message):
    user_id = message.from_user.id
    if user_id not in login_states:
        return

    state = login_states[user_id]
    step = state["step"]

    try:
        if step == "PHONE":
            state["timestamp"] = time.time()
            phone_number = message.text.strip()
            temp_client = Client(f"session_{user_id}", api_id=API_ID, api_hash=API_HASH, in_memory=True)
            await temp_client.connect()
            
            try:
                sent_code = await temp_client.send_code(phone_number)
            except Exception as e:
                await message.reply(f"Error sending code: {str(e)}\nPlease try /login again.")
                await temp_client.disconnect()
                del login_states[user_id]
                return

            state["client"] = temp_client
            state["phone"] = phone_number
            state["phone_code_hash"] = sent_code.phone_code_hash
            state["step"] = "CODE"
            
            await message.reply("OTP Code sent to your Telegram account. Send it here (e.g. `1 2 3 4 5`).")

        elif step == "CODE":
            state["timestamp"] = time.time()
            code = message.text.replace("-", "").replace(" ", "").strip()
            temp_client = state["client"]
            
            try:
                await temp_client.sign_in(state["phone"], state["phone_code_hash"], code)
            except SessionPasswordNeeded:
                state["step"] = "PASSWORD"
                await message.reply("Two-Step Verification enabled. Send your **Cloud Password**.")
                return
            except PhoneCodeInvalid:
                await message.reply("Invalid code. Try again.")
                return
            except Exception as e:
                await message.reply(f"Login failed: {e}")
                await temp_client.disconnect()
                del login_states[user_id]
                return

            session_string = await temp_client.export_session_string()
            await save_session_string(user_id, session_string)
            await temp_client.disconnect()
            del login_states[user_id]
            await message.reply("✅ Login Successful!")

        elif step == "PASSWORD":
            state["timestamp"] = time.time()
            password = message.text.strip()
            temp_client = state["client"]
            
            try:
                await temp_client.check_password(password)
            except Exception as e:
                await message.reply(f"Login failed: {e}")
                await temp_client.disconnect()
                del login_states[user_id]
                return

            session_string = await temp_client.export_session_string()
            await save_session_string(user_id, session_string)
            await temp_client.disconnect()
            del login_states[user_id]
            await message.reply("✅ Login Successful!")

    except Exception as e:
        print(f"Error: {e}")
        await message.reply("Error. Login cancelled.")
        if "client" in state:
            await state["client"].disconnect()
        del login_states[user_id]

@app.on_message(filters.command("cancel") & filters.private)
async def cancel_downloads(client, message):
    user_id = message.from_user.id
    from bot.config import active_downloads, cancel_flags
    
    if user_id in active_downloads:
        cancel_flags.add(user_id)
        active_downloads.discard(user_id) # Force discard immediately as well
        await message.reply("🛑 Download cancellation request sent. Your process has been removed from active list.")
    else:
        # Just in case the flag was set but not in active_downloads
        if user_id in cancel_flags:
            cancel_flags.discard(user_id)
        await message.reply("No active downloads to cancel.")

@app.on_message(filters.command("cancel_login") & filters.private)
async def cancel_login(client, message):
    user_id = message.from_user.id
    if user_id in login_states:
        state = login_states[user_id]
        if "client" in state:
            try:
                await state["client"].disconnect()
            except:
                pass
        del login_states[user_id]
        await message.reply("✅ Login process cancelled.")
    else:
        await message.reply("No active login process to cancel.")

@app.on_message(filters.command("logout") & filters.private)
async def logout(client, message):
    user_id = message.from_user.id
    user = await get_user(user_id)
    
    # Clear any active login session
    if user_id in login_states:
        state = login_states[user_id]
        if "client" in state:
            try:
                await state["client"].disconnect()
            except:
                pass
        del login_states[user_id]

    if user and user.get('phone_session_string'):
        await logout_user(user_id)
        await message.reply("✅ Logged out successfully! Your session has been cleared.")
    else:
        await message.reply("You are not logged in.")
