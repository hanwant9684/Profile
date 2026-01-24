from hydrogram import filters
from hydrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from hydrogram.errors import SessionPasswordNeeded, PhoneCodeInvalid, PasswordHashInvalid
from bot.config import app, login_states, API_ID, API_HASH
from bot.database import get_user, create_user, update_user_terms, save_session_string
from hydrogram import Client

@app.on_message(filters.command("start") & filters.private)
async def start(client, message):
    user_id = message.from_user.id
    user = await get_user(user_id)
    
    if not user:
        user = await create_user(user_id)
    
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

    login_states[user_id] = {"step": "PHONE"}
    await message.reply(
        "To download from restricted channels, you need to log in.\n\n"
        "Please send your **Phone Number** in international format (e.g., +1234567890)."
    )

@app.on_message(filters.private & filters.text & ~filters.command(["start", "login", "cancel", "myinfo", "setrole", "download", "upgrade", "broadcast", "ban", "unban", "settings", "set_force_sub", "set_dump"]) & ~filters.regex(r"https://t\.me/"))
async def handle_login_steps(client, message: Message):
    user_id = message.from_user.id
    if user_id not in login_states:
        return

    state = login_states[user_id]
    step = state["step"]

    try:
        if step == "PHONE":
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
async def cancel_login(client, message):
    user_id = message.from_user.id
    if user_id in login_states:
        state = login_states[user_id]
        if "client" in state:
            await state["client"].disconnect()
        del login_states[user_id]
        await message.reply("Login cancelled.")
    else:
        await message.reply("Nothing to cancel.")
