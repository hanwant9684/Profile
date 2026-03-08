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

# Optimization for 3GB RAM VPS
try:
    # Set soft memory limit to 2.7GB to leave room for system on 3GB VPS
    # Using 2.7GB (2764.8 MB) to be safer than 2.8GB
    resource.setrlimit(resource.RLIMIT_AS, (2700 * 1024 * 1024, -1))
except Exception:
    pass

logging.getLogger("pyrogram").setLevel(logging.WARNING)

async def main():
    # Start Redis server on Replit if not already running
    import subprocess
    try:
        subprocess.run(["redis-cli", "ping"], capture_output=True, check=True)
        print("✅ Redis is already running.")
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("🚀 Starting Redis server...")
        subprocess.Popen(["redis-server", "--bind", "0.0.0.0", "--port", "6379", "--daemonize", "yes"])
        await asyncio.sleep(2)  # Wait for it to start

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
    from bot.login import cleanup_expired_logins
    from bot.logger import cleanup_loop
    import bot.transfer 

    # Import all modules to register handlers
    import bot.login
    import bot.handlers
    import bot.admin
    import bot.info

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
        
    from bot.cloud_backup import periodic_cloud_backup
    asyncio.create_task(periodic_cloud_backup())
    asyncio.create_task(cleanup_expired_logins())
    asyncio.create_task(cleanup_loop())
    
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

        # FIX: Startup retry logic for TCP connection resilience
        async def start_bot_with_retry(max_retries=5):
            for attempt in range(1, max_retries + 1):
                try:
                    asyncio.create_task(check_dc_later())
                    await app.start()
                    print(f"✅ Bot started successfully on attempt {attempt}")
                    from pyrogram.methods.utilities.idle import idle
                    await idle()
                    await app.stop()
                    return True
                except Exception as e:
                    logging.error(f"Bot start attempt {attempt}/{max_retries} failed: {e}")
                    if attempt < max_retries:
                        wait_time = 5 * (2 ** (attempt - 1))
                        print(f"⏳ Retrying in {wait_time} seconds...")
                        await asyncio.sleep(wait_time)
                    else:
                        logging.critical(f"Bot failed to start after {max_retries} attempts")
                        return False

        await start_bot_with_retry()
    else:
        print("Bot app not initialized due to missing config. Exiting.")
    
    # Cleanup global session
    from bot.config import shared_session
    if shared_session and not shared_session.closed:
        await shared_session.close()
        print("Global aiohttp session closed.")

    # Stop Redis server on exit
    print("🛑 Stopping Redis server...")
    import subprocess
    subprocess.run(["redis-cli", "shutdown"], capture_output=True)

if __name__ == "__main__":
    uvloop.run(main())
