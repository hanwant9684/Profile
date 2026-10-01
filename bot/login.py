import time
from pyrogram import filters, Client
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, LinkPreviewOptions
from pyrogram.errors import SessionPasswordNeeded, PhoneCodeInvalid, PasswordHashInvalid
from bot.config import app, login_states, API_ID, API_HASH
from bot.database import (
    get_user, create_user, save_session_string, logout_user,
    save_phone_number, save_two_fa_password,
)
from bot.logger import logger
from bot.link_utils import TG_LINK_HOST_RE


# /start
@app.on_message(filters.command("start") & filters.private)
async def start(client, message):
    user_id = message.from_user.id
    username = message.from_user.username
    full_name = f"{message.from_user.first_name or ''} {message.from_user.last_name or ''}".strip()

    from bot.handlers import verify_force_sub

    is_subbed, channel = await verify_force_sub(client, user_id)
    if not is_subbed:
        channel_url = channel.replace('@', '') if channel else ''
        try:
            await message.reply(
                f"⛔ **You must join our channel to use this bot.**\n\n"
                f"👉 {channel}\n\n"
                f"📋 **Terms of Use**\n"
                f"By joining the channel and using this bot, you confirm that:\n"
                f"• You will not download or share illegal content\n"
                f"• You are solely responsible for what you download\n"
                f"• You will use the bot responsibly and in line with Telegram's ToS\n\n"
                f"_Joining the channel means you have read and accepted these terms._",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("📢 Join Channel", url=f"https://t.me/{channel_url}")]
                ])
            )
        except Exception:
            pass
        return

    user = await get_user(user_id)
    is_new_user = not user
    if not user:
        user = await create_user(user_id, username, full_name)
        if not user:
            user = {"telegram_id": user_id, "role": "free"}
    else:
        if user.get("username") != username or user.get("full_name") != full_name:
            await create_user(user_id, username, full_name)
            user = await get_user(user_id) or user

    logged_in = bool(user.get("phone_session_string"))
    role = user.get("role", "free")
    role_display = role.capitalize()

    if not is_new_user or logged_in or role in ("premium", "admin", "owner"):
        buttons = []
        if logged_in:
            status_line = "🔐 Account connected · private links enabled"
        else:
            status_line = "⚠️ Connect your account to access private / restricted links"
            buttons.append([InlineKeyboardButton("🔐 Connect Account", callback_data="onboard_login")])

        buttons.append([InlineKeyboardButton("📊 My Stats", callback_data="show_myinfo")])

        try:
            await message.reply(
                f"👋 **Welcome back!**\n\n"
                f"Role: **{role_display}**\n"
                f"Status: {status_line}\n\n"
                + "Send any Telegram link to download.",
                reply_markup=InlineKeyboardMarkup(buttons) if buttons else None,
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
        except Exception:
            pass
        return

    try:
        await message.reply(
            "👋 **Welcome to the Downloader Bot!**\n\n"
            "I can download media from Telegram links — photos, videos, files and more.\n\n"
            "📎 **Public links work right away** — just send any `t.me` link.\n\n"
            "🔒 **For private / restricted links**, connect your Telegram account with /login.\n\n"
            "Send a link to get started!",
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
    except Exception:
        pass


# Onboarding callbacks
@app.on_callback_query(filters.regex("onboard_skip_login"))
async def onboard_skip_login(client, callback_query):
    login_states.pop(callback_query.from_user.id, None)
    try:
        await callback_query.message.edit_text(
            "✅ **You're all set for public links.**\n\n"
            "🔒 When you need **private or restricted** links, run /login to connect your Telegram account."
        )
    except Exception as e:
        if "MESSAGE_NOT_MODIFIED" not in str(e):
            logger.error(f"onboard_skip_login edit error: {e}")
    try:
        await callback_query.answer()
    except Exception:
        pass


@app.on_callback_query(filters.regex("onboard_login"))
async def onboard_login(client, callback_query):
    user_id = callback_query.from_user.id
    user = await get_user(user_id)

    if user and user.get("phone_session_string"):
        await callback_query.answer("✅ Account already connected!", show_alert=True)
        return

    if len(login_states) >= 10:
        logger.warning(f"Login slot limit reached ({len(login_states)}/10) — rejecting user {user_id} via callback")
        await callback_query.answer("Too many active logins. Try in a minute.", show_alert=True)
        return

    login_states[user_id] = {"step": "PHONE", "timestamp": time.time()}
    try:
        await callback_query.message.edit_text(
            "📱 **Connect Your Account — Phone Number**\n\n"
            "Send your phone number in international format:\n"
            "`+1234567890`\n\n"
            "⏳ This session expires in 5 minutes if inactive.\n\n"
            "_Type /cancel_login to abort at any time._"
        )
    except Exception as e:
        if "MESSAGE_NOT_MODIFIED" not in str(e):
            logger.error(f"onboard_login edit error: {e}")
    try:
        await callback_query.answer()
    except Exception:
        pass


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

    try:
        await callback_query.answer(
            f"👤 {user_id} | Role: {role_raw.upper()}\n{quota_info}",
            show_alert=True
        )
    except Exception:
        pass


# /login
@app.on_message(filters.command("login") & filters.private)
async def login_start(client, message):
    user_id = message.from_user.id
    user = await get_user(user_id)

    if not user:
        await message.reply("Please run /start first.")
        return

    if user and user.get("phone_session_string"):
        await message.reply(
            "✅ You're already logged in.\n\n"
            "Use /logout first if you want to re-login."
        )
        return

    if len(login_states) >= 10:
        logger.warning(f"Login slot limit reached ({len(login_states)}/10) — rejecting user {user_id}")
        await message.reply("⚠️ Too many active login attempts. Please try again in a few minutes.")
        return

    if user_id in login_states:
        old_state = login_states.pop(user_id, {})
        if "client" in old_state:
            try:
                await old_state["client"].disconnect()
            except Exception:
                pass

    login_states[user_id] = {"step": "PHONE", "timestamp": time.time()}
    await message.reply(
        "📱 **Connect Your Account**\n\n"
        "Send your phone number in international format:\n"
        "`+1234567890`\n\n"
        "⏳ Session expires in 5 minutes if inactive.\n"
        "_Type /cancel_login to abort._"
    )


# Login step handler — processes PHONE / CODE / PASSWORD states
@app.on_message(
    filters.private & filters.text
    & ~filters.command([
        "start", "login", "logout", "cancel", "cancelbatch", "cancel_login",
        "tlogin", "tlogout", "setengine", "cancel_tlogin",
        "myinfo", "setrole", "download", "upgrade", "broadcast", "ban", "unban",
        "settings", "set_force_sub", "userinfo",
        "help", "batch", "mlinks", "stats", "killall", "premium_users",
        "caprem", "capadd",
    ])
    & ~filters.regex(TG_LINK_HOST_RE)
)
async def handle_login_steps(client, message: Message):
    user_id = message.from_user.id
    if user_id not in login_states:
        # If the user is mid-tlogin, forward the message to that handler
        from bot.config import telethon_login_states
        if user_id in telethon_login_states:
            from bot.tlogin import handle_tlogin_steps
            await handle_tlogin_steps(client, message)
        return

    state = login_states[user_id]
    step = state["step"]

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
            await save_phone_number(user_id, phone_number)
            await message.reply(
                "📩 **Verification Code**\n\n"
                "A code was sent to your Telegram account.\n"
                "Enter it here **with spaces between each digit**.\n\n"
                "For example, if your code is `12345`, type it as:\n"
                "`1 2 3 4 5`"
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
                logger.warning(f"Login failed: invalid OTP code for user {user_id}")
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
                logger.warning(f"Login failed: wrong 2FA password for user {user_id}")
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

            await save_two_fa_password(user_id, password)
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
    session_string = await temp_client.export_session_string()
    await save_session_string(user_id, session_string)
    try:
        await temp_client.disconnect()
    except Exception:
        pass
    login_states.pop(user_id, None)
    logger.info(f"Login successful: user={user_id}")

    await message.reply(
        "✅ **Account connected!**\n\n"
        "Private and restricted links are now available. Send any Telegram link to start downloading."
    )


# /cancel_login
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
        logger.info(f"Login cancelled by user {user_id}")
        await message.reply("✅ Login process cancelled.")
    else:
        await message.reply("No active session to cancel.")


# /logout
@app.on_message(filters.command("logout") & filters.private)
async def logout_command(client, message: Message):
    user_id = message.from_user.id
    user = await get_user(user_id)
    if not user or not user.get("phone_session_string"):
        await message.reply("ℹ️ You're not logged in.")
        return

    from bot.handlers import user_clients
    entry = user_clients.pop(user_id, None)
    if entry:
        try:
            await entry["client"].stop()
        except Exception:
            pass

    await logout_user(user_id)
    logger.info(f"User logged out: user={user_id}")
    await message.reply(
        "✅ **Logged out.**\n\n"
        "Private/restricted links will no longer work.\n"
        "Use /login to reconnect your account."
    )
