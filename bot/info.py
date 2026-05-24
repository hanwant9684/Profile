from hydrogram import filters
from bot.config import app
from bot.database import get_user, DAILY_LIMIT, MONTHLY_LIMIT
from datetime import datetime

@app.on_message(filters.command("myinfo") & filters.private)
async def myinfo(client, message):
    user_id = message.from_user.id
    user = await get_user(user_id)
    if not user:
        await message.reply("User not found. /start first.")
        return

    role_raw = user.get('role', 'free')
    role = role_raw.upper()
    is_privileged = role_raw in ['premium', 'admin', 'owner']

    if is_privileged:
        quota_info = "Unlimited"
    else:
        today = datetime.now().date()
        this_month_first = today.replace(day=1)

        downloads_today = user.get("downloads_today", 0)
        last_dl_date = user.get("last_download_date")
        if last_dl_date and datetime.fromisoformat(last_dl_date).date() != today:
            downloads_today = 0

        downloads_this_month = user.get("downloads_this_month", 0)
        last_dl_month = user.get("last_download_month")
        if last_dl_month and datetime.fromisoformat(last_dl_month).date() != this_month_first:
            downloads_this_month = 0

        quota_info = (
            f"{downloads_today}/{DAILY_LIMIT} today · "
            f"{downloads_this_month}/{MONTHLY_LIMIT} this month"
        )

    expiry_info = ""
    if role_raw == 'premium' and user.get('premium_expiry_date'):
        expiry_info = f"\nExpires: `{user.get('premium_expiry_date')}`"

    await message.reply(
        f"👤 **User Info**\n"
        f"ID: `{user_id}`\n"
        f"Role: **{role}**\n"
        f"Usage: {quota_info}"
        f"{expiry_info}\n"
        f"Logged in: {'Yes' if user.get('phone_session_string') else 'No'}"
    )
