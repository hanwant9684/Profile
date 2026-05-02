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


# ─── /start ──────────────────────────────────────────────────────────────────

@app.on_message(filters.command("start") & filters.private)
async def start(client, message):
    user_id = message.from_user.id
    username = message.from_user.username
    full_name = f"{message.from_user.first_name or ''} {message.from_user.last_name or ''}".strip()

    from bot.handlers import verify_force_sub
    from bot.database import get_setting

    user_pre = await get_user(user_id)
    role_pre = (user_pre.get("role") if user_pre else None) or "free"
    if role_pre not in ("admin", "owner"):
        mm = await get_setting("maintenance_mode")
        if mm and mm.get("value") == "on":
            await message.reply(
                "🔧 **Bot is under maintenance.**\n\n"
                "We'll be back shortly. Please try again later."
            )
            return

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
        if not user:
            # create_user failed — still allow them to proceed; accept_terms will upsert
            user = {"telegram_id": user_id, "is_agreed_terms": False, "role": "free"}
    else:
        if user.get("username") != username or user.get("full_name") != full_name:
            await create_user(user_id, username, full_name)
            user = await get_user(user_id) or user

    # Already fully onboarded — show a clean welcome back
    if user and user.get("is_agreed_terms"):
        logged_in = bool(user.get("phone_session_string"))
        has_bot = bool(user.get("bot_token"))
        role = user.get("role", "free").capitalize()

        status_parts = []
        if logged_in:
            status_parts.append("🔐 Account connected")
        if has_bot:
            status_parts.append("🤖 Upload bot set")
        if not status_parts:
            status_parts.append("Public links ready")

        status_line = " · ".join(status_parts)

        buttons = []
        if not logged_in:
            buttons.append([InlineKeyboardButton("🔐 Connect Account", callback_data="onboard_login")])
        if logged_in and not has_bot:
            buttons.append([InlineKeyboardButton("🤖 Set Upload Bot", callback_data="onboard_setbot")])
        buttons.append([InlineKeyboardButton("📊 My Stats", callback_data="show_myinfo")])

        await message.reply(
            f"👋 **Welcome back!**\n\n"
            f"Role: **{role}**\n"
            f"Status: {status_line}\n\n"
            f"Send any Telegram link to start downloading.",
            reply_markup=InlineKeyboardMarkup(buttons) if buttons else None
        )
        return

    # New user — show welcome + T&C
    await message.reply(
        "👋 **Welcome to the Downloader Bot!**\n\n"
        "I can download media from Telegram links — photos, videos, files and more.\n\n"
        "📋 **Quick Terms:**\n"
        "• No illegal content\n"
        "• You are responsible for what you download\n"
        "• Use responsibly\n\n"
        "Tap below to accept and get started. It only takes a few seconds to set up!",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Accept & Get Started", callback_data="accept_terms")]
        ])
    )


# ─── Accept T&C → offer onboarding paths ─────────────────────────────────────

@app.on_callback_query(filters.regex("accept_terms"))
async def accept_terms(client, callback_query):
    user_id = callback_query.from_user.id
    username = callback_query.from_user.username
    full_name = f"{callback_query.from_user.first_name or ''} {callback_query.from_user.last_name or ''}".strip()
    await update_user_terms(user_id, True, username=username, full_name=full_name)
    try:
        await callback_query.message.edit_text(
            "✅ **You're in!**\n\n"
            "You can send any **public** Telegram link right now and I'll download it.\n\n"
            "Want to also download from **private or restricted** channels?\n"
            "Connect your Telegram account — it only takes ~30 seconds.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔐 Connect Account", callback_data="onboard_login")],
                [InlineKeyboardButton("⚡ Skip — Public Links Only", callback_data="onboard_skip")]
            ])
        )
    except Exception as e:
        if "MESSAGE_NOT_MODIFIED" not in str(e):
            logger.error(f"accept_terms edit error: {e}")
    await callback_query.answer()


# ─── Onboarding: skip login ───────────────────────────────────────────────────

@app.on_callback_query(filters.regex("onboard_skip"))
async def onboard_skip(client, callback_query):
    try:
        await callback_query.message.edit_text(
            "🚀 **You're all set!**\n\n"
            "Send any public Telegram link to start downloading.\n\n"
            "You can always connect your account later using /login\n"
            "and set up an upload bot using /setbot."
        )
    except Exception as e:
        if "MESSAGE_NOT_MODIFIED" not in str(e):
            logger.error(f"onboard_skip edit error: {e}")
    await callback_query.answer()


# ─── Onboarding: start login from button ─────────────────────────────────────

