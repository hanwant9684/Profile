import os
import asyncio
import logging
from pyrogram.types import LinkPreviewOptions

_RAW = os.environ.get("LOG_CHANNEL_ID", "").strip()
LOG_CHANNEL_ID = (
    int(_RAW) if _RAW.lstrip("-").isdigit() else (_RAW or None)
)

_ICONS = {
    ("public",    True):  "✅",
    ("private",   True):  "🔒",
    ("story",     True):  "📖",
    ("bot_dm",    True):  "🤖",
    ("bot_start", True):  "🤖",
}
_LABELS = {
    "public":    "Public",
    "private":   "Private",
    "story":     "Story",
    "bot_dm":    "Bot DM",
    "bot_start": "Bot Start",
}


async def _post(text: str):
    if not LOG_CHANNEL_ID:
        return
    try:
        from bot.config import app
        await app.send_message(
            LOG_CHANNEL_ID,
            text,
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
    except Exception as exc:
        logging.debug(f"Log channel send error: {exc}")


def log_download(user_id: int, username, link: str, link_type: str, success: bool):
    """Fire-and-forget: posts one message to the log channel. Never raises, never blocks."""
    if not LOG_CHANNEL_ID:
        return
    icon = _ICONS.get((link_type, success), "❌")
    label = _LABELS.get(link_type, link_type.replace("_", " ").capitalize())
    user_str = f"@{username}" if username else f"#{user_id}"
    status_str = "done" if success else "failed"
    text = (
        f"{icon} **{label}** · {status_str}\n"
        f"👤 {user_str} · `{user_id}`\n"
        f"🔗 {link[:120]}"
    )
    try:
        asyncio.create_task(_post(text))
    except RuntimeError:
        pass
