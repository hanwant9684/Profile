import aiohttp
import logging
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from bot.config import RICHADS_PUBLISHER_ID, RICHADS_WIDGET_ID, AD_DAILY_LIMIT, AD_FOR_PREMIUM
from bot.database import get_user, increment_ad_count, get_ad_count_today

logger = logging.getLogger(__name__)

async def fetch_ad(user_id, lang_code="en"):
    url = "http://15068.xml.adx1.com/telegram-mb"
    payload = {
        "language_code": lang_code,
        "publisher_id": RICHADS_PUBLISHER_ID,
        "widget_id": RICHADS_WIDGET_ID,
        "telegram_id": str(user_id),
        "production": True
    }
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, timeout=5) as response:
                if response.status == 200:
                    ads = await response.json()
                    if ads and isinstance(ads, list) and len(ads) > 0:
                        logger.info(f"RichAds: Ad received for user {user_id}")
                        return ads[0]
                    else:
                        logger.info(f"RichAds: Request sent but no ad available for user {user_id}")
                else:
                    logger.warning(f"RichAds: API returned status {response.status} for user {user_id}")
    except Exception as e:
        logger.error(f"RichAds: Error fetching ad: {e}")
    return None

async def show_ad(client, user_id, lang_code="en"):
    user = await get_user(user_id)
    if not user:
        return
    
    # Check premium settings
    if user.get("role") in ["premium", "admin", "owner"] and not AD_FOR_PREMIUM:
        return
    
    # Check daily limit
    ad_count = await get_ad_count_today(user_id)
    if ad_count >= AD_DAILY_LIMIT:
        logger.info(f"RichAds: Daily limit reached for user {user_id} ({ad_count}/{AD_DAILY_LIMIT})")
        return

    logger.info(f"RichAds: Requesting ad for user {user_id}...")
    ad_data = await fetch_ad(user_id, lang_code)
    if not ad_data:
        return

    try:
        # According to docs, we send as photo
        caption = f"**{ad_data.get('title', 'Advertisement')}**\n\n{ad_data.get('message', '')}"
        reply_markup = InlineKeyboardMarkup([
            [InlineKeyboardButton(ad_data.get("button", "Go!"), url=ad_data.get("link"))]
        ])
        
        await client.send_photo(
            chat_id=user_id,
            photo=ad_data.get("image") or ad_data.get("image_preload"),
            caption=caption,
            reply_markup=reply_markup
        )
        logger.info(f"RichAds: Ad successfully displayed to user {user_id}")
        
        # Trigger notification URL if provided
        notification_url = ad_data.get("notification_url")
        if notification_url:
            async with aiohttp.ClientSession() as session:
                await session.get(notification_url)
        
        await increment_ad_count(user_id)
        
    except Exception as e:
        logger.error(f"RichAds: Error showing ad: {e}")
