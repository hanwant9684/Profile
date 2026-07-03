"""
Telethon engine commands — premium only.

/tlogin   — connect a Telethon userbot session (separate from Pyrogram session)
/tlogout  — disconnect & clear the Telethon session
/setengine pyrogram|telethon — switch the active download engine
/cancel_tlogin — abort an in-progress /tlogin flow
"""

import time
import logging
from pyrogram import filters
from pyrogram.types import Message, LinkPreviewOptions

from bot.config import app, API_ID, API_HASH, telethon_login_states, telethon_clients, telethon_clients_last_used
from bot.database import (
    get_user,
    save_telethon_session,
    logout_telethon_user,
    get_download_engine,
    set_download_engine,
)

logger = logging.getLogger(__name__)

_PREMIUM_ROLES = ("premium", "admin", "owner")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_premium(user: dict) -> bool:
    return user.get("role") in _PREMIUM_ROLES


async def _evict_telethon_client(user_id: int):
    """Disconnect and remove a cached Telethon client."""
    entry = telethon_clients.pop(user_id, None)
    telethon_clients_last_used.pop(user_id, None)
    if entry:
        try:
            await entry["client"].disconnect()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# /tlogin
# ---------------------------------------------------------------------------

@app.on_message(filters.command("tlogin") & filters.private)
async def tlogin_start(client, message: Message):
    user_id = message.from_user.id
    user = await get_user(user_id)

    if not user:
        await message.reply("Please run /start first.")
        return

    if not _is_premium(user):
        await message.reply(
            "❌ **Telethon engine is a Premium feature.**\n\n"
            "Upgrade to unlock the Telethon download engine and its faster parallel transfers.\n"
            "👉 /upgrade"
        )
        return

    if user.get("telethon_session_string"):
        await message.reply(
            "✅ Telethon session is already active.\n\n"
            "Use /tlogout to remove it first, then /tlogin to reconnect."
        )
        return

    if user_id in telethon_login_states:
        state = telethon_login_states[user_id]
        if "client" in state:
            try:
                await state["client"].disconnect()
            except Exception:
                pass
        telethon_login_states.pop(user_id, None)

    telethon_login_states[user_id] = {"step": "PHONE", "timestamp": time.time()}
    await message.reply(
        "📱 **Telethon Login — Phone Number**\n\n"
        "Send your phone number in international format:\n"
        "`+1234567890`\n\n"
        "⏳ Session expires in 5 minutes if inactive.\n"
        "_Type /cancel_tlogin to abort._"
    )


# ---------------------------------------------------------------------------
# /tlogout
# ---------------------------------------------------------------------------

@app.on_message(filters.command("tlogout") & filters.private)
async def tlogout_command(client, message: Message):
    user_id = message.from_user.id
    user = await get_user(user_id)

    if not user or not _is_premium(user):
        await message.reply("❌ Telethon engine is a Premium feature.")
        return

    if not user.get("telethon_session_string"):
        await message.reply("ℹ️ No Telethon session found. Use /tlogin to connect one.")
        return

    await _evict_telethon_client(user_id)
    await logout_telethon_user(user_id)

    # If the engine was Telethon, revert to Pyrogram automatically
    current_engine = await get_download_engine(user_id)
    if current_engine == "telethon":
        await set_download_engine(user_id, "pyrogram")
        extra = "\n\n🔄 Download engine switched back to **Pyrogram** automatically."
    else:
        extra = ""

    logger.info(f"Telethon logout: user={user_id}")
    await message.reply(
        "✅ **Telethon session removed.**\n"
        "Your Pyrogram session (if any) is unaffected." + extra
    )


# ---------------------------------------------------------------------------
# /setengine
# ---------------------------------------------------------------------------

