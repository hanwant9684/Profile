import os
import asyncio
from hydrogram import Client

API_ID = os.environ.get("API_ID")
API_HASH = os.environ.get("API_HASH")
BOT_TOKEN = os.environ.get("BOT_TOKEN")
OWNER_ID = os.environ.get("OWNER_ID")
MONGO_DB = os.environ.get("MONGO_DB")
DUMP_CHANNEL_ID = os.environ.get("DUMP_CHANNEL_ID")

MAX_CONCURRENT_DOWNLOADS = 5
active_downloads = set()
global_download_semaphore = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)
login_states = {}

if not API_ID or not API_HASH or not BOT_TOKEN:
    print("WARNING: API_ID, API_HASH, or BOT_TOKEN not set.")

if not MONGO_DB:
    print("WARNING: MONGO_DB not set.")

app = Client("bot_session", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)
