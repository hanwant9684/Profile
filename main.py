import asyncio
import logging
import os
try:
    import uvloop
    uvloop.install()
except ImportError:
    pass
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
    from bot.web import start_health_check
    
    if os.environ.get("RUN_WEB_SERVER", "False").lower() == "true":
        print("Starting web server for health checks...")
        start_health_check()
        
    asyncio.get_event_loop().create_task(cleanup_expired_logins())
    print("Starting bot...")
    if app:
        app.run()
    else:
        print("Bot app not initialized due to missing config. Exiting.")