@app.on_callback_query(filters.regex("onboard_login"))
async def onboard_login(client, callback_query):
    user_id = callback_query.from_user.id
    user = await get_user(user_id)

    if user and user.get("phone_session_string"):
        has_bot = bool(user.get("bot_token"))
        if has_bot:
            await callback_query.answer("✅ You're already fully set up!", show_alert=True)
        else:
            try:
                await callback_query.message.edit_text(
                    "✅ **Account already connected!**\n\n"
                    "One more optional step: register a personal upload bot.\n"
                    "This keeps your uploads isolated and avoids rate limits.\n\n"
                    "1. Open @BotFather → /newbot → copy the token\n"
                    "2. Paste the token here",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("⏭ Skip for now", callback_data="onboard_skip_bot")]
                    ])
                )
                login_states[user_id] = {"step": "AWAITING_BOT_TOKEN", "timestamp": time.time()}
            except Exception as e:
                if "MESSAGE_NOT_MODIFIED" not in str(e):
                    logger.error(f"onboard_login already-logged-in edit error: {e}")
            await callback_query.answer()
        return

    if len(login_states) >= 10:
        await callback_query.answer("Too many active logins. Try in a minute.", show_alert=True)
        return

    login_states[user_id] = {"step": "PHONE", "timestamp": time.time()}
    try:
        await callback_query.message.edit_text(
            "📱 **Step 1 of 2 — Phone Number**\n\n"
            "Send your phone number in international format:\n"
            "`+1234567890`\n\n"
            "⏳ This session will expire in 5 minutes if inactive.\n\n"
            "_Tip: type /cancel\\_login to abort at any time._"
        )
    except Exception as e:
        if "MESSAGE_NOT_MODIFIED" not in str(e):
            logger.error(f"onboard_login edit error: {e}")
    await callback_query.answer()


# ─── Onboarding: show setbot prompt from welcome-back button ─────────────────

@app.on_callback_query(filters.regex("onboard_setbot"))
async def onboard_setbot(client, callback_query):
    user_id = callback_query.from_user.id
    user = await get_user(user_id)
    if not user or not user.get("phone_session_string"):
        await callback_query.answer("Connect your account first with /login.", show_alert=True)
        return

    login_states[user_id] = {"step": "AWAITING_BOT_TOKEN", "timestamp": time.time()}
    try:
        await callback_query.message.edit_text(
            "🤖 **Register Your Upload Bot**\n\n"
            "Your own bot handles file uploads, keeping your quota separate from other users.\n\n"
            "1. Open @BotFather → /newbot\n"
            "2. Choose a name and username\n"
            "3. Copy the token and paste it here\n\n"
            "_Token looks like: `123456789:AAH...`_",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⏭ Skip for now", callback_data="onboard_skip_bot")]
            ])
        )
    except Exception as e:
        if "MESSAGE_NOT_MODIFIED" not in str(e):
            logger.error(f"onboard_setbot edit error: {e}")
    await callback_query.answer()


# ─── Onboarding: skip bot setup ──────────────────────────────────────────────

@app.on_callback_query(filters.regex("onboard_skip_bot"))
async def onboard_skip_bot(client, callback_query):
    user_id = callback_query.from_user.id
    login_states.pop(user_id, None)
    try:
        await callback_query.message.edit_text(
            "🚀 **You're all set!**\n\n"
            "Send any Telegram link to start downloading.\n\n"
            "You can set up an upload bot later with /setbot."
        )
    except Exception as e:
        if "MESSAGE_NOT_MODIFIED" not in str(e):
            logger.error(f"onboard_skip_bot edit error: {e}")
    await callback_query.answer()


# ─── Show myinfo from welcome-back button ────────────────────────────────────

@app.on_callback_query(filters.regex("show_myinfo"))
async def show_myinfo_callback(client, callback_query):
    from bot.database import DAILY_LIMIT, MONTHLY_LIMIT
    from datetime import datetime
    user_id = callback_query.from_user.id
    user = await get_user(user_id)
    if not user:
        await callback_query.answer("User not found.", show_alert=True)
        return

    role_raw = user.get("role", "free")
    is_privileged = role_raw in ("premium", "admin", "owner")

    if is_privileged:
        quota_info = "Unlimited"
    else:
        today = datetime.now().date()
        this_month_first = today.replace(day=1)
        dl_today = user.get("downloads_today", 0)
        last_dl_date = user.get("last_download_date")
        if last_dl_date and datetime.fromisoformat(last_dl_date).date() != today:
            dl_today = 0
        dl_month = user.get("downloads_this_month", 0)
        last_dl_month = user.get("last_download_month")
        if last_dl_month and datetime.fromisoformat(last_dl_month).date() != this_month_first:
            dl_month = 0
        quota_info = f"{dl_today}/{DAILY_LIMIT} today · {dl_month}/{MONTHLY_LIMIT} this month"

    expiry_info = ""
    if role_raw == "premium" and user.get("premium_expiry_date"):
        expiry_info = f"\n📅 Expires: `{user.get('premium_expiry_date')}`"

    await callback_query.answer(
        f"👤 {user_id} | Role: {role_raw.upper()}\n{quota_info}",
        show_alert=True
    )


