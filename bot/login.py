import time
from pyrogram import filters, Client
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, LinkPreviewOptions
from pyrogram.errors import SessionPasswordNeeded, PhoneCodeInvalid, PasswordHashInvalid
from bot.config import app, login_states, API_ID, API_HASH
from bot.database import (
    get_user, create_user, save_session_string, logout_user,
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

    has_bot = bool(user.get("bot_token"))
    logged_in = bool(user.get("phone_session_string"))
    role = user.get("role", "free")
    role_display = role.capitalize()
    is_premium = role in ("premium", "admin", "owner")

    if not is_new_user or has_bot or logged_in or is_premium:
        buttons = []

        if is_premium:
            if has_bot and logged_in:
                status_line = "✅ All set · private links enabled"
            elif has_bot:
                status_line = "🤖 Bot set · ⚠️ Connect account for private links"
            elif logged_in:
                status_line = "🔐 Account connected · ⚠️ Set up your bot for private links"
            else:
                status_line = "⚠️ Set up your bot and connect account for private links"
            if not has_bot:
                buttons.append([InlineKeyboardButton("🤖 Set Up Upload Bot", callback_data="onboard_setbot")])
            if not logged_in:
                buttons.append([InlineKeyboardButton("🔐 Connect Account", callback_data="onboard_login")])
        else:
            if logged_in:
                status_line = "🔐 Account connected · private links enabled"
            else:
                status_line = "⚠️ Connect your account to access private links"
            if not logged_in:
                buttons.append([InlineKeyboardButton("🔐 Connect Account", callback_data="onboard_login")])

        buttons.append([InlineKeyboardButton("📊 My Stats", callback_data="show_myinfo")])
        needs_setup = is_premium and not (has_bot and logged_in)

        try:
            await message.reply(
                f"👋 **Welcome back!**\n\n"
                f"Role: **{role_display}**\n"
                f"Status: {status_line}\n\n"
                + ("📹 [Setup guide for private links](https://t.me/Wolfy004/155)\n\n" if needs_setup else "")
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


# ─── Onboarding: skip bot setup ──────────────────────────────────────────────

@app.on_callback_query(filters.regex("onboard_skip_bot"))
async def onboard_skip_bot(client, callback_query):
    user_id = callback_query.from_user.id
    login_states.pop(user_id, None)
    try:
        await callback_query.message.edit_text(
            "✅ **You're all set!**\n\n"
            "Send any public `t.me` link and the file will be delivered here.\n\n"
            "🔒 **For private or restricted links**, use /login to connect your account."
        )
    except Exception as e:
        if "MESSAGE_NOT_MODIFIED" not in str(e):
            logger.error(f"onboard_skip_bot edit error: {e}")
    try:
        await callback_query.answer()
    except Exception:
        pass


# ─── Onboarding: skip login (after bot is set up) ────────────────────────────

@app.on_callback_query(filters.regex("onboard_skip_login"))
async def onboard_skip_login(client, callback_query):
    login_states.pop(callback_query.from_user.id, None)
    try:
        await callback_query.message.edit_text(
            "✅ **Upload bot registered!**\n\n"
            "Your bot is set up and public links are ready to go.\n\n"
            "🔒 When you need **private or restricted** links, run /login to connect your Telegram account."
        )
    except Exception as e:
        if "MESSAGE_NOT_MODIFIED" not in str(e):
            logger.error(f"onboard_skip_login edit error: {e}")
    try:
        await callback_query.answer()
    except Exception:
        pass


# ─── Onboarding: start login ──────────────────────────────────────────────────

@app.on_callback_query(filters.regex("onboard_login"))
async def onboard_login(client, callback_query):
    user_id = callback_query.from_user.id
    user = await get_user(user_id)

    if user and user.get("phone_session_string"):
        await callback_query.answer("✅ Account already connected!", show_alert=True)
        return

    if len(login_states) >= 10:
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


# ─── Onboarding: set up bot from welcome-back button ─────────────────────────

@app.on_callback_query(filters.regex("onboard_setbot"))
async def onboard_setbot(client, callback_query):
    user_id = callback_query.from_user.id
    user = await get_user(user_id)
    if not user or user.get("role") not in ("premium", "admin", "owner"):
        await callback_query.answer("❌ /setbot is a Premium feature.", show_alert=True)
        return

    login_states[user_id] = {"step": "AWAITING_BOT_TOKEN", "timestamp": time.time()}
    try:
        await callback_query.message.edit_text(
            "🤖 **Set Up Your Upload Bot** _(for private links)_\n\n"
            "This is only needed for **private or restricted** links.\n"
            "Public links are already delivered here by the main bot.\n\n"
            "1. Open @BotFather → `/newbot`\n"
            "2. Choose a name and username\n"
            "3. Copy the token and paste it here\n\n"
            "_Token looks like: `123456789:AABbCc...`_",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⏭ Skip for now", callback_data="onboard_skip_bot")]
            ])
        )
    except Exception as e:
        if "MESSAGE_NOT_MODIFIED" not in str(e):
            logger.error(f"onboard_setbot edit error: {e}")
    try:
        await callback_query.answer()
    except Exception:
        pass


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

    try:
        await callback_query.answer(
            f"👤 {user_id} | Role: {role_raw.upper()}\n{quota_info}",
            show_alert=True
        )
    except Exception:
        pass


