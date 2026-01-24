from hydrogram import filters
from bot.config import app
from bot.database import get_user, check_and_update_quota

@app.on_message(filters.command("myinfo") & filters.private)
async def myinfo(client, message):
    user_id = message.from_user.id
    user = get_user(user_id)
    if not user:
        await message.reply("User not found. /start first.")
        return
        
    allowed, msg = check_and_update_quota(user_id)
    quota_info = "Unlimited" if user.role != 'free' else f"{user.downloads_today}/5"
    
    expiry_info = ""
    if user.role == 'premium' and user.premium_expiry_date:
        expiry_info = f"\nExpires: `{user.premium_expiry_date}`"

    await message.reply(
        f"👤 **User Info**\n"
        f"ID: `{user_id}`\n"
        f"Role: **{user.role.upper()}**\n"
        f"Daily Usage: {quota_info}"
        f"{expiry_info}\n"
        f"Logged in: {'Yes' if user.phone_session_string else 'No'}"
    )
