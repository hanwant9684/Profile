import asyncio
import uvloop
import logging
import os
import shutil

asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())

from dotenv import load_dotenv
load_dotenv()

logging.getLogger("pyrogram").setLevel(logging.WARNING)


async def main():
    import subprocess

    try:
        subprocess.run(["redis-cli", "ping"], capture_output=True, check=True)
        print("✅ Redis is already running.")
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("🚀 Starting Redis server...")
        subprocess.Popen(
            ["redis-server", "--bind", "0.0.0.0", "--port", "6379", "--daemonize", "yes"]
        )
        await asyncio.sleep(2)

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

    from bot.proxy_finder import find_working_proxy, PROXY_CACHE_FILE
    if not os.environ.get("PROXY_URL"):
        working_proxy = find_working_proxy()
        if working_proxy:
            os.environ["PROXY_URL"] = f"socks5://{working_proxy}"

    from bot.config import app
    from bot.database import init_db

    import bot.transfer
    import bot.login
    import bot.handlers
    import bot.batch
    import bot.admin
    import bot.info

    print("Initializing database...")
    await init_db()

    try:
        import tgcrypto
        print("✅ TgCrypto (pyrofork edition) is active. Fast transfers enabled.")
    except ImportError:
        print("❌ TgCrypto NOT FOUND. Install tgcrypto-pyrofork for fast transfers.")

    try:
        import pymediainfo
        print("✅ PyMediaInfo (pyrofork edition) is active.")
    except ImportError:
        print("⚠️  PyMediaInfo NOT FOUND. Install pymediainfo-pyrofork for media metadata support.")

    print("Starting cleanup tasks...")

    from bot.cloud_backup import periodic_cloud_backup
    asyncio.create_task(periodic_cloud_backup())

    print("Starting bot...")

    async def check_dc_later():
        await asyncio.sleep(5)
        try:
            me = await app.get_me()
            auth_dc = await app.storage.dc_id()
            dc_locations = {
                1: "USA/Miami", 2: "Amsterdam", 3: "USA/Miami",
                4: "Amsterdam", 5: "Singapore",
            }
            auth_dc_loc = dc_locations.get(auth_dc, "Unknown")
            photo_dc = me.dc_id
            print(f"✅ Bot auth session: DC{auth_dc} ({auth_dc_loc})")
            if photo_dc:
                photo_dc_loc = dc_locations.get(photo_dc, "Unknown")
                print(f"ℹ️  Bot profile photo: DC{photo_dc} ({photo_dc_loc})")
        except Exception as e:
            logging.debug(f"DC check error: {e}")

    asyncio.create_task(check_dc_later())
    await app.start()
    from pyrogram.methods.utilities.idle import idle
    await idle()
    await app.stop()

    print("🛑 Stopping Redis server...")
    subprocess.run(["redis-cli", "shutdown"], capture_output=True)


if __name__ == "__main__":
    uvloop.run(main())
