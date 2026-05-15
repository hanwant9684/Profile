import asyncio
import logging
from pyrogram import filters
from bot.config import app, OWNER_ID, active_downloads, MAX_CONCURRENT_DOWNLOADS
from bot.handlers import update_status
from bot.database import set_user_role, ban_user, update_setting, get_setting, get_all_users, get_user_count, get_user, iter_user_ids

@app.on_message(filters.command("stats") & filters.private)
async def stats(client, message):
    user = await get_user(message.from_user.id)
    if (OWNER_ID is None or int(message.from_user.id) != int(OWNER_ID)) and (not user or user.get("role") != "admin"):
        return
    total_users = await get_user_count()
    await message.reply(
        f"📊 **Bot Statistics**\n\n"
        f"👥 Total Users: `{total_users}`\n"
        f"⚡ Active Downloads: `{len(active_downloads)}/{MAX_CONCURRENT_DOWNLOADS}`"
    )

@app.on_message(filters.command("killall") & filters.private)
async def kill_all_processes(client, message):
    user = await get_user(message.from_user.id)
    if (OWNER_ID is None or int(message.from_user.id) != int(OWNER_ID)) and (not user or user.get("role") != "admin"):
        return
    from bot.config import cancel_flags
    if not active_downloads:
        await message.reply("⚠️ No active downloads to kill.")
        return
    count = len(active_downloads)
    for uid in list(active_downloads):
        cancel_flags.add(uid)
    active_downloads.clear()
    await message.reply(f"✅ Killed all `{count}` active processes and sent cancellation signals.")

@app.on_message(filters.command("setrole") & filters.private)
async def setrole(client, message):
    user = await get_user(message.from_user.id)
    if (OWNER_ID is None or int(message.from_user.id) != int(OWNER_ID)) and (not user or user.get("role") != "admin"):
        await message.reply("⛔ Authorized personnel only.")
        return
    try:
        parts = message.text.split()
        if len(parts) < 3:
            raise ValueError("Not enough arguments")
        target_id = int(parts[1]) if parts[1].strip("-").isdigit() else parts[1]
        new_role = parts[2]
        duration = parts[3] if len(parts) > 3 else None
        if new_role not in ['free', 'premium', 'admin', 'owner']:
            await message.reply("Invalid role. Use: free, premium, admin, owner")
            return
        await set_user_role(target_id, new_role, duration)
        resp = f"✅ User `{target_id}` role updated to **{new_role}**."
        if duration and new_role == 'premium':
            resp += f" (Expires in {duration} days)"
            try:
                await client.send_message(
                    target_id,
                    "🎉 **Congratulations!**\n\n"
                    f"Your account has been upgraded to **Premium** for {duration} days.\n"
                    "You now have access to all premium features! 🚀"
                )
                resp += "\n\n🔔 User has been notified."
            except Exception as e:
                resp += f"\n\n⚠️ Failed to notify user: {e}"
        await message.reply(resp)
    except ValueError:
        await message.reply("Usage: `/setrole <user_id> <role> [days]`")
    except Exception as e:
        await message.reply(f"Error: {e}")

@app.on_message(filters.command("ban") & filters.private)
async def ban(client, message):
    user = await get_user(message.from_user.id)
    if (OWNER_ID is None or int(message.from_user.id) != int(OWNER_ID)) and (not user or user.get("role") != "admin"):
        return
    try:
        target_id = message.text.split()[1]
        await ban_user(target_id, True)
        await message.reply(f"🚫 User `{target_id}` has been **BANNED**.")
    except Exception:
        await message.reply("Usage: `/ban <user_id>`")

@app.on_message(filters.command("unban") & filters.private)
async def unban(client, message):
    user = await get_user(message.from_user.id)
    if (OWNER_ID is None or int(message.from_user.id) != int(OWNER_ID)) and (not user or user.get("role") != "admin"):
        return
    try:
        target_id = message.text.split()[1]
        await ban_user(target_id, False)
        await message.reply(f"✅ User `{target_id}` has been **UNBANNED**.")
    except Exception:
        await message.reply("Usage: `/unban <user_id>`")

@app.on_message(filters.command("set_force_sub") & filters.private)
async def set_force_sub(client, message):
    user = await get_user(message.from_user.id)
    if (OWNER_ID is None or int(message.from_user.id) != int(OWNER_ID)) and (not user or user.get("role") != "admin"):
        return
    try:
        channel = message.text.split()[1]
        await update_setting("force_sub_channel", channel)
        await message.reply(f"✅ Force Sub channel set to: {channel}")
    except Exception:
        await message.reply("Usage: `/set_force_sub @channel`")

