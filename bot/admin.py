import asyncio
from pyrogram import filters
from bot.config import app, OWNER_ID, active_downloads, MAX_CONCURRENT_DOWNLOADS
from bot.database import set_user_role, ban_user, update_setting, get_setting, get_all_users, get_user_count

@app.on_message(filters.command("stats") & filters.private)
async def stats(client, message):
    if str(message.from_user.id) != str(OWNER_ID): return
    
    total_users = await get_user_count()
    
    await message.reply(
        f"📊 **Bot Statistics**\n\n"
        f"👥 Total Users: `{total_users}`\n"
        f"⚡ Active Downloads: `{len(active_downloads)}/{MAX_CONCURRENT_DOWNLOADS}`"
    )

@app.on_message(filters.command("killall") & filters.private)
async def kill_all_processes(client, message):
    if str(message.from_user.id) != str(OWNER_ID): return
    
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
    user_id = str(message.from_user.id)
    if user_id != str(OWNER_ID):
        await message.reply("⛔ Authorized personnel only.")
        return
        
    try:
        parts = message.text.split()
        if len(parts) < 3:
             raise ValueError("Not enough arguments")
        
        target_id = parts[1]
        new_role = parts[2]
        duration = parts[3] if len(parts) > 3 else None

        if new_role not in ['free', 'premium', 'admin', 'owner']:
            await message.reply("Invalid role. Use: free, premium, admin, owner")
            return
            
        await set_user_role(target_id, new_role, duration)
        
        resp = f"✅ User `{target_id}` role updated to **{new_role}**."
        if duration and new_role == 'premium':
            resp += f" (Expires in {duration} days)"
            
        await message.reply(resp)
    except ValueError:
        await message.reply("Usage: `/setrole <user_id> <role> [days]`")
    except Exception as e:
        await message.reply(f"Error: {e}")

@app.on_message(filters.command("ban") & filters.private)
async def ban(client, message):
    user_id = str(message.from_user.id)
    if user_id != str(OWNER_ID):
        return
        
    try:
        target_id = message.text.split()[1]
        await ban_user(target_id, True)
        await message.reply(f"🚫 User `{target_id}` has been **BANNED**.")
    except:
        await message.reply("Usage: `/ban <user_id>`")

@app.on_message(filters.command("unban") & filters.private)
async def unban(client, message):
    user_id = str(message.from_user.id)
    if user_id != str(OWNER_ID):
        return
        
    try:
        target_id = message.text.split()[1]
        await ban_user(target_id, False)
        await message.reply(f"✅ User `{target_id}` has been **UNBANNED**.")
    except:
        await message.reply("Usage: `/unban <user_id>`")

@app.on_message(filters.command("set_force_sub") & filters.private)
async def set_force_sub(client, message):
    user_id = str(message.from_user.id)
    if user_id != str(OWNER_ID):
        return
    
    try:
        channel = message.text.split()[1]
        await update_setting("force_sub_channel", channel)
        await message.reply(f"✅ Force Sub channel set to: {channel}")
    except:
        await message.reply("Usage: `/set_force_sub @channel`")

@app.on_message(filters.command("set_dump") & filters.private)
async def set_dump(client, message):
    user_id = str(message.from_user.id)
    if user_id != str(OWNER_ID):
        return
    
    try:
        channel_id = message.text.split()[1]
        await update_setting("dump_channel_id", channel_id)
        await message.reply(f"✅ Dump channel ID set to: `{channel_id}`")
    except:
        await message.reply("Usage: `/set_dump <channel_id>`")

@app.on_message(filters.command("settings") & filters.private)
async def view_settings(client, message):
    user_id = str(message.from_user.id)
    if user_id != str(OWNER_ID):
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
    user_id = str(message.from_user.id)
    if user_id != str(OWNER_ID):
        return
        
    if not message.reply_to_message:
        await message.reply("Reply to a message to broadcast it.")
        return
        
    msg = await message.reply("🚀 Starting broadcast...")
    
    users = await get_all_users()
    
    count = 0
    blocked = 0
    
    for row in users:
        try:
            await message.reply_to_message.copy(row.get('telegram_id'))
            count += 1
            await asyncio.sleep(0.05)
        except Exception:
            blocked += 1
    
    await msg.edit_text(f"✅ Broadcast complete.\nSent: {count}\nFailed/Blocked: {blocked}")

@app.on_message(filters.command("premium_users") & filters.private, group=-1)
async def list_premium_users(client, message):
    user_id = message.from_user.id
    from bot.config import OWNER_ID
    
    if str(user_id) != str(OWNER_ID):
        return
        
    try:
        from bot.database import db
        if db is None:
            await message.reply("❌ Database not initialized.")
            return

        users_coll = db["users"]
        premium_users = await users_coll.find({"role": "premium"}).to_list(length=None)
        
        if not premium_users:
            await message.reply("No premium users found.")
            return

        text = "💎 **Premium Users List**\n\n"
        for user in premium_users:
            u_id = user.get("telegram_id")
            expiry = user.get("premium_expiry_date", "Never")
            
            name = "Unknown"
            username_str = ""
            
            try:
                # Direct fetch from Telegram API to ensure current info
                # Cast to int for get_users if u_id is a numeric string
                user_key = int(u_id) if str(u_id).strip("-").isdigit() else u_id
                tg_user = await client.get_users(user_key)
                
                name = tg_user.first_name or "No Name"
                if tg_user.last_name:
                    name += f" {tg_user.last_name}"
                
                if tg_user.username:
                    username_str = f" (@{tg_user.username})"
            except Exception as e:
                # Fallback to DB info if Telegram fetch fails
                name = user.get("first_name") or "Unknown"
                if user.get("last_name"):
                    name += f" {user['last_name']}"
                
                username = user.get("username")
                if username:
                    username_str = f" (@{username})"
                
            text += f"👤 Name: **{name}**{username_str}\n🆔 ID: `{u_id}`\n📅 Expiry: `{expiry}`\n\n"
        
        if len(text) > 4096:
            for x in range(0, len(text), 4096):
                await message.reply(text[x:x+4096])
        else:
            await message.reply(text)
    except Exception as e:
        await message.reply(f"Error: {e}")
    
    message.stop_propagation()

