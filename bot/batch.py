import asyncio
import re
import logging

from pyrogram import filters
from pyrogram.errors import FloodWait, FloodPremiumWait

from bot.config import app, batch_cancel_flags, batch_sessions
from bot.database import get_user


# --- Adaptive inter-item pacer ---
#
# Keeps the per-account RPC rate under Telegram's limit during batch operations
# without picking magic numbers. Steady state = 1.5 s/item (~0.7 calls/sec into
# messages.copyMessage / sendMedia, comfortably under the limit). When pyrofork
# raises a FloodWait, we use the exact e.value Telegram returned and also bump
# our future per-item delay so the next items don't crash into the same wall.
# After a streak of clean successes, the delay decays back toward the base.

class BatchPacer:
    BASE_DELAY = 10.0    # seconds between items in steady state
    MAX_DELAY = 60.0    # ceiling for the adaptive delay
    DECAY_AFTER = 5     # consecutive successes before relaxing
    DECAY_FACTOR = 0.7  # delay multiplier per decay step

    def __init__(self):
        self.delay = self.BASE_DELAY
        self.success_streak = 0

    def on_success(self):
        self.success_streak += 1
        if self.success_streak >= self.DECAY_AFTER and self.delay > self.BASE_DELAY:
            self.delay = max(self.BASE_DELAY, self.delay * self.DECAY_FACTOR)
            self.success_streak = 0

    def on_flood(self, wait_seconds: int):
        # Inflate the per-item delay so subsequent iterations stay clear of the limit.
        self.success_streak = 0
        proposed = max(self.delay * 2, wait_seconds / 5 + 1)
        self.delay = min(self.MAX_DELAY, proposed)

    def on_error(self):
        # Non-flood errors don't change the delay but reset the success streak.
        self.success_streak = 0

    async def wait(self):
        await asyncio.sleep(self.delay)


# --- Link parsing helpers (batch-specific) ---

def _parse_batch_link(link: str):
    """
    Parse a start/end link for batch mode.
    Returns a dict with keys: is_private, channel_part, topic_part, msg_id
    or None on failure.
    """
    link = re.sub(r"\?.*$", "", link).rstrip("/")

    m = re.fullmatch(r"https://t\.me/c/(\d+)/(\d+)/(\d+)", link)
    if m:
        return dict(is_private=True, channel_part=m.group(1),
                    topic_part=m.group(2), msg_id=int(m.group(3)))

    m = re.fullmatch(r"https://t\.me/c/(\d+)/(\d+)", link)
    if m:
        return dict(is_private=True, channel_part=m.group(1),
                    topic_part=None, msg_id=int(m.group(2)))

    m = re.fullmatch(r"https://t\.me/([^/+][^/]*)/(\d+)/(\d+)", link)
    if m:
        return dict(is_private=False, channel_part=m.group(1),
                    topic_part=m.group(2), msg_id=int(m.group(3)))

    m = re.fullmatch(r"https://t\.me/([^/+][^/]*)/(\d+)", link)
    if m:
        return dict(is_private=False, channel_part=m.group(1),
                    topic_part=None, msg_id=int(m.group(2)))

    return None


def _build_batch_link(info: dict, msg_id: int) -> str:
    if info["is_private"]:
        if info["topic_part"]:
            return f"https://t.me/c/{info['channel_part']}/{info['topic_part']}/{msg_id}"
        return f"https://t.me/c/{info['channel_part']}/{msg_id}"
    if info["topic_part"]:
        return f"https://t.me/{info['channel_part']}/{info['topic_part']}/{msg_id}"
    return f"https://t.me/{info['channel_part']}/{msg_id}"


# --- Batch download  (/batch) ---