# ─── /login command (for returning users) ────────────────────────────────────

@app.on_message(filters.command("login") & filters.private)
async def login_start(client, message):
    user_id = message.from_user.id
    user = await get_user(user_id)

    if not user or not user.get("is_agreed_terms"):
        await message.reply("Please run /start first.")
        return

    if user and user.get("phone_session_string"):
        await message.reply(
            "✅ You're already logged in.\n\n"
            "Use /logout first if you want to re-login."
        )
        return

    if len(login_states) >= 10:
        await message.reply("⚠️ Too many active login attempts. Please try again in a few minutes.")
        return

    login_states[user_id] = {"step": "PHONE", "timestamp": time.time()}
    await message.reply(
        "📱 **Connect Your Account**\n\n"
        "Send your phone number in international format:\n"
        "`+1234567890`\n\n"
        "⏳ Session expires in 5 minutes if inactive.\n"
        "_Type /cancel\\_login to abort._"
    )


# ─── Login step handler ───────────────────────────────────────────────────────

@app.on_message(
    filters.private & filters.text
    & ~filters.command([
        "start", "login", "logout", "cancel", "cancelbatch", "cancel_login",
        "myinfo", "setrole", "download", "upgrade", "broadcast", "ban", "unban",
        "settings", "set_force_sub", "set_dump", "unset_dump", "set_maintenance",
        "help", "batch", "mlinks", "stats", "killall", "premium_users",
        "setbot", "rembot",
    ])
    & ~filters.regex(r"https://t\.me/")
)
async def handle_login_steps(client, message: Message):
    user_id = message.from_user.id
    if user_id not in login_states:
        return

    state = login_states[user_id]
    step = state["step"]

    # ── Bot token collection (post-login guided step) ──────────────────────
    if step == "AWAITING_BOT_TOKEN":
        state["timestamp"] = time.time()
        token = message.text.strip()

        try:
            await message.delete()
        except Exception:
            pass

        status = await message.reply("🔍 Validating bot token...")
        try:
            me = await validate_bot_token(token)
        except Exception as e:
            await status.edit_text(
                f"❌ **Invalid token.** Telegram rejected it:\n`{e}`\n\n"
                "Copy the full token from @BotFather and try again, or tap Skip.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("⏭ Skip for now", callback_data="onboard_skip_bot")]
                ])
            )
            return

        await stop_user_bot(user_id)
        await set_bot_token(user_id, token)
        login_states.pop(user_id, None)

        bot_username = f"@{me.username}" if me.username else str(me.id)
        await status.edit_text(
            f"✅ **Bot registered:** {bot_username}\n\n"
            f"Please open {bot_username} and press **Start** so it can DM you.\n\n"
            "🚀 **You're fully set up!** Send any Telegram link to start downloading.",
            disable_web_page_preview=True,
        )
        return

    # ── Telegram account login steps ───────────────────────────────────────
    try:
        if step == "PHONE":
            state["timestamp"] = time.time()
            phone_number = message.text.strip().replace(" ", "")

            if not phone_number.startswith("+") or not phone_number[1:].isdigit():
                await message.reply(
                    "❌ Invalid format. Use international format, e.g. `+1234567890`"
                )
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
                await message.reply(
                    f"❌ Error sending code: `{e}`\n\nPlease try /login again."
                )
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
            await message.reply(
                "📩 **Step 2 of 2 — Verification Code**\n\n"
                "A code was sent to your Telegram account.\n"
                "Enter it here with spaces: `1 2 3 4 5`"
            )

        elif step == "CODE":
            state["timestamp"] = time.time()
            code = message.text.replace("-", "").replace(" ", "").strip()
            temp_client = state["client"]

            try:
                await temp_client.sign_in(state["phone"], state["phone_code_hash"], code)
            except SessionPasswordNeeded:
                state["step"] = "PASSWORD"
                await message.reply(
                    "🔒 **Two-Step Verification**\n\n"
                    "Your account has a cloud password. Send it now."
                )
                return
            except PhoneCodeInvalid:
                await message.reply("❌ Invalid code. Please check and try again.")
                return
            except Exception as e:
                if "PHONE_CODE_EXPIRED" in str(e):
                    await message.reply("⏰ Code expired. Please run /login again.")
                else:
                    logger.error(f"Login code error: {e}")
                    await message.reply(f"❌ Login failed: {e}")
                try:
                    await temp_client.disconnect()
                except Exception:
                    pass
                login_states.pop(user_id, None)
                return

            await _finish_login(user_id, temp_client, message)

        elif step == "PASSWORD":
            state["timestamp"] = time.time()
            password = message.text.strip()
            temp_client = state["client"]

            try:
                await temp_client.check_password(password)
            except PasswordHashInvalid:
                await message.reply("❌ Wrong password. Please try /login again.")
                try:
                    await temp_client.disconnect()
                except Exception:
                    pass
                login_states.pop(user_id, None)
                return
            except Exception as e:
                logger.error(f"Login password error: {e}")
                await message.reply(f"❌ Login failed: {e}")
                try:
                    await temp_client.disconnect()
                except Exception:
                    pass
                login_states.pop(user_id, None)
                return

            await _finish_login(user_id, temp_client, message)

    except Exception as e:
        logger.error(f"handle_login_steps error: {e}")
        try:
            await message.reply("An error occurred. Login cancelled.")
        except Exception:
            pass
        if "client" in state:
            try:
                await state["client"].disconnect()
            except Exception:
                pass
        login_states.pop(user_id, None)


