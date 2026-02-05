import logging
from logging.handlers import RotatingFileHandler
import os
import gc
import psutil
import asyncio

logger = logging.getLogger("bot")

def setup_logger():
    log_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    log_file = "bot_logs.txt"

    file_handler = RotatingFileHandler(log_file, maxBytes=1*1024*1024, backupCount=1)
    file_handler.setFormatter(log_formatter)
    file_handler.setLevel(logging.INFO)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(log_formatter)
    console_handler.setLevel(logging.INFO)

    logging.basicConfig(
        level=logging.INFO,
        handlers=[file_handler, console_handler]
    )

    # Suppress specific noisy logs
    logging.getLogger("pyrogram.session.session").setLevel(logging.ERROR)
    logging.getLogger("pyrogram.connection.connection").setLevel(logging.ERROR)

async def cleanup_loop():
    """Periodic RAM and garbage collection cleanup"""
    while True:
        await asyncio.sleep(1800) # 30 minutes
        gc.collect()
        process = psutil.Process(os.getpid())
        mem_info = process.memory_info()
        logging.info(f"Scheduled Cleanup: GC collected. Current RSS: {mem_info.rss / 1024 / 1024:.2f} MB")