@app.on_message(filters.command("batch") & filters.private)
async def batch_handler(client, message):
    user_id = message.from_user.id
    user = await get_user(user_id)

    if not user or user.get("role", "free") == "free":
        await message.reply(
            "❌ **Batch download is for Premium users only.**\n\n"
            "👉 Use /upgrade to see plans."
        )
        return

    if user_id in batch_sessions:
        await message.reply("⚠️ You already have an active batch. Use /cancelbatch to stop it.")
        return

    parts = message.text.split()
    if len(parts) < 3:
        await message.reply(
            "❌ **Usage:**\n"
            "`/batch start_link end_link` — download from start to end\n"
            "`/batch start_link 50` — download 50 files from start link"
        )
        return

    start_link = parts[1]
    second_arg = parts[2]

    info = _parse_batch_link(start_link)
    if not info:
        await message.reply("❌ Invalid start link.")
        return

    start_id = info["msg_id"]

    if second_arg.isdigit():
        count = int(second_arg)
        if not 1 <= count <= 50:
            await message.reply("⚠️ Count must be between 1 and 50.")
            return
        end_id = start_id + count - 1
    else:
        end_info = _parse_batch_link(second_arg)
        if not end_info:
            await message.reply("❌ Invalid end link.")
            return
        end_id = end_info["msg_id"]
        if start_id > end_id:
            start_id, end_id = end_id, start_id
        count = end_id - start_id + 1
        if count > 50:
            await message.reply("⚠️ Maximum 50 files per batch.")
            return

    status = await message.reply(
        f"🚀 **Batch started** — {count} item(s)\n\n"
        f"✅ Done: 0 | ❌ Skipped: 0\n\n"
        f"/cancel — stop current · /cancelbatch — stop all"
    )

    processed_albums = set()
    done = skipped = 0
    pacer = BatchPacer()
    batch_sessions.add(user_id)

    try:
        for idx, mid in enumerate(range(start_id, end_id + 1), 1):
            if user_id in batch_cancel_flags:
                batch_cancel_flags.discard(user_id)
                await status.edit_text(
                    f"🛑 **Batch cancelled**\n\n"
                    f"✅ Done: {done} | ❌ Skipped: {skipped}"
                )
                return

            link = _build_batch_link(info, mid)
            try:
                await status.edit_text(
                    f"📥 **Batch** — item {idx}/{count}\n\n"
                    f"✅ Done: {done} | ❌ Skipped: {skipped}\n"
                    f"🔗 `{link}`"
                )
            except Exception:
                pass

            try:
                from bot.handlers import download_handler
                result = await download_handler(
                    client, message,
                    link_override=link,
                    status_msg_override=status,
                    processed_albums=processed_albums,
                    skip_quota=True,
                    user_override=user,
                )
                if result is not None:
                    done += 1
                    pacer.on_success()
                else:
                    skipped += 1
                    pacer.on_error()
            except (FloodWait, FloodPremiumWait) as e:
                # Telegram explicitly told us to wait e.value seconds.
                logging.warning(f"Batch hit FloodWait {e.value}s (link={link}); pacing up.")
                pacer.on_flood(e.value)
                await asyncio.sleep(e.value)
                skipped += 1
            except Exception as e:
                logging.error(f"Batch item error (link={link}): {e}")
                pacer.on_error()
                skipped += 1

            if idx < count:
                await pacer.wait()

        try:
            await status.edit_text(
                f"✅ **Batch complete!**\n\n"
                f"📋 Total: {count} | ✅ Done: {done} | ❌ Skipped: {skipped}"
            )
        except Exception:
            pass

    finally:
        batch_sessions.discard(user_id)
        batch_cancel_flags.discard(user_id)


# --- Multi-link download  (/mlinks) ---

@app.on_message(filters.command("mlinks") & filters.private)
async def mlinks_handler(client, message):
    user_id = message.from_user.id
    user = await get_user(user_id)

    if not user or user.get("role", "free") == "free":
        await message.reply(
            "❌ **Multi-link download is for Premium users only.**\n\n"
            "👉 Use /upgrade to see plans."
        )
        return

    if user_id in batch_sessions:
        await message.reply("⚠️ You already have an active batch running. Use /cancelbatch to stop it.")
        return

    links = re.findall(r"https?://t\.me/\S+", message.text)
    links = [l.rstrip(".,;)") for l in links]
    links = [l for l in links if l]

    if not links:
        await message.reply(
            "❌ No valid links found.\n\n"
            "**Usage:** `/mlinks`\n"
            "`https://t.me/channel/123`\n"
            "`https://t.me/channel/456`"
        )
        return

    if len(links) > 50:
        links = links[:50]
        await message.reply("⚠️ More than 50 links provided. Only the first 50 will be processed.")

    count = len(links)
    status = await message.reply(
        f"🚀 **Multi-link** — {count} link(s)\n\n"
        f"✅ Done: 0 | ❌ Skipped: 0\n\n"
        f"/cancel — stop current · /cancelbatch — stop all"
    )

    processed_albums = set()
    done = skipped = 0
    pacer = BatchPacer()
    batch_sessions.add(user_id)

    try:
        for idx, link in enumerate(links, 1):
            if user_id in batch_cancel_flags:
                batch_cancel_flags.discard(user_id)
                await status.edit_text(
                    f"🛑 **Cancelled**\n\n"
                    f"✅ Done: {done} | ❌ Skipped: {skipped}"
                )
                return

            try:
                await status.edit_text(
                    f"📥 **Multi-link** — link {idx}/{count}\n\n"
                    f"✅ Done: {done} | ❌ Skipped: {skipped}\n"
                    f"🔗 `{link}`"
                )
            except Exception:
                pass

            try:
                from bot.handlers import download_handler
                result = await download_handler(
                    client, message,
                    link_override=link,
                    status_msg_override=status,
                    processed_albums=processed_albums,
                    skip_quota=True,
                    user_override=user,
                )
                if result is not None:
                    done += 1
                    pacer.on_success()
                else:
                    skipped += 1
                    pacer.on_error()
            except (FloodWait, FloodPremiumWait) as e:
                logging.warning(f"mlinks hit FloodWait {e.value}s (link={link}); pacing up.")
                pacer.on_flood(e.value)
                await asyncio.sleep(e.value)
                skipped += 1
            except Exception as e:
                logging.error(f"mlinks item error (link={link}): {e}")
                pacer.on_error()
                skipped += 1

            if idx < count:
                await pacer.wait()

        try:
            await status.edit_text(
                f"✅ **Done!**\n\n"
                f"📋 Total: {count} | ✅ Done: {done} | ❌ Skipped: {skipped}"
            )
        except Exception:
            pass

    finally:
        batch_sessions.discard(user_id)
        batch_cancel_flags.discard(user_id)


# --- Cancel batch  (/cancelbatch) ---

@app.on_message(filters.command("cancelbatch") & filters.private)
async def cancelbatch_handler(client, message):
    user_id = message.from_user.id
    if user_id in batch_sessions:
        batch_cancel_flags.add(user_id)
        await message.reply("🛑 Batch cancellation requested. Current item will finish first.")
    else:
        await message.reply("ℹ️ No active batch to cancel.")
