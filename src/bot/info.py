from hydrogram import filters
from src.bot.config import app
from src.bot.database import get_user, check_and_update_quota

@app.on_message(filters.command("myinfo") & filters.private)
async def myinfo(client, message):
    user_id = message.from_user.id
    user = await get_user(user_id)
    if not user:
        await message.reply("User not found. /start first.")
        return
        
    allowed, msg = await check_and_update_quota(user_id)
    quota_info = "Unlimited" if user.get('role') != 'free' else f"{user.get('downloads_today', 0)}/5"
    
    expiry_info = ""
    if user.get('role') == 'premium' and user.get('premium_expiry_date'):
        expiry_info = f"\nExpires: `{user.get('premium_expiry_date')}`"

    await message.reply(
        f"👤 **User Info**\n"
        f"ID: `{user_id}`\n"
        f"Role: **{user.get('role', 'free').upper()}**\n"
        f"Daily Usage: {quota_info}"
        f"{expiry_info}\n"
        f"Logged in: {'Yes' if user.get('phone_session_string') else 'No'}"
    )
