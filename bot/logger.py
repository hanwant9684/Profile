import logging
from logging.handlers import RotatingFileHandler

logger = logging.getLogger("bot")

def setup_logger():
    log_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    log_file = "bot_logs.txt"

    file_handler = RotatingFileHandler(log_file, maxBytes=5*1024*1024, backupCount=3)
    file_handler.setFormatter(log_formatter)
    file_handler.setLevel(logging.INFO)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(log_formatter)
    console_handler.setLevel(logging.INFO)

    logging.basicConfig(
        level=logging.INFO,
        handlers=[file_handler, console_handler]
    )

    logging.getLogger("pyrogram.session.session").setLevel(logging.ERROR)
    logging.getLogger("pyrogram.connection.connection").setLevel(logging.ERROR)