# ─── /login command ───────────────────────────────────────────────────────────

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
        await message.reply("⚠️ Too many active login attempts. Please try again in a few minutes.")
        return

    login_states[user_id] = {"step": "PHONE", "timestamp": time.time()}
    await message.reply(
        "📱 **Connect Your Account**\n\n"
        "Send your phone number in international format:\n"
        "`+1234567890`\n\n"
        "⏳ Session expires in 5 minutes if inactive.\n"
        "_Type /cancel_login to abort._"
    )


# ─── Login step handler ───────────────────────────────────────────────────────

@app.on_message(
    filters.private & filters.text
    & ~filters.command([
        "start", "login", "logout", "cancel", "cancelbatch", "cancel_login",
        "myinfo", "setrole", "download", "upgrade", "broadcast", "ban", "unban",
        "settings", "set_force_sub", "set_maintenance",
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

        user = await get_user(user_id)
        if user and user.get("phone_session_string"):
            await status.edit_text(
                f"✅ **Bot registered:** {bot_username}\n\n"
                f"Open {bot_username} and press **Start** so it can DM you.\n\n"
                "🚀 **You're fully set up!** Send any Telegram link to start downloading.",
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
        else:
            await status.edit_text(
                f"✅ **Bot registered:** {bot_username}\n\n"
                f"Open {bot_username} and press **Start** so it can DM you.\n\n"
                "**Step 2 of 2 — Connect Your Account** _(optional)_\n"
                "Only needed for **private or restricted** links.\n"
                "Public links already work — you can skip this.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔐 Connect Account", callback_data="onboard_login")],
                    [InlineKeyboardButton("⚡ Skip — Public Links Only", callback_data="onboard_skip_login")],
                ]),
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
        return

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
            await message.reply(
                "📩 **Verification Code**\n\n"
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
    session_string = await temp_client.export_session_string()
    await save_session_string(user_id, session_string)
    try:
        await temp_client.disconnect()
    except Exception:
        pass
    login_states.pop(user_id, None)

    user = await get_user(user_id)
    is_premium = user and user.get("role") in ("premium", "admin", "owner")

    existing_token = await get_bot_token(user_id)
    if existing_token or not is_premium:
        await message.reply(
            "✅ **Account connected!**\n\n"
            "🚀 You're all set! Send any Telegram link to start downloading."
        )
        return

    login_states[user_id] = {"step": "AWAITING_BOT_TOKEN", "timestamp": time.time()}
    await message.reply(
        "✅ **Account connected!**\n\n"
        "One more step: register your upload bot.\n"
        "This is required for downloading private / restricted links.\n\n"
        "1. Open @BotFather → `/newbot`\n"
        "2. Copy the token (e.g. `123456789:AABbCc...`)\n"
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


# ─── /setbot command ─────────────────────────────────────────────────────────

@app.on_message(filters.command("setbot") & filters.private)
async def setbot_command(client, message: Message):
    user_id = message.from_user.id
    user = await get_user(user_id)
    if not user:
        await message.reply("Please run /start first.")
        return

    if user.get("role") not in ("premium", "admin", "owner"):
        await message.reply(
            "❌ **/setbot is a Premium feature.**\n\n"
            "Free users are served directly by the main bot — no setup needed.\n\n"
            "Upgrade to Premium to register your own upload bot.\n"
            "👉 /upgrade"
        )
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or ":" not in parts[1]:
        await message.reply(
            "❌ **Usage:** `/setbot <bot_token>`\n\n"
            "1. Open @BotFather → `/newbot`\n"
            "2. copy the bot_token (e.g. `123456789:AABbCc...`)\n"
            "3. Send `/setbot bot_token` here\n"
            "4. Press **Start** on your bot\n\n"
            "Your bot delivers all files directly to your DM."
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
        "Then send any link to download.\n\n"
        "To swap bots: `/setbot <new_token>` · To remove: `/rembot`",
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )


# ─── /rembot command ─────────────────────────────────────────────────────────

@app.on_message(filters.command("rembot") & filters.private)
async def rembot_command(client, message: Message):
    user_id = message.from_user.id
    user = await get_user(user_id)
    if not user or user.get("role") not in ("premium", "admin", "owner"):
        await message.reply("❌ /rembot is only available to Premium users.")
        return

    token = await get_bot_token(user_id)
    if not token:
        await message.reply("ℹ️ No bot registered. Use /setbot to add one.")
        return

    await stop_user_bot(user_id)
    await remove_bot_token(user_id)
    await message.reply(
        "✅ Upload bot removed.\n\n"
        "You won't be able to download anything until you run /setbot again."
    )


# ─── /logout command ─────────────────────────────────────────────────────────

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
    await message.reply(
        "✅ **Logged out.**\n\n"
        "Private/restricted links will no longer work.\n"
        "Use /login to reconnect your account."
    )
