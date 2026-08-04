import logging
from logging.handlers import RotatingFileHandler

logger = logging.getLogger("bot")


class _WerkzeugProbeFilter(logging.Filter):
    """Suppress 400 errors from TLS/port-scanner probes hitting the plain HTTP webhook port."""
    _NOISE = ("Bad request version", "Bad request syntax", "Bad HTTP/0.9 request")

    def filter(self, record):
        msg = record.getMessage()
        return not any(tok in msg for tok in self._NOISE)


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

    # Pyrogram internal noise
    logging.getLogger("pyrogram.session.session").setLevel(logging.ERROR)
    logging.getLogger("pyrogram.connection.connection").setLevel(logging.ERROR)

    # Telethon reconnect chatter — server-closes every ~90 s and reconnects automatically;
    # these are INFO/WARNING noise that fills the log with 1000+ identical lines.
    logging.getLogger("telethon.network.connection.connection").setLevel(logging.ERROR)
    logging.getLogger("telethon.network.mtprotosender").setLevel(logging.ERROR)
    logging.getLogger("telethon.client.downloads").setLevel(logging.WARNING)

    # Werkzeug — suppress TLS/port-scanner probes hitting the plain HTTP webhook port
    logging.getLogger("werkzeug").addFilter(_WerkzeugProbeFilter())