@app.on_message(filters.command("settings") & filters.private)
async def view_settings(client, message):
    user = await get_user(message.from_user.id)
    if (OWNER_ID is None or int(message.from_user.id) != int(OWNER_ID)) and (not user or user.get("role") != "admin"):
        return

    from bot.database import DAILY_LIMIT, MONTHLY_LIMIT
    from bot.handlers import user_clients

    fs = await get_setting("force_sub_channel")
    mm = await get_setting("maintenance_mode")

    fs_val = (fs.get("value") if fs else None) or "Not Set"
    mm_on = (mm.get("value") if mm else None) == "on"

    active_dl = len(active_downloads)
    active_sess = len(user_clients)

    mm_display = "🔴 ON  — bot paused for users" if mm_on else "🟢 OFF — bot running normally"
    fs_display = f"`{fs_val}`" if fs_val != "Not Set" else "Not Set"

    text = (
        "╔══════════════════════════╗\n"
        "║      ⚙️  Bot Settings      ║\n"
        "╚══════════════════════════╝\n\n"

        "━━━  Access Control  ━━━\n"
        f"📢 Force Sub:     {fs_display}\n"
        f"🔧 Maintenance:  {mm_display}\n\n"

        "━━━  Quotas (free users)  ━━━\n"
        f"📅 Daily limit:    `{DAILY_LIMIT}` downloads / day\n"
        f"📆 Monthly limit:  `{MONTHLY_LIMIT}` downloads / month\n\n"

        "━━━  Live Stats  ━━━\n"
        f"⬇️ Active downloads:  `{active_dl}`\n"
        f"👤 Open sessions:     `{active_sess}`\n\n"

        "━━━  Commands  ━━━\n"
        "`/set_force_sub @channel` — set force sub\n"
        "`/set_maintenance on|off` — toggle maintenance"
    )
    await message.reply(text)


@app.on_message(filters.command("set_maintenance") & filters.private)
async def set_maintenance(client, message):
    user = await get_user(message.from_user.id)
    if (OWNER_ID is None or int(message.from_user.id) != int(OWNER_ID)) and (not user or user.get("role") != "admin"):
        return
    parts = message.text.split()
    if len(parts) < 2 or parts[1].lower() not in ("on", "off"):
        await message.reply("Usage: `/set_maintenance on` or `/set_maintenance off`")
        return
    state = parts[1].lower()
    await update_setting("maintenance_mode", state)
    if state == "on":
        await message.reply(
            "🔴 **Maintenance mode ON.**\n\n"
            "All regular users are now blocked with a maintenance notice.\n"
            "Admins and the owner can still use the bot normally."
        )
    else:
        await message.reply("🟢 **Maintenance mode OFF.** Bot is open to all users again.")

@app.on_message(filters.command("broadcast") & filters.private)
async def broadcast(client, message):
    user = await get_user(message.from_user.id)
    if (OWNER_ID is None or int(message.from_user.id) != int(OWNER_ID)) and (not user or user.get("role") != "admin"):
        return
    if not message.reply_to_message:
        await message.reply(
            "📣 **Broadcast Command Guide**\n\n"
            "To broadcast a message or any media (photo, video, document, etc.):\n"
            "1️⃣ Reply to the message/media you want to send.\n"
            "2️⃣ Use `/broadcast` to send to **ALL** users.\n"
            "3️⃣ Use `/broadcast <user_id>` to send to a **SPECIFIC** user.\n"
            "4️⃣ Use `/broadcast <id1> <id2>` to send to **MULTIPLE** users.\n\n"
            "💡 **Examples:**\n"
            "• `/broadcast` (Reply to a photo to send it to everyone)\n"
            "• `/broadcast 12345678` (Sends to only one user)\n"
            "• `/broadcast 123 456 789` (Sends to three users)"
        )
        return
    parts = message.text.split()
    target_ids = parts[1:] if len(parts) > 1 else []
    msg = await message.reply("🚀 Starting broadcast...")
    count = 0
    blocked = 0
    index = 0
    if target_ids:
        for tid in target_ids:
            try:
                target_key = int(tid) if str(tid).strip("-").isdigit() else tid
                await message.reply_to_message.copy(target_key)
                count += 1
            except Exception:
                blocked += 1
            await asyncio.sleep(0.3)
        await update_status(msg, f"✅ Broadcast complete.\nTotal: {len(target_ids)}\nSent: {count}\nFailed/Blocked: {blocked}")
    else:
        total = await get_user_count()
        async for tid in iter_user_ids():
            try:
                await message.reply_to_message.copy(int(tid))
                count += 1
            except Exception as e:
                blocked += 1
                logging.debug(f"Broadcast failed for {tid}: {e}")
            index += 1
            if index % 20 == 0:
                await update_status(msg, f"🚀 Broadcasting...\nProgress: {index}/{total}\nSent: {count}\nFailed: {blocked}")
            await asyncio.sleep(0.3)
        await update_status(msg, f"✅ Broadcast complete.\nTotal: {total}\nSent: {count}\nFailed/Blocked: {blocked}")

PREMIUM_PAGE_SIZE = 8


