import os
import asyncio
import logging
import aiohttp
import redis.asyncio as redis
import json
from bot.logger import setup_logger
from pyrogram import Client
from dotenv import load_dotenv

# Initialize logging
setup_logger()

load_dotenv()

# API Credentials - cast to correct types and handle missing
API_ID = os.environ.get("API_ID")
if API_ID:
    try:
        API_ID = int(API_ID)
    except ValueError:
        API_ID = None
API_HASH = str(os.environ.get("API_HASH", ""))
BOT_TOKEN = os.environ.get("BOT_TOKEN")

# Redis Configuration
REDIS_URL = os.environ.get("REDIS_URL")
redis_client = None
if REDIS_URL:
    try:
        redis_client = redis.from_url(REDIS_URL, decode_responses=True)
    except Exception as e:
        logging.error(f"Failed to initialize Redis client: {e}")
else:
    logging.warning("REDIS_URL environment variable is missing! Caching will be disabled.")

# Bot Configuration
OWNER_ID_RAW = os.environ.get("OWNER_ID")
OWNER_ID = int(OWNER_ID_RAW) if OWNER_ID_RAW and OWNER_ID_RAW.isdigit() else None
OWNER_USERNAME = os.environ.get("OWNER_USERNAME", "OwnerUsername")
SUPPORT_CHAT_LINK = os.environ.get("SUPPORT_CHAT_LINK", "https://t.me/Wolfy004chatbot")
PAYPAL_LINK = os.environ.get("PAYPAL_LINK", "Contact Owner")
UPI_ID = os.environ.get("UPI_ID", "Contact Owner")
APPLE_PAY_ID = os.environ.get("APPLE_PAY_ID", "Contact Owner")
CRYPTO_ADDRESS = os.environ.get("CRYPTO_ADDRESS", "Contact Owner")
CARD_PAYMENT_LINK = os.environ.get("CARD_PAYMENT_LINK", "Contact Owner")
DATABASE_PATH = os.environ.get("DATABASE_PATH", "telegram_bot.db")

async def get_dump_channel_id():
    """Pull dump_channel_id from Redis for instant access"""
    return None #Remove this line for dump channel activation.
    try:
        cached_val = await redis_client.get("setting:dump_channel_id")
        if cached_val:
            data = json.loads(cached_val)
            return data.get('value')
    except Exception:
        pass
    return os.environ.get("DUMP_CHANNEL_ID")

# Performance Settings
MAX_CONCURRENT_DOWNLOADS = 10

active_downloads = set()
cancel_flags = set()
global_download_semaphore = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)
login_states = {}

# Global aiohttp session
shared_session = None

async def get_shared_session():
    global shared_session
    if shared_session is None or shared_session.closed:
        timeout = aiohttp.ClientTimeout(total=30, connect=10)
        shared_session = aiohttp.ClientSession(timeout=timeout)
    return shared_session

# Verification and immediate exit if missing
missing_vars = []
if not API_ID: missing_vars.append("API_ID")
if not API_HASH: missing_vars.append("API_HASH")
if not BOT_TOKEN: missing_vars.append("BOT_TOKEN")

if missing_vars:
    import sys
    print(f"CRITICAL ERROR: Missing environment variables: {', '.join(missing_vars)}")
    sys.exit(1)

# RichAds Configuration
RICHADS_PUBLISHER_ID = os.environ.get("RICHADS_PUBLISHER_ID", "989337")
RICHADS_WIDGET_ID = os.environ.get("RICHADS_WIDGET_ID", "381546")
AD_DAILY_LIMIT_FREE = int(os.environ.get("AD_DAILY_LIMIT_FREE", 50))
AD_DAILY_LIMIT_PREMIUM = int(os.environ.get("AD_DAILY_LIMIT_PREMIUM", 5))
AD_DAILY_LIMIT = AD_DAILY_LIMIT_FREE # Legacy fallback
AD_FOR_PREMIUM = os.environ.get("AD_FOR_PREMIUM", "True").lower() == "true"

# Update client with higher max_concurrent_transmissions
app = Client(
    "bot_session",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    in_memory=True,
    sleep_threshold=120,
    no_updates=False,
    skip_updates=True,
    fetch_replies=0,
    max_concurrent_transmissions=64,
    workers=16
)
