import time
from pyrogram import filters, Client
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.errors import SessionPasswordNeeded, PhoneCodeInvalid, PasswordHashInvalid
from bot.config import app, login_states, API_ID, API_HASH
from bot.database import (
    get_user, create_user, update_user_terms, save_session_string, logout_user,
    set_bot_token, remove_bot_token, get_bot_token,
)
from bot.transfer import validate_bot_token, stop_user_bot
from bot.logger import logger

@app.on_message(filters.command("start") & filters.private)
async def start(client, message):
    user_id = message.from_user.id
    username = message.from_user.username
    full_name = f"{message.from_user.first_name or ''} {message.from_user.last_name or ''}".strip()

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
        user = await create_user(user_id, username, full_name)
    else:
        if user.get("username") != username or user.get("full_name") != full_name:
            await create_user(user_id, username, full_name)
            user = await get_user(user_id)

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
    try:
        await callback_query.message.edit_text("Terms accepted! You can now use the bot.\n\nSend /login to connect your Telegram account or send a link to download.")
    except Exception as e:
        if "MESSAGE_NOT_MODIFIED" not in str(e):
            logger.error(f"Error editing message: {e}")
    await callback_query.answer()

@app.on_message(filters.command("login") & filters.private)
async def login_start(client, message):
    user_id = message.from_user.id
    user = await get_user(user_id)

    if not user or not user.get('is_agreed_terms'):
        await message.reply("Please agree to the Terms & Conditions first using /start.")
        return

    if user and user.get('phone_session_string'):
        await message.reply("You are already logged in! If you want to re-login, please use /logout first.")
        return

    if len(login_states) >= 10:
        await message.reply("⚠️ Too many active login attempts. Please try again in a few minutes.")
        return

    login_states[user_id] = {"step": "PHONE", "timestamp": time.time()}
    await message.reply(
        "To download from restricted channels, you need to log in.\n\n"
        "Please send your **Phone Number** in international format (e.g., +1234567890).\n\n"
        "⏳ This session will expire in 5 minutes if no activity is detected."
    )

@app.on_message(filters.private & filters.text & ~filters.command(["start", "login", "logout", "cancel", "cancelbatch", "cancel_login", "myinfo", "setrole", "download", "upgrade", "broadcast", "ban", "unban", "settings", "set_force_sub", "set_dump", "unset_dump", "help", "batch", "mlinks", "stats", "killall", "premium_users", "setbot", "rembot"]) & ~filters.regex(r"https://t\.me/"))
async def handle_login_steps(client, message: Message):
    user_id = message.from_user.id
    if user_id not in login_states:
        return

    state = login_states[user_id]
    step = state["step"]

    try:
        if step == "PHONE":
            state["timestamp"] = time.time()
            phone_number = message.text.strip().replace(" ", "")

            if not phone_number.startswith("+") or not phone_number[1:].isdigit():
                await message.reply("❌ **Invalid Format.** Please send in international format (e.g., +1234567890).")
                return

            try:
                state["client"] = Client(
                    f"session_{user_id}",
                    api_id=int(API_ID) if API_ID else 0,
                    api_hash=str(API_HASH) if API_HASH else "",
                    in_memory=True,
                    sleep_threshold=60,
                    max_concurrent_transmissions=5,
                    workers=5
                )
                await state["client"].connect()
                sent_code = await state["client"].send_code(phone_number)
            except Exception as e:
                await message.reply(f"Error sending code: {str(e)}\nPlease try /login again.")
                if "client" in state:
                    try:
                        await state["client"].disconnect()
                    except Exception:
                        pass
                login_states.pop(user_id, None)
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
                await message.reply("❌ **Invalid Code.** Please check and try again.")
                return
            except Exception as e:
                error_msg = str(e)
                if "PHONE_CODE_EXPIRED" in error_msg:
                    await message.reply("⏰ **Code Expired.** Please start the /login process again.")
                else:
                    logger.error(f"Login code check error: {e}")
                    await message.reply(f"❌ **Login failed:** {e}")
                try:
                    await temp_client.disconnect()
                except Exception:
                    pass
                login_states.pop(user_id, None)
                return

            session_string = await temp_client.export_session_string()
            await save_session_string(user_id, session_string)
            try:
                await temp_client.disconnect()
            except Exception:
                pass
            login_states.pop(user_id, None)
            from bot.handlers import _dest_channel_cache
            _dest_channel_cache.pop(user_id, None)
            await message.reply("✅ Login Successful! Send any private channel link to start downloading.")

        elif step == "PASSWORD":
            state["timestamp"] = time.time()
            password = message.text.strip()
            temp_client = state["client"]

            try:
                await temp_client.check_password(password)
            except PasswordHashInvalid:
                await message.reply("❌ Invalid password. Please try /login again.")
                try:
                    await temp_client.disconnect()
                except Exception:
                    pass
                login_states.pop(user_id, None)
                return
            except Exception as e:
                logger.error(f"Login password check error: {e}")
                await message.reply(f"Login failed: {e}")
                try:
                    await temp_client.disconnect()
                except Exception:
                    pass
                login_states.pop(user_id, None)
                return

            session_string = await temp_client.export_session_string()
            await save_session_string(user_id, session_string)
            try:
                await temp_client.disconnect()
            except Exception:
                pass
            login_states.pop(user_id, None)
            from bot.handlers import _dest_channel_cache
            _dest_channel_cache.pop(user_id, None)
            await message.reply("✅ Login Successful! Send any private channel link to start downloading.")

    except Exception as e:
        logger.error(f"handle_login_steps error: {e}")
        try:
            await message.reply("Error. Login cancelled.")
        except Exception as reply_err:
            logger.warning(f"Could not send login cancellation message: {reply_err}")
        if "client" in state:
            try:
                await state["client"].disconnect()
            except Exception:
                pass
        login_states.pop(user_id, None)

