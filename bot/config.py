import os
import asyncio
from pyrogram import Client

# API Credentials
API_ID = os.environ.get("API_ID")
API_HASH = os.environ.get("API_HASH")
BOT_TOKEN = os.environ.get("BOT_TOKEN")

# Bot Configuration
OWNER_ID = os.environ.get("OWNER_ID")
MONGO_DB = os.environ.get("MONGO_DB") or os.environ.get("MONGODB")
DUMP_CHANNEL_ID = os.environ.get("DUMP_CHANNEL_ID")

# Performance Settings
MAX_CONCURRENT_DOWNLOADS = int(os.environ.get("MAX_CONCURRENT_DOWNLOADS", 5))
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

app = Client("bot_session", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)