async def _finish_login(user_id: int, temp_client, message: Message):
    """Save session and transition to the bot-setup step."""
    session_string = await temp_client.export_session_string()
    await save_session_string(user_id, session_string)
    try:
        await temp_client.disconnect()
    except Exception:
        pass
    login_states.pop(user_id, None)

    from bot.handlers import _dest_channel_cache
    _dest_channel_cache.pop(user_id, None)

    # Check if user already has a bot token set up
    existing_token = await get_bot_token(user_id)
    if existing_token:
        await message.reply(
            "✅ **Account connected!**\n\n"
            "🚀 You're fully set up! Send any Telegram link to start downloading."
        )
        return

    # Prompt for bot setup
    login_states[user_id] = {"step": "AWAITING_BOT_TOKEN", "timestamp": time.time()}
    await message.reply(
        "✅ **Account connected!**\n\n"
        "One last optional step: register a personal upload bot.\n"
        "This keeps your uploads isolated and prevents rate-limit issues.\n\n"
        "1. Open @BotFather → /newbot\n"
        "2. Copy the token (e.g. `123456789:AAH...`)\n"
        "3. Paste it here",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⏭ Skip for now", callback_data="onboard_skip_bot")]
        ])
    )


# ─── /cancel_login ────────────────────────────────────────────────────────────

@app.on_message(filters.command("cancel_login") & filters.private)
async def cancel_login(client, message):
    user_id = message.from_user.id
    if user_id in login_states:
        state = login_states.pop(user_id, {})
        if "client" in state:
            try:
                await state["client"].disconnect()
            except Exception:
                pass
        await message.reply("✅ Login process cancelled.")
    else:
        await message.reply("No active login process to cancel.")


# ─── /setbot command (standalone for returning users) ────────────────────────

@app.on_message(filters.command("setbot") & filters.private)
async def setbot_command(client, message: Message):
    user_id = message.from_user.id
    user = await get_user(user_id)
    if not user or not user.get("is_agreed_terms"):
        await message.reply("Please run /start first.")
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or ":" not in parts[1]:
        await message.reply(
            "❌ **Usage:** `/setbot <bot_token>`\n\n"
            "1. Open @BotFather → /newbot → copy the token\n"
            "2. Send `/setbot <token>` here\n\n"
            "Your bot handles all uploads, isolated from other users."
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
            "Make sure you copied the full token from @BotFather."
        )
        return

    await stop_user_bot(user_id)
    await set_bot_token(user_id, token)

    bot_username = f"@{me.username}" if me.username else str(me.id)
    await status.edit_text(
        f"✅ **Bot registered:** {bot_username}\n\n"
        f"Open {bot_username} and press **Start** so it can DM you.\n"
        "Then send any link to download — your bot handles the upload.\n\n"
        "To swap bots: `/setbot <new_token>` · To remove: `/rembot`",
        disable_web_page_preview=True,
    )


# ─── /rembot command ─────────────────────────────────────────────────────────

@app.on_message(filters.command("rembot") & filters.private)
async def rembot_command(client, message: Message):
    user_id = message.from_user.id
    token = await get_bot_token(user_id)
    if not token:
        await message.reply("ℹ️ No bot registered. Use /setbot to add one.")
        return

    await stop_user_bot(user_id)
    await remove_bot_token(user_id)
    await message.reply(
        "✅ **Bot removed.** Run /setbot to register a new one before "
        "downloading restricted content."
    )


# ─── /logout command ─────────────────────────────────────────────────────────

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

    if user and user.get("phone_session_string"):
        try:
            temp_client = Client(
                f"logout_{user_id}",
                session_string=user.get("phone_session_string"),
                api_id=API_ID,
                api_hash=API_HASH,
                in_memory=True
            )
            await temp_client.start()
            await temp_client.log_out()
        except Exception:
            pass

        await logout_user(user_id)
        await message.reply("✅ Logged out successfully. Your session has been cleared.")
    else:
        await message.reply("You are not logged in.")