@app.on_message(filters.command("cancel_login") & filters.private)
async def cancel_login(client, message):
    user_id = message.from_user.id
    if user_id in login_states:
        state = login_states[user_id]
        if "client" in state:
            try:
                await state["client"].disconnect()
            except Exception:
                pass
        del login_states[user_id]
        await message.reply("✅ Login process cancelled.")
    else:
        await message.reply("No active login process to cancel.")

@app.on_message(filters.command("setbot") & filters.private)
async def setbot_command(client, message: Message):
    """Register the user's own @BotFather bot. Their bot performs all uploads.

    Usage: /setbot 1234567:AAH...token
    """
    user_id = message.from_user.id
    user = await get_user(user_id)
    if not user or not user.get("is_agreed_terms"):
        await message.reply("Please run /start and accept the terms first.")
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or ":" not in parts[1]:
        await message.reply(
            "❌ **Usage:** `/setbot <bot_token>`\n\n"
            "1. Open @BotFather and run `/newbot` to create a fresh bot.\n"
            "2. Copy the token (looks like `123456789:AAH....`).\n"
            "3. Send `/setbot <that token>` here.\n\n"
            "Your bot will be used to upload all your downloaded files. "
            "Each upload uses *your* bot's quota with Telegram, isolated from other users."
        )
        return

    token = parts[1].strip()

    try:
        await message.delete()
    except Exception:
        pass

    status = await message.reply("🔍 Validating token...")

    try:
        me = await validate_bot_token(token)
    except Exception as e:
        await status.edit_text(
            f"❌ **Invalid bot token.** Telegram rejected it:\n`{e}`\n\n"
            f"Make sure you copied the full token from @BotFather."
        )
        return

    await stop_user_bot(user_id)
    await set_bot_token(user_id, token)

    bot_username = f"@{me.username}" if me.username else str(me.id)
    await status.edit_text(
        f"✅ **Bot registered:** {bot_username}\n\n"
        f"Now please open {bot_username} and press **Start** so it's allowed to "
        f"DM you. After that, send any link to download — your bot will handle "
        f"the upload.\n\n"
        f"To swap bots later, just run `/setbot <new_token>` again.\n"
        f"To remove it, run `/rembot`.",
        disable_web_page_preview=True,
    )


@app.on_message(filters.command("rembot") & filters.private)
async def rembot_command(client, message: Message):
    """Stop the user's bot client and forget the token."""
    user_id = message.from_user.id
    token = await get_bot_token(user_id)
    if not token:
        await message.reply("ℹ️ You don't have a bot registered. Use /setbot to add one.")
        return

    await stop_user_bot(user_id)
    await remove_bot_token(user_id)
    await message.reply(
        "✅ **Bot removed.** You'll need to run /setbot again before downloading "
        "any restricted content."
    )


@app.on_message(filters.command("logout") & filters.private)
async def logout(client, message):
    user_id = message.from_user.id
    user = await get_user(user_id)

    from bot.handlers import user_clients

    stale = user_clients.pop(user_id, None)
    if stale:
        try:
            await stale["client"].stop()
        except Exception:
            pass

    if user_id in login_states:
        state = login_states.pop(user_id, None)
        if state and "client" in state:
            try:
                await state["client"].disconnect()
            except Exception:
                pass

    if user and user.get('phone_session_string'):
        try:
            temp_client = Client(
                f"logout_{user_id}",
                session_string=user.get('phone_session_string'),
                api_id=API_ID,
                api_hash=API_HASH,
                in_memory=True
            )
            await temp_client.start()
            await temp_client.log_out()
        except Exception:
            pass

        await logout_user(user_id)
        await message.reply("✅ Logged out successfully! Your session has been cleared.")
    else:
        await message.reply("You are not logged in.")
