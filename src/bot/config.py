import os
import datetime
from hydrogram import Client
from hydrogram.types import InlineKeyboardMarkup, InlineKeyboardButton

# Env Vars
API_ID = os.environ.get("API_ID")
API_HASH = os.environ.get("API_HASH")
BOT_TOKEN = os.environ.get("BOT_TOKEN")
OWNER_ID = os.environ.get("OWNER_ID")
DATABASE_URL = os.environ.get("DATABASE_URL")
DUMP_CHANNEL_ID = os.environ.get("DUMP_CHANNEL_ID")

# Global Concurrency
MAX_CONCURRENT_DOWNLOADS = 5
import asyncio
active_downloads = set() # user_ids
global_download_semaphore = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)
login_states = {}

if not API_ID or not API_HASH or not BOT_TOKEN:
    print("WARNING: API_ID, API_HASH, or BOT_TOKEN not set.")

app = Client("bot_session", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)
