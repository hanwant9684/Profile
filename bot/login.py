import asyncio
import time
from pyrogram import filters
from pyrogram.client import Client
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.errors import SessionPasswordNeeded, PhoneCodeInvalid, PasswordHashInvalid
from bot.config import app, login_states, API_ID, API_HASH
from bot.database import get_user, create_user, update_user_terms, save_session_string, logout_user

from bot.logger import logger

@app.on_message(filters.command("start") & filters.private)
async def start(client, message):
    user_id = message.from_user.id

    from bot.handlers import verify_force_sub
    is_subbed, channel = await verify_force_sub(client, user_id)
    if not is_subbed:
        channel_url = channel.replace('@', '') if channel else ''
        await message.reply(
            f"⛔ You must join our channel to use this bot.\n\n👉 {channel}",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Join Channel", url=f"https://t.me/{channel_url}")]
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
        logger.error(f"Error showing RichAds: {e}")
    
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
    
    # Show RichAds after accepting terms
    try:
        from bot.ads import show_ad
        await show_ad(client, user_id)
    except Exception as e:
        logger.error(f"Error showing RichAds on T&C accept: {e}")
        
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
                        # Ensure we stop the client properly to release threads
                        await state["client"].stop()
                    except:
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
            logger.error(f"Cleanup error: {e}")
        await asyncio.sleep(60)

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
            try:
                state["client"] = Client(
                    f"session_{user_id}",
                    api_id=int(API_ID) if API_ID else 0,
                    api_hash=str(API_HASH) if API_HASH else "",
                    in_memory=True,
                    sleep_threshold=0,
                    max_concurrent_transmissions=1,
                    workers=1
                )
                await state["client"].connect()
                sent_code = await state["client"].send_code(phone_number)
            except Exception as e:
                await message.reply(f"Error sending code: {str(e)}\nPlease try /login again.")
                if "client" in state:
                    await state["client"].disconnect()
                del login_states[user_id]
                return
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
                logger.error(f"Login code check error: {e}")
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
            except PasswordHashInvalid:
                await message.reply("❌ Invalid password. Please try /login again.")
                await temp_client.disconnect()
                del login_states[user_id]
                return
            except Exception as e:
                logger.error(f"Login password check error: {e}")
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
        logger.error(f"handle_login_steps error: {e}")
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
