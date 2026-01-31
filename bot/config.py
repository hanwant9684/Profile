import os
import asyncio
import logging
from bot.logger import setup_logger, cleanup_loop
from pyrogram import Client
from dotenv import load_dotenv

# Initialize logging
setup_logger()

load_dotenv()

# API Credentials
API_ID = os.environ.get("API_ID")
API_HASH = os.environ.get("API_HASH")
BOT_TOKEN = os.environ.get("BOT_TOKEN")

# Bot Configuration
OWNER_ID = os.environ.get("OWNER_ID")
OWNER_USERNAME = os.environ.get("OWNER_USERNAME", "OwnerUsername")
SUPPORT_CHAT_LINK = os.environ.get("SUPPORT_CHAT_LINK", "https://t.me/Wolfy004chatbot")
PAYPAL_LINK = os.environ.get("PAYPAL_LINK", "Contact Owner")
UPI_ID = os.environ.get("UPI_ID", "Contact Owner")
APPLE_PAY_ID = os.environ.get("APPLE_PAY_ID", "Contact Owner")
CRYPTO_ADDRESS = os.environ.get("CRYPTO_ADDRESS", "Contact Owner")
CARD_PAYMENT_LINK = os.environ.get("CARD_PAYMENT_LINK", "Contact Owner")
DATABASE_PATH = os.environ.get("DATABASE_PATH", "telegram_bot.db")
DUMP_CHANNEL_ID = os.environ.get("DUMP_CHANNEL_ID")

# Performance Settings
MAX_CONCURRENT_DOWNLOADS = int(os.environ.get("MAX_CONCURRENT_DOWNLOADS", 10)) 
MAX_CONCURRENT_UPLOADS = int(os.environ.get("MAX_CONCURRENT_UPLOADS", 10))

def get_smart_download_workers(file_size):
    """
    Lower worker count for downloads (optimized for server stability).
    """
    if file_size < 10 * 1024 * 1024:
        return 1
    elif file_size < 100 * 1024 * 1024:
        return 2
    else:
        return 2

def get_smart_upload_workers(file_size):
    """
    Higher worker count for uploads (optimized for speed).
    """
    if file_size < 10 * 1024 * 1024:
        return 2
    elif file_size < 100 * 1024 * 1024:
        return 4
    else:
        return 10

def get_smart_chunk_size(file_size):
    """
    Calculates the optimal chunk size based on file size.
    Max allowed by Telegram is 512 KB.
    """
    if file_size < 10 * 1024 * 1024:
        return 128 * 1024
    else:
        return 512 * 1024

# Optimization for 1.5GB RAM VPS and faster execution
try:
    import uvloop
    import asyncio
    asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
except ImportError:
    pass

active_downloads = set()
cancel_flags = set()
global_download_semaphore = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)
global_upload_semaphore = asyncio.Semaphore(MAX_CONCURRENT_UPLOADS)
login_states = {}

# Verification
missing_vars = []
if not API_ID: missing_vars.append("API_ID")
if not API_HASH: missing_vars.append("API_HASH")
if not BOT_TOKEN: missing_vars.append("BOT_TOKEN")

if missing_vars:
    print(f"CRITICAL WARNING: Missing environment variables: {', '.join(missing_vars)}")
    # If missing critical variables, we won't try to start the app object to avoid crash

# RichAds Configuration
RICHADS_PUBLISHER_ID = os.environ.get("RICHADS_PUBLISHER_ID", "792361")
RICHADS_WIDGET_ID = os.environ.get("RICHADS_WIDGET_ID", "351352")
AD_DAILY_LIMIT = int(os.environ.get("AD_DAILY_LIMIT", 5))
AD_FOR_PREMIUM = os.environ.get("AD_FOR_PREMIUM", "False").lower() == "true"

app = Client(
    "bot_session", 
    api_id=API_ID, 
    api_hash=API_HASH, 
    bot_token=BOT_TOKEN,
    in_memory=True,  # Use in-memory storage to avoid SQLite database closed errors
    max_concurrent_transmissions=MAX_CONCURRENT_DOWNLOADS + MAX_CONCURRENT_UPLOADS # Total concurrent streams
)
