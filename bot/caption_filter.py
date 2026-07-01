import json
import logging
from pyrogram import filters
from bot.config import app
from bot.database import get_user, save_caption_filters, save_caption_append

logger = logging.getLogger(__name__)

MAX_FILTERS = 20
MAX_FILTER_LEN = 100


def _parse_filters(raw) -> list:
    if not raw:
        return []
    try:
        result = json.loads(raw) if isinstance(raw, str) else (raw or [])
        return result if isinstance(result, list) else []
    except Exception:
        return []


# ---------------------------------------------------------------------------
# /caprem — remove words/phrases from captions
# ---------------------------------------------------------------------------

@app.on_message(filters.command("caprem") & filters.private)
async def caprem_command(client, message):
    user_id = message.from_user.id
    user = await get_user(user_id)

    if not user or user.get("role", "free") not in ("premium", "admin", "owner"):
        await message.reply(
            "❌ **Caption Remove is a Premium feature.**\n\n"
            "👉 /upgrade to see plans."
        )
        return

    parts = message.text.split(maxsplit=2)
    subcommand = parts[1].lower() if len(parts) > 1 else None
    arg = parts[2].strip() if len(parts) > 2 else None

    current = _parse_filters(user.get("caption_filters"))

    if subcommand is None or subcommand == "list":
        await _show_caprem(message, current)
        return

    # /caprem set <text>
    if subcommand == "set":
        if not arg:
            await message.reply("Usage: `/caprem set <word or phrase>`")
            return
        if len(arg) > MAX_FILTER_LEN:
            await message.reply(f"❌ Too long. Max {MAX_FILTER_LEN} characters per entry.")
            return
        if len(current) >= MAX_FILTERS:
            await message.reply(f"❌ Maximum {MAX_FILTERS} entries allowed. Remove some with `/caprem del <text>` first.")
            return
        if arg.lower() in [f.lower() for f in current]:
            await message.reply(f"ℹ️ `{arg}` is already in your list.")
            return
        current.append(arg)
        await save_caption_filters(user_id, current)
        await message.reply(
            f"✅ `{arg}` will now be removed from captions.\n\n"
            f"Total: **{len(current)}/{MAX_FILTERS}**"
        )
        return

    # /caprem del <text>
    if subcommand == "del":
        if not arg:
            await message.reply("Usage: `/caprem del <word or phrase>`")
            return
        lower_arg = arg.lower()
        match = next((f for f in current if f.lower() == lower_arg), None)
        if match is None:
            await message.reply(f"❌ `{arg}` not found in your list.")
            return
        current.remove(match)
        await save_caption_filters(user_id, current)
        await message.reply(
            f"✅ Removed `{arg}`.\n\n"
            f"Remaining: **{len(current)}/{MAX_FILTERS}**"
        )
        return

    # /caprem reset
    if subcommand == "reset":
        if not current:
            await message.reply("ℹ️ Nothing to clear — list is already empty.")
            return
        await save_caption_filters(user_id, [])
        await message.reply("✅ All caption remove entries cleared.")
        return

    await message.reply(
        "❓ Unknown subcommand.\n\n"
        "**Usage:**\n"
        "`/caprem` — show current list\n"
        "`/caprem set <text>` — add a word or phrase to remove from captions\n"
        "`/caprem del <text>` — delete a specific entry\n"
        "`/caprem reset` — clear all entries"
    )


async def _show_caprem(message, current: list):
    if not current:
        await message.reply(
            "🗑 **Caption Remove**\n\n"
            "No entries set — captions are sent as-is.\n\n"
            "**Commands:**\n"
            "`/caprem set <text>` — add a word or phrase to remove\n"
            "`/caprem del <text>` — delete a specific entry\n"
            "`/caprem reset` — clear all\n\n"
            f"Limit: {MAX_FILTERS} entries, {MAX_FILTER_LEN} chars each."
        )
        return

    lines = "\n".join(f"`{i+1}.` `{f}`" for i, f in enumerate(current))
    await message.reply(
        f"🗑 **Caption Remove** — {len(current)}/{MAX_FILTERS} entries\n\n"
        f"{lines}\n\n"
        "**Commands:**\n"
        "`/caprem set <text>` — add entry\n"
        "`/caprem del <text>` — delete entry\n"
        "`/caprem reset` — clear all"
    )


# ---------------------------------------------------------------------------
# /capadd — add text to the end of captions
# ---------------------------------------------------------------------------

MAX_APPEND_LEN = 500


@app.on_message(filters.command("capadd") & filters.private)
async def capadd_command(client, message):
    try:
        user_id = message.from_user.id
        user = await get_user(user_id)

        if not user or user.get("role", "free") not in ("premium", "admin", "owner"):
            await message.reply(
                "❌ **Caption Add is a Premium feature.**\n\n"
                "👉 /upgrade to see plans."
            )
            return

        text = message.text or ""
        parts = text.split(maxsplit=2)
        subcommand = parts[1].lower() if len(parts) > 1 else None
        arg = parts[2].strip() if len(parts) > 2 else None

        current_append = (user.get("caption_append") or "").strip()

        if subcommand is None or subcommand == "show":
            await _show_capadd(message, current_append)
            return

        # /capadd set <text>
        if subcommand == "set":
            if not arg:
                await message.reply(
                    "Usage: `/capadd set <text>`\n\n"
                    "Use `\\n` to insert a line break.\n"
                    "Example: `/capadd set @MyChannel`"
                )
                return
            arg = arg.replace("\\n", "\n")
            if len(arg) > MAX_APPEND_LEN:
                await message.reply(f"❌ Too long. Max {MAX_APPEND_LEN} characters.")
                return
            await save_caption_append(user_id, arg)
            preview = arg.replace("\n", "↵")
            await message.reply(
                f"✅ Caption add text saved:\n\n`{preview}`\n\n"
                "This will be added to the end of every caption."
            )
            return

        # /capadd del
        if subcommand == "del":
            if not current_append:
                await message.reply("ℹ️ No caption add text is saved.")
                return
            await save_caption_append(user_id, "")
            await message.reply("✅ Caption add text deleted.")
            return

        await message.reply(
            "❓ Unknown subcommand.\n\n"
            "**Usage:**\n"
            "`/capadd` — show current add text\n"
            "`/capadd set <text>` — set text to add at the end of every caption\n"
            "`/capadd del` — delete the add text"
        )
    except Exception:
        await message.reply("❌ An error occurred. Please try again.")


async def _show_capadd(message, current_append: str):
    if not current_append:
        await message.reply(
            "➕ **Caption Add**\n\n"
            "No text set — captions are sent as-is.\n\n"
            "**Commands:**\n"
            "`/capadd set <text>` — add text to the end of every caption\n"
            "`/capadd del` — delete it\n\n"
            "Tip: use `\\n` in your text to add a new line.\n"
            f"Limit: {MAX_APPEND_LEN} characters."
        )
        return

    preview = current_append.replace("\n", "↵")
    await message.reply(
        f"➕ **Caption Add** — active\n\n"
        f"`{preview}`\n\n"
        "**Commands:**\n"
        "`/capadd set <text>` — change it\n"
        "`/capadd del` — delete it"
    )
