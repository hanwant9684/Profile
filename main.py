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

try:
    # Cap virtual memory at 6 GB — wide enough to prevent false OOM on a 4 GB VPS
    # (virtual address space includes shared libs + mmap and far exceeds physical RAM usage)
    resource.setrlimit(resource.RLIMIT_AS, (6 * 1024 * 1024 * 1024, -1))
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

    os.makedirs("downloads", exist_ok=True)
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
    import bot.transfer 

    # Import all modules to register handlers
    import bot.login
    import bot.handlers
    import bot.admin
    import bot.info

    print("Initializing database...")
    await init_db()

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
    
    print("Starting bot...")
    if app:
        # Check DC while running
        async def check_dc_later():
            await asyncio.sleep(5)
            try:
                me = await app.get_me()
                # app.storage.dc_id() is the REAL auth DC — where all MTProto traffic goes.
                # me.dc_id is only the DC of the bot's profile photo (can differ or be None).
                auth_dc = await app.storage.dc_id()
                dc_locations = {1: "USA/Miami", 2: "Amsterdam", 3: "USA/Miami", 4: "Amsterdam", 5: "Singapore"}
                auth_dc_loc = dc_locations.get(auth_dc, "Unknown")
                photo_dc = me.dc_id
                photo_dc_loc = dc_locations.get(photo_dc, "Unknown") if photo_dc else None
                print(f"✅ Bot auth session: DC{auth_dc} ({auth_dc_loc}) — all API/MTProto traffic goes here")
                if photo_dc:
                    print(f"ℹ️  Bot profile photo: DC{photo_dc} ({photo_dc_loc}) — photo storage only, not the session")
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
    
    # Stop Redis server on exit
    print("🛑 Stopping Redis server...")
    import subprocess
    subprocess.run(["redis-cli", "shutdown"], capture_output=True)

if __name__ == "__main__":
    uvloop.run(main())