def _build_premium_page(users: list, page: int, total: int) -> tuple[str, object]:
    from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    from datetime import datetime, timezone

    pages = max(1, (total + PREMIUM_PAGE_SIZE - 1) // PREMIUM_PAGE_SIZE)
    start = page * PREMIUM_PAGE_SIZE
    slice_ = users[start:start + PREMIUM_PAGE_SIZE]

    now = datetime.now(timezone.utc)

    lines = [
        "╔══════════════════════════╗",
        f"║  💎  Premium Members List  ║",
        "╚══════════════════════════╝",
        "",
        f"  Total: **{total}** active premium users",
        f"  Page **{page + 1}** of **{pages}**",
        "",
        "─────────────────────────────",
    ]

    for i, u in enumerate(slice_, start=start + 1):
        name = (u.get("full_name") or "Unknown").strip()[:20]
        username = f"@{u['username']}" if u.get("username") else "no username"
        uid = u.get("telegram_id")
        expiry = u.get("premium_expiry_date")

        if expiry is None:
            expiry_str = "♾️ Lifetime"
        else:
            if hasattr(expiry, "tzinfo"):
                if expiry.tzinfo is None:
                    expiry = expiry.replace(tzinfo=timezone.utc)
            else:
                try:
                    from datetime import datetime as dt
                    expiry = dt.fromisoformat(str(expiry)).replace(tzinfo=timezone.utc)
                except Exception:
                    expiry = None

            if expiry:
                diff = expiry - now
                days_left = diff.days
                if days_left < 0:
                    expiry_str = "⚠️ Expired"
                elif days_left == 0:
                    expiry_str = "⏳ Expires today"
                elif days_left <= 7:
                    expiry_str = f"🔴 {days_left}d left"
                elif days_left <= 30:
                    expiry_str = f"🟡 {days_left}d left"
                else:
                    expiry_str = f"🟢 {days_left}d left"
            else:
                expiry_str = "Unknown"

        lines.append(f"**{i}.** {name}")
        lines.append(f"    ├ {username}")
        lines.append(f"    ├ `{uid}`")
        lines.append(f"    └ {expiry_str}")
        if i < start + len(slice_):
            lines.append("")

    lines.append("─────────────────────────────")

    text = "\n".join(lines)

    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton("◀ Prev", callback_data=f"prem_page:{page - 1}"))
    nav_buttons.append(InlineKeyboardButton(f"{page + 1}/{pages}", callback_data="prem_noop"))
    if page < pages - 1:
        nav_buttons.append(InlineKeyboardButton("Next ▶", callback_data=f"prem_page:{page + 1}"))

    keyboard = InlineKeyboardMarkup([nav_buttons]) if nav_buttons else None
    return text, keyboard


@app.on_message(filters.command("premium_users") & filters.private, group=-1)
async def list_premium_users(client, message):
    user_id = message.from_user.id
    user = await get_user(user_id)
    if (OWNER_ID is None or int(user_id) != int(OWNER_ID)) and (not user or user.get("role") != "admin"):
        return
    try:
        from bot.database import pool
        from datetime import datetime
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT telegram_id, username, full_name, premium_expiry_date "
                "FROM users WHERE role = 'premium' "
                "AND (premium_expiry_date IS NULL OR premium_expiry_date >= $1) "
                "ORDER BY premium_expiry_date ASC NULLS LAST",
                datetime.now()
            )
        premium_users = [dict(row) for row in rows]
        total = len(premium_users)
        if total == 0:
            await message.reply(
                "╔══════════════════════╗\n"
                "║  💎  Premium Members  ║\n"
                "╚══════════════════════╝\n\n"
                "No active premium users found."
            )
            message.stop_propagation()
            return

        app._premium_cache = premium_users

        text, keyboard = _build_premium_page(premium_users, 0, total)
        await message.reply(text, reply_markup=keyboard)
    except Exception as e:
        await message.reply(f"❌ Error fetching premium list: {e}")
    message.stop_propagation()


@app.on_callback_query(filters.regex(r"^prem_page:(\d+)$"))
async def premium_page_callback(client, callback_query):
    user_id = callback_query.from_user.id
    user = await get_user(user_id)
    if (OWNER_ID is None or int(user_id) != int(OWNER_ID)) and (not user or user.get("role") != "admin"):
        await callback_query.answer("⛔ Unauthorized.", show_alert=True)
        return

    page = int(callback_query.matches[0].group(1))
    premium_users = getattr(app, "_premium_cache", None)

    if not premium_users:
        await callback_query.answer("Session expired. Run /premium_users again.", show_alert=True)
        return

    total = len(premium_users)
    pages = max(1, (total + PREMIUM_PAGE_SIZE - 1) // PREMIUM_PAGE_SIZE)
    if page < 0 or page >= pages:
        await callback_query.answer("Invalid page.", show_alert=True)
        return

    text, keyboard = _build_premium_page(premium_users, page, total)
    try:
        await callback_query.message.edit_text(text, reply_markup=keyboard)
    except Exception:
        pass
    await callback_query.answer()


@app.on_callback_query(filters.regex(r"^prem_noop$"))
async def premium_noop_callback(client, callback_query):
    await callback_query.answer()
