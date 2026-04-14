import os
import asyncio
import logging
import aiohttp
import redis.asyncio as redis
import json
from bot.logger import setup_logger, cleanup_loop
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
MAX_CONCURRENT_UPLOADS = int(os.environ.get("MAX_CONCURRENT_UPLOADS", 10))

def get_smart_chunk_size(file_size):
    if file_size < 10 * 1024 * 1024:      # < 10MB
        return 128 * 1024                # 128KB
    elif file_size < 100 * 1024 * 1024:  # 10-100MB
        return 512 * 1024                # 512KB
    else:                                # > 100MB
        return 1024 * 1024               # 1024KB (1MB)

def get_smart_download_workers(file_size):
    if file_size < 100 * 1024 * 1024:
        return 1
    else:
        return 4

def get_smart_upload_workers(file_size):
    return 4

active_downloads = set()
cancel_flags = set()
global_download_semaphore = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)
global_upload_semaphore = asyncio.Semaphore(MAX_CONCURRENT_UPLOADS)
login_states = {}

# Global aiohttp session
shared_session = None

async def get_shared_session():
    global shared_session
    if shared_session is None or shared_session.closed:
        shared_session = aiohttp.ClientSession()
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
AD_DAILY_LIMIT = int(os.environ.get("AD_DAILY_LIMIT", 500))
AD_FOR_PREMIUM = os.environ.get("AD_FOR_PREMIUM", "False").lower() == "true"

# Update client with higher max_concurrent_transmissions
app = Client(
    "bot_session", 
    api_id=API_ID, 
    api_hash=API_HASH, 
    bot_token=BOT_TOKEN,
    in_memory=True,
    sleep_threshold=60,
    no_updates=False,
    max_concurrent_transmissions=10,
    workers=10
)