@app.on_message(filters.command("setengine") & filters.private)
async def setengine_command(client, message: Message):
    user_id = message.from_user.id
    user = await get_user(user_id)

    if not user:
        await message.reply("Please run /start first.")
        return

    if not _is_premium(user):
        await message.reply(
            "❌ **/setengine is a Premium feature.**\n\n"
            "The Telethon engine is available for Premium users only.\n"
            "👉 /upgrade"
        )
        return

    parts = message.text.strip().split()
    if len(parts) < 2:
        current = await get_download_engine(user_id)
        has_tl = bool(user.get("telethon_session_string"))
        has_py = bool(user.get("phone_session_string"))
        py_label = "▶️ **Standard** ← active" if current == "pyrogram" else "   Standard"
        tl_label = "▶️ **Fast** ← active"     if current == "telethon"  else "   Fast"
        await message.reply(
            f"⚙️ **Download Engine**\n\n"
            f"{py_label}\n"
            f"{tl_label}\n\n"
            f"Sessions:\n"
            f"{'✅' if has_py else '❌'} Account session (/login)\n"
            f"{'✅' if has_tl else '❌'} Fast session (/tlogin)\n\n"
            f"Switch with:\n"
            f"`/setengine pyrogram` — standard\n"
            f"`/setengine telethon` — faster downloads",
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
        return

    engine = parts[1].lower().strip()
    if engine not in ("pyrogram", "telethon"):
        await message.reply("❌ Unknown engine. Use `/setengine pyrogram` or `/setengine telethon`.")
        return

    if engine == "telethon":
        if not user.get("telethon_session_string"):
            await message.reply(
                "❌ **No Telethon session found.**\n\n"
                "You need to log in with Telethon first before switching to it.\n"
                "Use /tlogin to connect your account via Telethon."
            )
            return
        if not user.get("bot_token"):
            await message.reply(
                "❌ **No upload bot registered.**\n\n"
                "The Telethon engine uses Telethon only for **downloading**. "
                "Uploading still goes through your personal upload bot.\n\n"
                "Please run /setbot first, then switch engine."
            )
            return

    await set_download_engine(user_id, engine)
    logger.info(f"Engine set to '{engine}' for user={user_id}")

    if engine == "telethon":
        await message.reply(
            "✅ **Fast engine activated.**\n\n"
            "Your downloads will now use the fast engine.\n"
            "Uploads go through your registered bot (/setbot).\n\n"
            "Switch back anytime: `/setengine pyrogram`"
        )
    else:
        await _evict_telethon_client(user_id)
        await message.reply(
            "✅ **Standard engine activated.**\n\n"
            "Your downloads will use the standard engine."
        )


# ---------------------------------------------------------------------------
# /cancel_tlogin
# ---------------------------------------------------------------------------

@app.on_message(filters.command("cancel_tlogin") & filters.private)
async def cancel_tlogin(client, message: Message):
    user_id = message.from_user.id
    if user_id in telethon_login_states:
        state = telethon_login_states.pop(user_id, {})
        if "client" in state:
            try:
                await state["client"].disconnect()
            except Exception:
                pass
        await message.reply("✅ Telethon login cancelled.")
    else:
        await message.reply("No active Telethon login to cancel.")


# ---------------------------------------------------------------------------
# Message handler — processes Telethon login steps (PHONE / CODE / PASSWORD)
# ---------------------------------------------------------------------------

@app.on_message(
    filters.private & filters.text
    & ~filters.command([
        "start", "login", "logout", "cancel", "cancelbatch", "cancel_login",
        "tlogin", "tlogout", "setengine", "cancel_tlogin",
        "myinfo", "setrole", "download", "upgrade", "broadcast", "ban", "unban",
        "settings", "set_force_sub", "userinfo",
        "help", "batch", "mlinks", "stats", "killall", "premium_users",
        "setbot", "rembot", "caprem", "capadd",
    ])
    & ~filters.regex(r"https://t\.me/")
)
async def handle_tlogin_steps(client, message: Message):
    user_id = message.from_user.id
    if user_id not in telethon_login_states:
        return

    state = telethon_login_states[user_id]
    step = state["step"]
    state["timestamp"] = time.time()

    try:
        if step == "PHONE":
            phone = message.text.strip().replace(" ", "")
            if not phone.startswith("+") or not phone[1:].isdigit():
                await message.reply("❌ Invalid format. Use international format: `+1234567890`")
                return

            try:
                await message.delete()
            except Exception:
                pass

            from telethon import TelegramClient
            from telethon.sessions import StringSession

            tl_client = TelegramClient(
                StringSession(),
                int(API_ID),
                str(API_HASH),
                connection_retries=3,
            )
            await tl_client.connect()
            sent = await tl_client.send_code_request(phone)

            state["client"] = tl_client
            state["phone"] = phone
            state["phone_code_hash"] = sent.phone_code_hash
            state["step"] = "CODE"

            await message.reply(
                "📩 **Verification Code**\n\n"
                "A code was sent to your Telegram account.\n"
                "Enter it here with spaces: `1 2 3 4 5`"
            )

        elif step == "CODE":
            code = message.text.replace("-", "").replace(" ", "").strip()
            tl_client = state["client"]

            try:
                await message.delete()
            except Exception:
                pass

            try:
                from telethon import errors as tl_errors
                await tl_client.sign_in(state["phone"], code=code, phone_code_hash=state["phone_code_hash"])
            except Exception as e:
                err_str = str(e)
                if "SessionPasswordNeededError" in type(e).__name__ or "password" in err_str.lower():
                    state["step"] = "PASSWORD"
                    await message.reply(
                        "🔒 **Two-Step Verification**\n\n"
                        "Your account has a cloud password. Send it now."
                    )
                    return
                elif "PhoneCodeInvalidError" in type(e).__name__ or "PHONE_CODE_INVALID" in err_str:
                    await message.reply("❌ Invalid code. Check and try again.")
                    return
                elif "PhoneCodeExpiredError" in type(e).__name__ or "PHONE_CODE_EXPIRED" in err_str:
                    await message.reply("⏰ Code expired. Use /tlogin to start over.")
                    try:
                        await tl_client.disconnect()
                    except Exception:
                        pass
                    telethon_login_states.pop(user_id, None)
                    return
                else:
                    logger.error(f"Telethon CODE step error for {user_id}: {e}")
                    await message.reply(f"❌ Login failed: {e}\n\nUse /tlogin to try again.")
                    try:
                        await tl_client.disconnect()
                    except Exception:
                        pass
                    telethon_login_states.pop(user_id, None)
                    return

            await _finish_tlogin(user_id, tl_client, message)

        elif step == "PASSWORD":
            password = message.text.strip()
            tl_client = state["client"]

            try:
                await message.delete()
            except Exception:
                pass

            try:
                await tl_client.sign_in(password=password)
            except Exception as e:
                err_str = str(e)
                if "PasswordHashInvalidError" in type(e).__name__ or "PASSWORD_HASH_INVALID" in err_str:
                    await message.reply("❌ Wrong password. Use /tlogin to try again.")
                else:
                    logger.error(f"Telethon PASSWORD step error for {user_id}: {e}")
                    await message.reply(f"❌ 2FA failed: {e}\n\nUse /tlogin to try again.")
                try:
                    await tl_client.disconnect()
                except Exception:
                    pass
                telethon_login_states.pop(user_id, None)
                return

            await _finish_tlogin(user_id, tl_client, message)

    except Exception as e:
        logger.error(f"handle_tlogin_steps error user={user_id}: {e}")
        await message.reply("An error occurred during Telethon login. Use /tlogin to try again.")
        state = telethon_login_states.pop(user_id, {})
        if "client" in state:
            try:
                await state["client"].disconnect()
            except Exception:
                pass


async def _finish_tlogin(user_id: int, tl_client, message: Message):
    """Export session string, save to DB, disconnect the temp client."""
    try:
        session_string = tl_client.session.save()
        await save_telethon_session(user_id, session_string)
        logger.info(f"Telethon login successful: user={user_id}")
    finally:
        try:
            await tl_client.disconnect()
        except Exception:
            pass
        telethon_login_states.pop(user_id, None)

    current_engine = await get_download_engine(user_id)
    engine_note = ""
    if current_engine != "telethon":
        engine_note = (
            "\n\n💡 To use Telethon as your download engine:\n"
            "`/setengine telethon`\n"
            "_(requires /setbot to be set up too)_"
        )

    await message.reply(
        "✅ **Telethon session connected!**\n\n"
        "Your Telethon account is now ready." + engine_note,
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )
