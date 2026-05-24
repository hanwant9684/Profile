import asyncio
import uvloop
import logging
import os
import shutil

asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())

from dotenv import load_dotenv
load_dotenv()

logging.getLogger("hydrogram").setLevel(logging.WARNING)


async def main():
    import subprocess

    try:
        subprocess.run(["redis-cli", "ping"], capture_output=True, check=True)
        print("✅ Redis is already running.")
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("🚀 Starting Redis server...")
        subprocess.Popen(
            ["redis-server", "--bind", "127.0.0.1", "--port", "6379",
             "--daemonize", "yes", "--dir", "/tmp", "--logfile", "/tmp/redis.log"]
        )
        await asyncio.sleep(3)

    os.makedirs("downloads", exist_ok=True)
    for name in os.listdir("downloads"):
        fp = os.path.join("downloads", name)
        try:
            if os.path.isfile(fp) or os.path.islink(fp):
                os.unlink(fp)
            elif os.path.isdir(fp):
                shutil.rmtree(fp)
        except Exception:
            pass

    from bot.config import app
    from bot.database import init_db

    import bot.transfer
    import bot.login
    import bot.handlers
    import bot.batch
    import bot.admin
    import bot.info
    import bot.referral

    print("Initializing database...")
    await init_db()

    try:
        import tgcrypto
        print("✅ TgCrypto is active. Fast transfers enabled.")
    except ImportError:
        print("❌ TgCrypto NOT FOUND. Install tgcrypto for fast transfers.")

    print("Starting cleanup tasks...")

    from bot.cloud_backup import periodic_cloud_backup
    asyncio.create_task(periodic_cloud_backup())

    print("Starting bot...")

    await app.start()
    from hydrogram.methods.utilities.idle import idle
    await idle()
    await app.stop()


if __name__ == "__main__":
    uvloop.run(main())
