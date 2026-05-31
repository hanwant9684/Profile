import os
import asyncio
import logging
import httpx
from bot.logger import setup_logger
from pyrogram import Client
from dotenv import load_dotenv

setup_logger()

load_dotenv()

API_ID = os.environ.get("API_ID")
if API_ID:
    try:
        API_ID = int(API_ID)
    except ValueError:
        API_ID = None
API_HASH = str(os.environ.get("API_HASH", ""))
BOT_TOKEN = os.environ.get("BOT_TOKEN")

OWNER_ID_RAW = os.environ.get("OWNER_ID")
OWNER_ID = int(OWNER_ID_RAW) if OWNER_ID_RAW and OWNER_ID_RAW.isdigit() else None
SUPPORT_CHAT_LINK = os.environ.get("SUPPORT_CHAT_LINK", "https://t.me/Wolfy004chatbot")


PAYPAL_LINK = os.environ.get("PAYPAL_LINK", "Contact Owner")
UPI_ID = os.environ.get("UPI_ID", "Contact Owner")
APPLE_PAY_ID = os.environ.get("APPLE_PAY_ID", "Contact Owner")
CRYPTO_ADDRESS = os.environ.get("CRYPTO_ADDRESS", "Contact Owner")
CARD_PAYMENT_LINK = os.environ.get("CARD_PAYMENT_LINK", "Contact Owner")

missing_vars = []
if not API_ID:
    missing_vars.append("API_ID")
if not API_HASH:
    missing_vars.append("API_HASH")
if not BOT_TOKEN:
    missing_vars.append("BOT_TOKEN")

if missing_vars:
    import sys
    logging.critical(f"Missing required environment variables: {', '.join(missing_vars)} — bot cannot start")
    sys.exit(1)

MAX_CONCURRENT_DOWNLOADS = 10

active_downloads: set = set()
cancel_flags: set = set()
batch_cancel_flags: set = set()
batch_sessions: set = set()
global_download_semaphore = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)
login_states: dict = {}

# Per-user bot Client cache: { user_id: pyrogram.Client }
# Each user registers their own bot via /setbot; we instantiate and reuse it.
user_bots: dict = {}
# Last-used timestamps for user_bots: { user_id: float (epoch) }
user_bots_last_used: dict = {}

_shared_client: httpx.AsyncClient | None = None


async def get_shared_client() -> httpx.AsyncClient:
    global _shared_client
    if _shared_client is None or _shared_client.is_closed:
        _shared_client = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0))
    return _shared_client


app = Client(
    "bot_session",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    in_memory=True,
    sleep_threshold=30,
    skip_updates=True,
    workers=100,
)
