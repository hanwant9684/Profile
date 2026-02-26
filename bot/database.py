import os
import asyncpg
import redis.asyncio as redis
import logging
import asyncio
import json
from datetime import datetime, timedelta
from typing import Optional, Dict, List
from bot.config import OWNER_ID

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

DATABASE_URL = os.environ.get("DATABASE_URL")
REDIS_URL = os.environ.get("REDIS_URL")

pool: Optional[asyncpg.Pool] = None
redis_client: Optional[redis.Redis] = None

async def init_db():
    global pool, redis_client
    if pool:
        return
    
    try:
        if not DATABASE_URL:
            logger.error("DATABASE_URL is not set")
            return
        
        # Try to restore from cloud on startup
        from bot.cloud_backup import restore_from_github_async
        await restore_from_github_async()
        
        pool = await asyncpg.create_pool(DATABASE_URL)
        
        if REDIS_URL:
            try:
                redis_client = redis.from_url(REDIS_URL, decode_responses=True)
                # Test connection
                await redis_client.ping()
                logger.info("Redis connection established")
            except Exception as e:
                logger.error(f"Failed to connect to Redis at {REDIS_URL}: {e}")
                redis_client = None
        else:
            logger.warning("REDIS_URL is not set, running without Redis cache")
            redis_client = None
        
        async with pool.acquire() as conn:
            # Main table creation
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS users (
                    telegram_id BIGINT PRIMARY KEY,
                    username TEXT,
                    full_name TEXT,
                    role TEXT DEFAULT 'free',
                    downloads_today INTEGER DEFAULT 0,
                    last_download_date DATE,
                    is_agreed_terms BOOLEAN DEFAULT FALSE,
                    phone_session_string TEXT,
                    download_channel_id TEXT,
                    download_channel_hash TEXT,
                    premium_expiry_date TIMESTAMP WITH TIME ZONE,
                    is_banned BOOLEAN DEFAULT FALSE,
                    ads_today INTEGER DEFAULT 0,
                    last_ad_date DATE,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # Auto-migration: Add columns if they don't exist in existing table
            try:
                await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS username TEXT")
                await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS full_name TEXT")
            except Exception as e:
                logger.info(f"Migration notice (likely columns already exist): {e}")

            await conn.execute('''
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT,
                    json_value JSONB,
                    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            await conn.execute('CREATE INDEX IF NOT EXISTS idx_users_role_expiry ON users(role, premium_expiry_date)')
            await conn.execute('CREATE INDEX IF NOT EXISTS idx_users_banned ON users(is_banned)')
            
        logger.info("PostgreSQL database initialized")
    except Exception as e:
        logger.error(f"PostgreSQL initialization error: {e}")
        raise

async def get_user(user_id) -> Optional[Dict]:
    try:
        # Check Redis first
        cache_key = f"user:{user_id}"
        if redis_client:
            try:
                cached_user = await redis_client.get(cache_key)
                if cached_user:
                    return json.loads(cached_user)
            except Exception as e:
                logger.error(f"Redis get error: {e}")

        async with pool.acquire() as conn:
            row = await conn.fetchrow('SELECT * FROM users WHERE telegram_id = $1', int(user_id))
        
        if row:
            user = dict(row)
            # Ensure owner role is set if this is the owner
            if OWNER_ID and int(user_id) == int(OWNER_ID) and user.get("role") != "owner":
                await set_user_role(user_id, "owner")
                user["role"] = "owner"
                
            # Convert datetime objects to ISO strings for JSON serialization
            if user.get('premium_expiry_date'):
                user['premium_expiry_date'] = user['premium_expiry_date'].isoformat()
            if user.get('created_at'):
                user['created_at'] = user['created_at'].isoformat()
            if user.get('updated_at'):
                user['updated_at'] = user['updated_at'].isoformat()
            if user.get('last_download_date'):
                user['last_download_date'] = user['last_download_date'].isoformat()
            if user.get('last_ad_date'):
                user['last_ad_date'] = user['last_ad_date'].isoformat()
                
            # Cache in Redis for 10 minutes
            if redis_client:
                try:
                    await redis_client.setex(cache_key, 600, json.dumps(user))
                except Exception as e:
                    logger.error(f"Redis set error: {e}")
            return user
        
        if OWNER_ID and int(user_id) == int(OWNER_ID):
            user = await create_user(user_id)
            if user:
                await set_user_role(user_id, "owner")
                user["role"] = "owner"
            # Cache in Redis for 10 minutes
            await redis_client.setex(cache_key, 600, json.dumps(user))
            return user
        
        return None
    except Exception as e:
        logger.error(f"Error getting user {user_id}: {e}")
        return None

async def create_user(user_id, username=None, full_name=None) -> Optional[Dict]:
    try:
        now = datetime.now()
        today = now.date()
        
        async with pool.acquire() as conn:
            await conn.execute('''
                INSERT INTO users (telegram_id, username, full_name, role, downloads_today, last_download_date, 
                                   is_agreed_terms, is_banned, ads_today, created_at, updated_at)
                VALUES ($1, $2, $3, 'free', 0, $4, FALSE, FALSE, 0, $5, $6)
                ON CONFLICT (telegram_id) DO UPDATE SET 
                    username = EXCLUDED.username,
                    full_name = EXCLUDED.full_name,
                    updated_at = EXCLUDED.updated_at
            ''', int(user_id), username, full_name, today, now, now)
        
        # Clear cache
        if redis_client:
            await redis_client.delete(f"user:{user_id}")
        return await get_user(user_id)
    except Exception as e:
        logger.error(f"Error creating user {user_id}: {e}")
        return None

async def update_user_terms(user_id, agreed=True):
    try:
        async with pool.acquire() as conn:
            await conn.execute('UPDATE users SET is_agreed_terms = $1, updated_at = $2 WHERE telegram_id = $3',
                           agreed, datetime.now(), int(user_id))
        await redis_client.delete(f"user:{user_id}")
    except Exception as e:
        logger.error(f"Error updating terms for {user_id}: {e}")

async def save_session_string(user_id, session_string):
    try:
        async with pool.acquire() as conn:
            await conn.execute('UPDATE users SET phone_session_string = $1, updated_at = $2 WHERE telegram_id = $3',
                           session_string, datetime.now(), int(user_id))
        await redis_client.delete(f"user:{user_id}")
        logger.info(f"Saved session for user {user_id}")
    except Exception as e:
        logger.error(f"Error saving session for {user_id}: {e}")

async def logout_user(user_id):
    try:
        async with pool.acquire() as conn:
            await conn.execute('UPDATE users SET phone_session_string = NULL, updated_at = $1 WHERE telegram_id = $2',
                           datetime.now(), int(user_id))
        if redis_client:
            await redis_client.delete(f"user:{user_id}")
        logger.info(f"User {user_id} logged out")
    except Exception as e:
        logger.error(f"Error logging out user {user_id}: {e}")

async def update_user(user_id, data: dict):
    """Update user fields dynamically"""
    try:
        if not data:
            return
        
        fields = []
        values = []
        for i, (k, v) in enumerate(data.items(), start=1):
            fields.append(f"{k} = ${i}")
            values.append(v)
        
        # Add updated_at
        fields.append(f"updated_at = ${len(data) + 1}")
        values.append(datetime.now())
        # Add user_id
        values.append(int(user_id))
        
        query = f"UPDATE users SET {', '.join(fields)} WHERE telegram_id = ${len(values)}"
        
        async with pool.acquire() as conn:
            await conn.execute(query, *values)
        
        if redis_client:
            await redis_client.delete(f"user:{user_id}")
    except Exception as e:
        logger.error(f"Error updating user {user_id}: {e}")

async def update_user_channel(user_id, channel_id, channel_hash=None):
    try:
        async with pool.acquire() as conn:
            await conn.execute('UPDATE users SET download_channel_id = $1, download_channel_hash = $2, updated_at = $3 WHERE telegram_id = $4',
                           str(channel_id), channel_hash, datetime.now(), int(user_id))
        await redis_client.delete(f"user:{user_id}")
        logger.info(f"Updated download channel for user {user_id}: {channel_id}")
    except Exception as e:
        logger.error(f"Error updating user channel for {user_id}: {e}")

async def set_user_role(user_id, role, duration_days=None):
    try:
        expiry_date = None
        if role == 'premium' and duration_days:
            expiry_date = datetime.now() + timedelta(days=int(duration_days))
        
        async with pool.acquire() as conn:
            await conn.execute('UPDATE users SET role = $1, premium_expiry_date = $2, updated_at = $3 WHERE telegram_id = $4',
                           role, expiry_date, datetime.now(), int(user_id))
        await redis_client.delete(f"user:{user_id}")
    except Exception as e:
        logger.error(f"Error setting role for {user_id}: {e}")

async def ban_user(user_id, is_banned=True):
    try:
        async with pool.acquire() as conn:
            await conn.execute('UPDATE users SET is_banned = $1, updated_at = $2 WHERE telegram_id = $3',
                           is_banned, datetime.now(), int(user_id))
        await redis_client.delete(f"user:{user_id}")
    except Exception as e:
        logger.error(f"Error banning user {user_id}: {e}")

async def check_and_update_quota(user_id):
    try:
        user = await get_user(user_id)
        if not user:
            return False, "User not found."
        
        if user.get("is_banned"):
            return False, "You are banned from using this bot."
        
        now = datetime.now()
        today = now.date()
        
        if user.get("role") == 'premium' and user.get("premium_expiry_date"):
            try:
                # Handle both ISO string and datetime object
                expiry_val = user["premium_expiry_date"]
                if isinstance(expiry_val, str):
                    expiry = datetime.fromisoformat(expiry_val)
                else:
                    expiry = expiry_val
                
                # Ensure comparison is timezone-aware if expiry has timezone
                if expiry.tzinfo is not None and now.tzinfo is None:
                    from datetime import timezone
                    now = datetime.now(timezone.utc)
                elif expiry.tzinfo is None and now.tzinfo is not None:
                    expiry = expiry.replace(tzinfo=None)

                if expiry < now:
                    # Automatically update role to free and clear Redis
                    async with pool.acquire() as conn:
                        await conn.execute("UPDATE users SET role = 'free', updated_at = $1 WHERE telegram_id = $2", now, int(user_id))
                    if redis_client:
                        await redis_client.delete(f"user:{user_id}")
                    user["role"] = "free"
            except Exception as e:
                logger.error(f"Error checking premium expiry for {user_id}: {e}")
        
        if user.get("role") in ['premium', 'admin', 'owner']:
            return True, "Unlimited"
        
        last_download_date = None
        if user.get("last_download_date"):
            last_download_date = datetime.fromisoformat(user["last_download_date"]).date()

        if last_download_date != today:
            async with pool.acquire() as conn:
                await conn.execute('UPDATE users SET downloads_today = 0, last_download_date = $1 WHERE telegram_id = $2',
                               today, int(user_id))
            await redis_client.delete(f"user:{user_id}")
            user["downloads_today"] = 0
        
        if user.get("downloads_today", 0) >= 5:
            return False, "Daily limit reached (5/5). Upgrade to Premium for unlimited downloads. use /upgrade"
        
        return True, f"{user.get('downloads_today', 0)}/5"
    except Exception as e:
        logger.error(f"Error checking quota for {user_id}: {e}")
        return False, "Database error."

async def increment_quota(user_id, count=1):
    try:
        user = await get_user(user_id)
        if not user or user.get("role") != 'free':
            return
            
        async with pool.acquire() as conn:
            await conn.execute('UPDATE users SET downloads_today = downloads_today + $1 WHERE telegram_id = $2',
                           count, int(user_id))
        await redis_client.delete(f"user:{user_id}")
    except Exception as e:
        logger.error(f"Error incrementing quota for {user_id}: {e}")

async def increment_ad_count(user_id):
    try:
        today = datetime.now().date()
        async with pool.acquire() as conn:
            await conn.execute('UPDATE users SET ads_today = ads_today + 1, last_ad_date = $1 WHERE telegram_id = $2',
                           today, int(user_id))
        await redis_client.delete(f"user:{user_id}")
    except Exception as e:
        logger.error(f"Error incrementing ad count for {user_id}: {e}")

async def get_ad_count_today(user_id):
    try:
        user = await get_user(user_id)
        if not user:
            return 0
        
        today = datetime.now().date()
        last_ad_date = None
        
        # user['last_ad_date'] is already an ISO string from get_user
        if user.get("last_ad_date"):
            try:
                last_ad_date = datetime.fromisoformat(user["last_ad_date"]).date()
            except (ValueError, TypeError):
                last_ad_date = None

        if last_ad_date != today:
            # If the date has changed, reset the count for the new day
            async with pool.acquire() as conn:
                await conn.execute('UPDATE users SET ads_today = 0, last_ad_date = $1 WHERE telegram_id = $2',
                               today, int(user_id))
            if redis_client:
                await redis_client.delete(f"user:{user_id}")
            return 0
            
        return user.get("ads_today", 0)
    except Exception as e:
        logger.error(f"Error getting ad count for {user_id}: {e}")
        return 0

async def get_remaining_quota(user_id):
    try:
        user = await get_user(user_id)
        if not user:
            return 0, False
        
        if user.get("role") in ['premium', 'admin', 'owner']:
            return 999999, True
        
        today = datetime.now().date()
        downloads_today = user.get("downloads_today", 0)
        
        last_download_date = None
        if user.get("last_download_date"):
            last_download_date = datetime.fromisoformat(user["last_download_date"]).date()

        if last_download_date != today:
            downloads_today = 0
        
        remaining = max(0, 5 - downloads_today)
        return remaining, False
    except Exception as e:
        logger.error(f"Error getting remaining quota for {user_id}: {e}")
        return 0, False

async def get_setting(key):
    try:
        # Check Redis first
        cache_key = f"setting:{key}"
        if redis_client:
            try:
                cached_val = await redis_client.get(cache_key)
                if cached_val:
                    return json.loads(cached_val)
            except Exception as e:
                logger.error(f"Redis get setting error: {e}")

        async with pool.acquire() as conn:
            row = await conn.fetchrow('SELECT * FROM settings WHERE key = $1', key)
        
        if row:
            res = dict(row)
            if res.get('json_value'):
                res['json_value'] = json.dumps(res['json_value'])
            if res.get('updated_at'):
                res['updated_at'] = res['updated_at'].isoformat()
            
            await redis_client.setex(cache_key, 3600, json.dumps(res))
            return res
        return None
    except Exception as e:
        logger.error(f"Error getting setting {key}: {e}")
        return None

async def update_setting(key, value, json_value=None):
    try:
        if json_value and isinstance(json_value, str):
            json_value = json.loads(json_value)
            
        async with pool.acquire() as conn:
            await conn.execute('''
                INSERT INTO settings (key, value, json_value, updated_at)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT(key) DO UPDATE SET value = $2, json_value = $3, updated_at = $4
            ''', key, value, json_value, datetime.now())
        await redis_client.delete(f"setting:{key}")
    except Exception as e:
        logger.error(f"Error updating setting {key}: {e}")

async def get_all_users() -> List[Dict]:
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch('SELECT * FROM users')
        return [dict(row) for row in rows]
    except Exception as e:
        logger.error(f"Error getting all users: {e}")
        return []

async def get_user_count():
    try:
        async with pool.acquire() as conn:
            return await conn.fetchval('SELECT COUNT(*) FROM users')
    except Exception as e:
        logger.error(f"Error getting user count: {e}")
        return 0
