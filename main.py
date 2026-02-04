import asyncio
import logging
import os
import sys
import resource
from dotenv import load_dotenv

# Initialize uvloop and event loop immediately before any other imports
import asyncio
try:
    import uvloop
    asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
except ImportError:
    pass

# Ensure an event loop exists for Pyrogram's sync initialization
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

load_dotenv()

from bot.config import app
from bot.database import init_db
from bot.cloud_backup import restore_latest_from_cloud, periodic_cloud_backup
from bot.login import cleanup_expired_logins
from bot.logger import cleanup_loop
import bot.transfer # Ensure transfer is available

# Optimization for 1.5GB RAM VPS
try:
    # Set soft memory limit to 1.3GB to leave room for system
    resource.setrlimit(resource.RLIMIT_AS, (1300 * 1024 * 1024, -1))
except Exception:
    pass

logging.getLogger("pyrogram").setLevel(logging.WARNING)

# Import all modules to register handlers
import bot.login
import bot.handlers
import bot.admin
import bot.info

if __name__ == "__main__":
    print("Attempting to restore database from cloud backup...")
    asyncio.get_event_loop().run_until_complete(restore_latest_from_cloud())
    
    print("Initializing database...")
    init_db()

    # Check for TgCrypto and debug crypto speed
    try:
        import tgcrypto
        print(f"✅ TgCrypto is active. Fast transfers enabled.")
    except ImportError:
        print("❌ TgCrypto NOT FOUND. Bot will be slow.")
    except Exception as e:
        print(f"❌ TgCrypto Debug Error: {e}")

    print("Starting cleanup tasks...")
    if os.environ.get("RUN_WEB_SERVER", "False").lower() == "true":
        print("Starting web server for health checks...")
        start_health_check()
        
    loop = asyncio.get_event_loop()
    loop.create_task(cleanup_expired_logins())
    loop.create_task(cleanup_loop())
    loop.create_task(periodic_cloud_backup(interval_minutes=10))
    
    print("Starting bot...")
    if app:
        # Check DC while running
        async def check_dc_later():
            await asyncio.sleep(5)
            try:
                me = await app.get_me()
                print(f"✅ Bot is running on DC {me.dc_id}")
            except Exception as e:
                logging.debug(f"DC Check Error: {e}")

        async def main_bot():
            asyncio.create_task(check_dc_later())
            await app.start()
            # This is to keep the event loop running while pyrogram's idle() handles signals
            from pyrogram.methods.utilities.idle import idle
            await idle()
            await app.stop()

        try:
            loop.run_until_complete(main_bot())
        except KeyboardInterrupt:
            pass
    else:
        print("Bot app not initialized due to missing config. Exiting.")
