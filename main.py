import asyncio
import uvloop 
import logging
import os
import sys
import resource
from dotenv import load_dotenv

# Set event loop policy FIRST
asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())

load_dotenv()

# Optimization for 1.5GB RAM VPS
try:
    # Set soft memory limit to 1.3GB to leave room for system
    resource.setrlimit(resource.RLIMIT_AS, (1300 * 1024 * 1024, -1))
except Exception:
    pass

logging.getLogger("pyrogram").setLevel(logging.WARNING)

async def main():
    if os.path.exists("downloads"):
        import shutil
        for filename in os.listdir("downloads"):
            file_path = os.path.join("downloads", filename)
            try:
                if os.path.isfile(file_path) or os.path.islink(file_path):
                    os.unlink(file_path)
                elif os.path.isdir(file_path):
                    shutil.rmtree(file_path)
                except Exception:
                    pass

    # Pyrogram Client and other imports inside the async main to ensure the loop is active
    from bot.config import app
    from bot.database import init_db
    from bot.cloud_backup import restore_latest_from_cloud, periodic_cloud_backup
    from bot.login import cleanup_expired_logins
    from bot.logger import cleanup_loop
    import bot.transfer 

    # Import all modules to register handlers
    import bot.login
    import bot.handlers
    import bot.admin
    import bot.info

    print("Attempting to restore database from cloud backup...")
    await restore_latest_from_cloud()
    
    print("Initializing database...")
    await init_db()

    # Ensure shared session is initialized
    from bot.config import get_shared_session
    await get_shared_session()

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
        # start_health_check()
        
    asyncio.create_task(cleanup_expired_logins())
    asyncio.create_task(cleanup_loop())
    asyncio.create_task(periodic_cloud_backup(interval_minutes=10))
    
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

        await main_bot()
    else:
        print("Bot app not initialized due to missing config. Exiting.")
    
    # Cleanup global session
    from bot.config import shared_session
    if shared_session and not shared_session.closed:
        await shared_session.close()
        print("Global aiohttp session closed.")

if __name__ == "__main__":
    try:
        uvloop.run(main()) #changed to below line (added new line beloe)
        #asyncio.run(main())
    except KeyboardInterrupt:
        pass
