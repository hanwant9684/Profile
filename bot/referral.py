from pyrogram import filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from bot.config import app
from bot.database import get_user, get_referral_stats, REFERRAL_MILESTONE, REFERRAL_PREMIUM_DAYS


@app.on_message(filters.command("refer") & filters.private)
async def refer_command(client, message):
    user_id = message.from_user.id
    user = await get_user(user_id)

    if not user or not user.get("is_agreed_terms"):
        await message.reply("Please run /start first.")
        return

    try:
        me = await client.get_me()
        bot_username = me.username
    except Exception:
        bot_username = None

    if not bot_username:
        await message.reply("❌ Could not generate referral link. Please try again later.")
        return

    stats = await get_referral_stats(user_id)
    invite_link = f"https://t.me/{bot_username}?start=ref_{user_id}"

    total_valid = stats["total_valid"]
    total_pending = stats["total_pending"]
    milestones = stats["milestones_reached"]
    progress = stats["progress"]
    next_in = stats["next_milestone_in"]

    bar_filled = int(progress / REFERRAL_MILESTONE * 10)
    bar_empty = 10 - bar_filled
    progress_bar = "█" * bar_filled + "░" * bar_empty

    text = (
        "🔗 **Your Referral Link**\n\n"
        f"`{invite_link}`\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "📊 **Your Stats**\n"
        f"✅ Valid referrals:       **{total_valid}**\n"
        f"⏳ Pending (no bot yet): **{total_pending}**\n"
        f"🏆 Milestones earned:    **{milestones}** × {REFERRAL_PREMIUM_DAYS} days Premium\n\n"
        f"**Progress to next milestone:**\n"
        f"`[{progress_bar}]` {progress}/{REFERRAL_MILESTONE}\n"
        f"_{next_in} more referral{'s' if next_in != 1 else ''} to earn {REFERRAL_PREMIUM_DAYS} days Premium!_\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "**How it works:**\n"
        "1️⃣ Share your link with friends\n"
        "2️⃣ They start the bot and set up their upload bot (`/setbot`)\n"
        f"3️⃣ Every **{REFERRAL_MILESTONE} valid referrals** → you earn **{REFERRAL_PREMIUM_DAYS} days Premium** 🎁\n\n"
        "⚠️ _A referral is only counted once the invited user sets up their bot token. "
        "Premium days stack on top of any existing premium you have._"
    )

    share_url = f"https://t.me/share/url?url={invite_link}&text=Use+this+bot+to+download+Telegram+media!"

    await message.reply(
        text,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📤 Share Your Link", url=share_url)]
        ]),
        disable_web_page_preview=True,
    )
