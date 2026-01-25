import asyncio
import logging
from dotenv import load_dotenv

load_dotenv()

from bot.config import app
from bot.database import init_db

logging.getLogger("pyrogram").setLevel(logging.WARNING)

# Import all modules to register handlers
import bot.login
import bot.handlers
import bot.admin
import bot.info

if __name__ == "__main__":
    print("Initializing database...")
    init_db()
    print("Starting cleanup task...")
    from bot.login import cleanup_expired_logins
    asyncio.get_event_loop().create_task(cleanup_expired_logins())
    print("Starting bot...")
    app.run()
