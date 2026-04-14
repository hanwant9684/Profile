from pyrogram import filters
from bot.config import app
from bot.database import get_user, check_and_update_quota

@app.on_message(filters.command("myinfo") & filters.private)
async def myinfo(client, message):
    user_id = message.from_user.id
    user = await get_user(user_id)
    if not user:
        await message.reply("User not found. /start first.")
        return
        
    role_raw = user.get('role', 'free')
    role = role_raw.upper()
    quota_info = "Unlimited" if role_raw in ['premium', 'admin', 'owner'] else f"{user.get('downloads_today', 0)}/5"
    
    expiry_info = ""
    if role_raw == 'premium' and user.get('premium_expiry_date'):
        expiry_info = f"\nExpires: `{user.get('premium_expiry_date')}`"

    await message.reply(
        f"👤 **User Info**\n"
        f"ID: `{user_id}`\n"
        f"Role: **{role}**\n"
        f"Daily Usage: {quota_info}"
        f"{expiry_info}\n"
        f"Logged in: {'Yes' if user.get('phone_session_string') else 'No'}"
    )
