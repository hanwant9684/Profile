import asyncio
import uvloop
import logging
import os
import shutil

asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())

from dotenv import load_dotenv
load_dotenv()

logging.getLogger("pyrogram").setLevel(logging.WARNING)
logging.getLogger("telethon").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("werkzeug").setLevel(logging.WARNING)


async def main():
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
    import bot.tlogin
    import bot.batch
    import bot.admin
    import bot.info
    import bot.caption_filter
    import bot.payment_handlers  # auto-payment inline-keyboard handlers

    print("Initializing database...")
    await init_db()

    try:
        import tgcrypto
        print("✅ TgCrypto is active. Fast transfers enabled.")
    except ImportError:
        print("❌ TgCrypto NOT FOUND. Install tgcrypto for fast transfers.")

    print("Starting cleanup tasks...")

    from bot.cloud_backup import periodic_cloud_backup
    from bot.database import periodic_premium_sweep
    asyncio.create_task(periodic_cloud_backup())
    asyncio.create_task(periodic_premium_sweep())

    print("Starting bot...")
    await app.start()

    # Auto-detect bot username so all t.me/ links are always correct
    import bot.config as _cfg
    try:
        _me = await app.get_me()
        if _me.username:
            _cfg.BOT_USERNAME = _me.username
            print(f"✅ Bot username detected: @{_me.username}")
        # If SUPPORT_CHAT_LINK not set, default to the bot itself
        if not _cfg.SUPPORT_CHAT_LINK:
            _cfg.SUPPORT_CHAT_LINK = f"https://t.me/{_cfg.BOT_USERNAME}"
    except Exception as _e:
        print(f"⚠️ Could not detect bot username: {_e}")

    # Start the webhook server on a background thread (needs the bot running first)
    from bot.webhook_server import start_webhook_thread
    wh_thread = start_webhook_thread(asyncio.get_event_loop(), app)
    print(f"✅ Webhook server started (thread: {wh_thread.name})")

    from pyrogram.methods.utilities.idle import idle
    await idle()
    await app.stop()


if __name__ == "__main__":
    uvloop.run(main())
