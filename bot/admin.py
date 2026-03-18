import asyncio
import logging
from pyrogram import filters

async def update_status(msg, text):
    try:
        await msg.edit_text(text)
    except Exception as e:
        logging.debug(f"Status update failed: {e}")

from bot.config import app, OWNER_ID, active_downloads, MAX_CONCURRENT_DOWNLOADS
from bot.database import set_user_role, ban_user, update_setting, get_setting, get_all_users, get_user_count, get_user, iter_user_ids

@app.on_message(filters.command("stats") & filters.private)
async def stats(client, message):
    user = await get_user(message.from_user.id)
    if (OWNER_ID is None or int(message.from_user.id) != int(OWNER_ID)) and (not user or user.get("role") != "admin"): return
    
    total_users = await get_user_count()
    
    await message.reply(
        f"📊 **Bot Statistics**\n\n"
        f"👥 Total Users: `{total_users}`\n"
        f"⚡ Active Downloads: `{len(active_downloads)}/{MAX_CONCURRENT_DOWNLOADS}`"
    )

@app.on_message(filters.command("killall") & filters.private)
async def kill_all_processes(client, message):
    user = await get_user(message.from_user.id)
    if (OWNER_ID is None or int(message.from_user.id) != int(OWNER_ID)) and (not user or user.get("role") != "admin"): return
    
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

@app.on_message(filters.command("set_dump") & filters.private)
async def set_dump(client, message):
    user = await get_user(message.from_user.id)
    if (OWNER_ID is None or int(message.from_user.id) != int(OWNER_ID)) and (not user or user.get("role") != "admin"):
        return
    
    try:
        channel_id = message.text.split()[1]
        await update_setting("dump_channel_id", channel_id)
        await message.reply(f"✅ Dump channel ID set to: `{channel_id}`")
    except Exception:
        await message.reply("Usage: `/set_dump <channel_id>`")

@app.on_message(filters.command("settings") & filters.private)
async def view_settings(client, message):
    user = await get_user(message.from_user.id)
    if (OWNER_ID is None or int(message.from_user.id) != int(OWNER_ID)) and (not user or user.get("role") != "admin"):
        return
        
    fs = await get_setting("force_sub_channel")
    dc = await get_setting("dump_channel_id")
    ac = await get_setting("ad_config")
    
    fs_val = fs.get('value') if fs else "Not Set"
    dc_val = dc.get('value') if dc else "Not Set"
    ac_val = ac.get('json_value') if ac else "Disabled"
    
    text = (
        "⚙️ **Current Settings**\n\n"
        f"📢 Force Sub: `{fs_val}`\n"
        f"🗑️ Dump Channel: `{dc_val}`\n"
        f"📺 Ads Config: `{ac_val}`"
    )
    await message.reply(text)

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
        # Broadcast to specific users
        for tid in target_ids:
            try:
                target_key = int(tid) if str(tid).strip("-").isdigit() else tid
                await message.reply_to_message.copy(target_key)
                count += 1
            except Exception:
                blocked += 1
            await asyncio.sleep(0.05)
        await update_status(msg, f"✅ Broadcast complete.\nTotal: {len(target_ids)}\nSent: {count}\nFailed/Blocked: {blocked}")
    else:
        # Paginated broadcast — never loads all users into RAM
        total = await get_user_count()
        async for tid in iter_user_ids():
            try:
                await message.reply_to_message.copy(int(tid))
                count += 1
            except Exception as e:
                blocked += 1
                logging.debug(f"Broadcast failed for {tid}: {e}")

            index += 1
            if index % 50 == 0:
                await update_status(msg, f"🚀 Broadcasting...\nProgress: {index}/{total}\nSent: {count}\nFailed: {blocked}")

            await asyncio.sleep(0.05)

        await update_status(msg, f"✅ Broadcast complete.\nTotal: {total}\nSent: {count}\nFailed/Blocked: {blocked}")

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
            rows = await conn.fetch("SELECT telegram_id, username, full_name, premium_expiry_date FROM users WHERE role = 'premium' AND (premium_expiry_date IS NULL OR premium_expiry_date >= $1)", datetime.now())
        premium_users = [dict(row) for row in rows]
        
        if not premium_users:
            await message.reply("No premium users found.")
            return

        text = "💎 **Premium Users List**\n\n"
        for user_data in premium_users:
            u_id = user_data.get("telegram_id")
            expiry = user_data.get("premium_expiry_date", "Never")
            
            name = user_data.get("full_name") or "No Name"
            username_str = ""
            if user_data.get("username"):
                username_str = f" (@{user_data['username']})"
                
            text += f"👤 Name: **{name}**{username_str}\n🆔 ID: `{u_id}`\n📅 Expiry: `{expiry}`\n\n"
        
        if len(text) > 4096:
            for x in range(0, len(text), 4096):
                await message.reply(text[x:x+4096])
        else:
            await message.reply(text)
    except Exception as e:
        await message.reply(f"Error: {e}")
    
    message.stop_propagation()
