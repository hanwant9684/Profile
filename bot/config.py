import os
import asyncio
from pyrogram import Client
from dotenv import load_dotenv

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
MONGO_DB = os.environ.get("MONGO_DB") or os.environ.get("MONGODB")
DUMP_CHANNEL_ID = os.environ.get("DUMP_CHANNEL_ID")

# Performance Settings
MAX_CONCURRENT_DOWNLOADS = int(os.environ.get("MAX_CONCURRENT_DOWNLOADS", 5))
MEMORY_BUFFER_LIMIT = 20 * 1024 * 1024  # 10MB
CHUNK_SIZE = 512 * 1024 # 512KB optimized chunk size
active_downloads = set()
cancel_flags = set()
global_download_semaphore = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)
login_states = {}

# Verification
missing_vars = []
if not API_ID: missing_vars.append("API_ID")
if not API_HASH: missing_vars.append("API_HASH")
if not BOT_TOKEN: missing_vars.append("BOT_TOKEN")
if not MONGO_DB: missing_vars.append("MONGO_DB")

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
    workers=10
)
