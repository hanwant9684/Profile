import asyncio
import re
import logging

from pyrogram import filters
from bot.config import app, batch_cancel_flags, batch_sessions, cancel_flags
from bot.database import get_user
from bot.transfer import get_user_bot
from bot.link_utils import TG_LINK_HOST_RE, normalize_telegram_link


# Link parsing helpers
def _parse_batch_link(link: str):
    link = normalize_telegram_link(re.sub(r"\?.*$", "", link).rstrip("/"))

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


# /batch — download a range of messages
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

    try:
        user_bot = await get_user_bot(user_id)
    except Exception:
        await message.reply(
            "❌ **Your upload bot token is invalid or expired.**\n\n"
            "Use /rembot to clear it, then /setbot to register a new one."
        )
        return
    if user_bot is None:
        await message.reply(
            "❌ **Upload bot not set up.**\n\n"
            "Batch download requires your own upload bot.\n"
            "Use /setbot to register one before running /batch.\n\n"
            "1. Open @BotFather → `/newbot`\n"
            "2. Copy the token\n"
            "3. Run /setbot and send the token when prompted\n"
            "4. Press **Start** on your bot"
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
        if not 1 <= count <= 200:
            await message.reply("⚠️ Count must be between 1 and 50.")
            return
        end_id = start_id + count - 1
    else:
        end_info = _parse_batch_link(second_arg)
        if not end_info:
            await message.reply("❌ Invalid end link.")
            return
        if (end_info["channel_part"] != info["channel_part"] or
                end_info["topic_part"] != info["topic_part"] or
                end_info["is_private"] != info["is_private"]):
            await message.reply("❌ Start and end links must be from the same channel and topic.")
            return
        end_id = end_info["msg_id"]
        if start_id > end_id:
            start_id, end_id = end_id, start_id
        count = end_id - start_id + 1
        if count > 200:
            await message.reply("⚠️ Maximum 50 files per batch.")
            return

    status = await message.reply(
        f"🚀 **Batch started** — {count} item(s)\n\n"
        f"✅ Done: 0 | ❌ Skipped: 0\n\n"
        f"/cancel — stop current · /cancelbatch — stop all"
    )

    processed_albums = set()
    done = skipped = 0
    batch_sessions.add(user_id)

    ids = list(range(start_id, end_id + 1))
    try:
        for idx, mid in enumerate(ids, 1):
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
                if result == "SESSION_INVALID":
                    try:
                        await status.edit_text(
                            f"🛑 **Batch stopped — session expired**\n\n"
                            f"✅ Done: {done} | ❌ Skipped: {skipped}\n\n"
                            f"Your Telegram session expired or was revoked.\n"
                            f"Please /login again, then restart the batch."
                        )
                    except Exception:
                        pass
                    return
                elif result == "ALBUM_DEDUP":
                    pass
                elif result is not None:
                    done += 1
                    if mid != ids[-1]:
                        for _ in range(10):
                            if user_id in batch_cancel_flags:
                                break
                            await asyncio.sleep(1)
                else:
                    skipped += 1
                    if mid != ids[-1]:
                        for _ in range(10):
                            if user_id in batch_cancel_flags:
                                break
                            await asyncio.sleep(1)
            except Exception as e:
                logging.error(f"Batch item error (link={link}): {e}")
                skipped += 1
                if mid != ids[-1]:
                    for _ in range(10):
                        if user_id in batch_cancel_flags:
                            break
                        await asyncio.sleep(1)

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


# /mlinks — download multiple individual links
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

    try:
        user_bot = await get_user_bot(user_id)
    except Exception:
        await message.reply(
            "❌ **Your upload bot token is invalid or expired.**\n\n"
            "Use /rembot to clear it, then /setbot to register a new one."
        )
        return
    if user_bot is None:
        await message.reply(
            "❌ **Upload bot not set up.**\n\n"
            "Multi-link download requires your own upload bot.\n"
            "Use /setbot to register one before running /mlinks.\n\n"
            "1. Open @BotFather → `/newbot`\n"
            "2. Copy the token\n"
            "3. Run /setbot and send the token when prompted\n"
            "4. Press **Start** on your bot"
        )
        return

    if user_id in batch_sessions:
        await message.reply("⚠️ You already have an active batch running. Use /cancelbatch to stop it.")
        return

    links = re.findall(TG_LINK_HOST_RE + r"\S+", message.text)
    links = [normalize_telegram_link(l.rstrip(".,;)")) for l in links]
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
                if result == "SESSION_INVALID":
                    try:
                        await status.edit_text(
                            f"🛑 **Stopped — session expired**\n\n"
                            f"✅ Done: {done} | ❌ Skipped: {skipped}\n\n"
                            f"Your Telegram session expired or was revoked.\n"
                            f"Please /login again, then restart."
                        )
                    except Exception:
                        pass
                    return
                elif result == "ALBUM_DEDUP":
                    pass
                elif result is not None:
                    done += 1
                    if idx < count:
                        for _ in range(10):
                            if user_id in batch_cancel_flags:
                                break
                            await asyncio.sleep(1)
                else:
                    skipped += 1
                    if idx < count:
                        for _ in range(10):
                            if user_id in batch_cancel_flags:
                                break
                            await asyncio.sleep(1)
            except Exception as e:
                logging.error(f"mlinks item error (link={link}): {e}")
                skipped += 1
                if idx < count:
                    for _ in range(10):
                        if user_id in batch_cancel_flags:
                            break
                        await asyncio.sleep(1)

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


# /cancelbatch
@app.on_message(filters.command("cancelbatch") & filters.private)
async def cancelbatch_handler(client, message):
    user_id = message.from_user.id
    if user_id in batch_sessions:
        batch_cancel_flags.add(user_id)
        cancel_flags.add(user_id)
        await message.reply("🛑 Cancelling batch...")
    else:
        await message.reply("ℹ️ No active batch to cancel.")
