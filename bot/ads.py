import aiohttp
import logging
import html
from typing import Optional, Dict, Any, List
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from bot.config import RICHADS_PUBLISHER_ID, RICHADS_WIDGET_ID, AD_DAILY_LIMIT, AD_FOR_PREMIUM
from bot.database import get_user, increment_ad_count, get_ad_count_today

logger = logging.getLogger(__name__)

RICHADS_API_URL = "http://15068.xml.adx1.com/telegram-mb"

class RichAdsManager:
    def __init__(self):
        self.publisher_id = RICHADS_PUBLISHER_ID
        self.widget_id = RICHADS_WIDGET_ID
        self.production = True
        self.for_premium = AD_FOR_PREMIUM

    def is_enabled(self) -> bool:
        """Check if RichAds is configured"""
        return bool(self.publisher_id)

    async def fetch_ad(self, language_code: str = "en", telegram_id: str = None) -> Optional[List[Dict[str, Any]]]:
        """Fetch ad from RichAds API"""
        if not self.is_enabled():
            return None

        payload = {
            "language_code": language_code[:2].lower() if language_code else "en",
            "publisher_id": self.publisher_id,
            "production": self.production
        }

        if self.widget_id:
            payload["widget_id"] = self.widget_id
        if telegram_id:
            payload["telegram_id"] = str(telegram_id)

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(RICHADS_API_URL, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as response:
                    if response.status == 200:
                        ads = await response.json()
                        if ads and len(ads) > 0:
                            logger.info(f"RichAds: Ad received for user {telegram_id}")
                            return ads
                        logger.info(f"RichAds: No ads available for user {telegram_id}")
                        return None
                    else:
                        logger.warning(f"RichAds: API Error {response.status} for user {telegram_id}")
                        return None
        except Exception as e:
            logger.error(f"RichAds: Fetch error for user {telegram_id}: {e}")
            return None

    async def notify_impression(self, notification_url: str):
        """Notify RichAds that ad impression happened"""
        if not notification_url:
            return
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(html.unescape(notification_url), timeout=5) as response:
                    if response.status == 200:
                        logger.debug("RichAds: Impression tracked")
        except Exception as e:
            logger.debug(f"RichAds: Impression error: {e}")

    async def show_ad(self, client, user_id, lang_code="en"):
        """Fetch and show RichAd to user"""
        if not self.is_enabled():
            return

        user = await get_user(user_id)
        if not user:
            return
        
        # Check premium settings
        if user.get("role") in ["premium", "admin", "owner"] and not self.for_premium:
            return
        
        # Check daily limit
        ad_count = await get_ad_count_today(user_id)
        if ad_count >= AD_DAILY_LIMIT:
            logger.info(f"RichAds: Daily limit reached for user {user_id}")
            return

        ads = await self.fetch_ad(language_code=lang_code, telegram_id=str(user_id))
        if not ads:
            return

        ad = ads[0]
        try:
            click_url = html.unescape(ad.get("link", ""))
            image_url = html.unescape(ad.get("image") or ad.get("image_preload") or "")
            video_url = html.unescape(ad.get("video") or "")
            
            # Build caption
            title = ad.get("title", "Sponsored")
            message = ad.get("message", "")
            brand = ad.get("brand", "")
            
            caption = f"📢 **{title}**\n\n{message}"
            if brand:
                caption += f"\n\n🏷️ {brand}"

            button_text = ad.get("button", "Learn More")
            reply_markup = InlineKeyboardMarkup([
                [InlineKeyboardButton(f"👉 {button_text}", url=click_url)]
            ])

            # Try to send as video first if available
            if video_url:
                try:
                    await client.send_video(
                        chat_id=user_id,
                        video=video_url,
                        caption=caption,
                        reply_markup=reply_markup
                    )
                    logger.info(f"RichAds: Video ad displayed to {user_id}")
                except Exception as ve:
                    logger.warning(f"RichAds: Failed to send video ad: {ve}. Falling back to photo.")
                    if image_url:
                        await client.send_photo(
                            chat_id=user_id,
                            photo=image_url,
                            caption=caption,
                            reply_markup=reply_markup
                        )
            elif image_url:
                await client.send_photo(
                    chat_id=user_id,
                    photo=image_url,
                    caption=caption,
                    reply_markup=reply_markup
                )
            else:
                await client.send_message(
                    chat_id=user_id,
                    text=caption,
                    reply_markup=reply_markup
                )

            logger.info(f"RichAds: Ad successfully displayed to user {user_id}")
            
            # Track impression
            notification_url = ad.get("notification_url")
            if notification_url:
                await self.notify_impression(notification_url)
            
            await increment_ad_count(user_id)
            
        except Exception as e:
            # Silently handle errors showing ads to prevent log clutter and disruptions
            pass

# Global instance and legacy compatibility
richads_manager = RichAdsManager()
async def fetch_ad(user_id, lang_code="en"):
    ads = await richads_manager.fetch_ad(lang_code, str(user_id))
    return ads[0] if ads else None

async def show_ad(client, user_id, lang_code="en"):
    await richads_manager.show_ad(client, user_id, lang_code)
