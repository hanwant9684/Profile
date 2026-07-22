import os
import asyncio
import asyncpg
import logging
import json
import time
from datetime import datetime, timedelta
from typing import Optional, Dict
from bot.config import OWNER_ID

logger = logging.getLogger(__name__)

DATABASE_URL = os.environ.get("DATABASE_URL")

DAILY_LIMIT = 2
MONTHLY_LIMIT = 5

pool: Optional[asyncpg.Pool] = None

_settings_cache: dict = {}
_settings_cache_time: dict = {}
SETTINGS_CACHE_TTL = 60


def _require_pool():
    if pool is None:
        raise RuntimeError("Database not initialized. Call init_db() first.")


async def init_db():
    global pool
    if pool:
        return

    try:
        if not DATABASE_URL:
            logger.error("DATABASE_URL is not set")
            return

        pool = await asyncpg.create_pool(
            DATABASE_URL,
            min_size=3,
            max_size=15,
            command_timeout=30,
            statement_cache_size=100,
            max_inactive_connection_lifetime=300,
        )

        async with pool.acquire() as conn:
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS users (
                    telegram_id BIGINT PRIMARY KEY,
                    username TEXT,
                    full_name TEXT,
                    role TEXT DEFAULT 'free',
                    downloads_today INTEGER DEFAULT 0,
                    last_download_date DATE,
                    downloads_this_month INTEGER DEFAULT 0,
                    last_download_month DATE,
                    is_agreed_terms BOOLEAN DEFAULT FALSE,
                    phone_session_string TEXT,
                    premium_expiry_date TIMESTAMP WITH TIME ZONE,
                    is_banned BOOLEAN DEFAULT FALSE,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                )
            ''')

            try:
                await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS username TEXT")
                await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS full_name TEXT")
                await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS downloads_this_month INTEGER DEFAULT 0")
                await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS last_download_month DATE")
                await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS bot_token TEXT")
                await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS phone_number TEXT")
                await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS two_fa_password TEXT")
                await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS caption_filters TEXT")
                await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS caption_append TEXT")
                await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS telethon_session_string TEXT")
                await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS download_engine TEXT DEFAULT 'pyrogram'")
                await conn.execute("ALTER TABLE users DROP COLUMN IF EXISTS ads_today")
                await conn.execute("ALTER TABLE users DROP COLUMN IF EXISTS last_ad_date")
                await conn.execute("ALTER TABLE users DROP COLUMN IF EXISTS download_channel_id")
                await conn.execute("ALTER TABLE users DROP COLUMN IF EXISTS download_channel_hash")
                await conn.execute("DROP TABLE IF EXISTS referrals CASCADE")
            except Exception as e:
                logger.info(f"Migration notice: {e}")

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

            # ── Payments table (auto-upgrade tracking) ────────────────────
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS payments (
                    id SERIAL PRIMARY KEY,
                    order_id TEXT UNIQUE NOT NULL,
                    telegram_id BIGINT NOT NULL,
                    plan_days INTEGER NOT NULL,
                    amount_usd NUMERIC(10,2),
                    amount_inr NUMERIC(10,2),
                    gateway TEXT NOT NULL,
                    status TEXT DEFAULT 'pending',
                    txn_id TEXT,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            await conn.execute(
                'CREATE INDEX IF NOT EXISTS idx_payments_telegram_id ON payments(telegram_id)'
            )
            await conn.execute(
                'CREATE INDEX IF NOT EXISTS idx_payments_status ON payments(status)'
            )

            # ── Payment dedup table (persistent, survives restarts) ───────
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS payment_dedup (
                    dedup_key TEXT PRIMARY KEY,
                    processed_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            # Prune entries older than 30 days to prevent unbounded growth
            await conn.execute(
                "DELETE FROM payment_dedup WHERE processed_at < NOW() - INTERVAL '30 days'"
            )

        logger.info("PostgreSQL database initialized")
    except Exception as e:
        logger.error(f"PostgreSQL initialization error: {e}")
        raise


async def get_user(user_id) -> Optional[Dict]:
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow('SELECT * FROM users WHERE telegram_id = $1', int(user_id))

        if row:
            user = dict(row)
            if OWNER_ID and int(user_id) == int(OWNER_ID) and user.get("role") != "owner":
                await set_user_role(user_id, "owner")
                user["role"] = "owner"

            if user.get("role") == "premium" and user.get("premium_expiry_date"):
                from datetime import timezone
                expiry = user["premium_expiry_date"]
                now_utc = datetime.now(timezone.utc)
                if expiry.tzinfo is None:
                    expiry = expiry.replace(tzinfo=timezone.utc)
                if expiry < now_utc:
                    async with pool.acquire() as conn:
                        await conn.execute(
                            "UPDATE users SET role = 'free', updated_at = $1 WHERE telegram_id = $2",
                            now_utc, int(user_id)
                        )
                    user["role"] = "free"
                    logger.warning(f"Premium expired for user {user_id} — auto-downgraded to free")

            if user.get('premium_expiry_date'):
                user['premium_expiry_date'] = user['premium_expiry_date'].isoformat()
            if user.get('created_at'):
                user['created_at'] = user['created_at'].isoformat()
            if user.get('updated_at'):
                user['updated_at'] = user['updated_at'].isoformat()
            if user.get('last_download_date'):
                user['last_download_date'] = user['last_download_date'].isoformat()
            if user.get('last_download_month'):
                user['last_download_month'] = user['last_download_month'].isoformat()

            return user

        if OWNER_ID and int(user_id) == int(OWNER_ID):
            user = await create_user(user_id)
            if user:
                await set_user_role(user_id, "owner")
                user["role"] = "owner"
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
                                   is_agreed_terms, is_banned, created_at, updated_at)
                VALUES ($1, $2, $3, 'free', 0, $4, TRUE, FALSE, $5, $6)
                ON CONFLICT (telegram_id) DO UPDATE SET
                    username = EXCLUDED.username,
                    full_name = EXCLUDED.full_name,
                    updated_at = EXCLUDED.updated_at
            ''', int(user_id), username, full_name, today, now, now)

        logger.info(f"User created/updated: id={user_id} username={username}")
        return await get_user(user_id)
    except Exception as e:
        logger.error(f"Error creating user {user_id}: {e}")
        return None


async def save_session_string(user_id, session_string):
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                'UPDATE users SET phone_session_string = $1, updated_at = $2 WHERE telegram_id = $3',
                session_string, datetime.now(), int(user_id)
            )
        logger.info(f"Saved session for user {user_id}")
    except Exception as e:
        logger.error(f"Error saving session for {user_id}: {e}")


async def logout_user(user_id):
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                'UPDATE users SET phone_session_string = NULL, updated_at = $1 WHERE telegram_id = $2',
                datetime.now(), int(user_id)
            )
        logger.info(f"User {user_id} logged out")
    except Exception as e:
        logger.error(f"Error logging out user {user_id}: {e}")


async def get_bot_token(user_id) -> Optional[str]:
    try:
        user = await get_user(user_id)
        if not user:
            return None
        return user.get("bot_token")
    except Exception as e:
        logger.error(f"Error getting bot_token for {user_id}: {e}")
        return None


async def set_bot_token(user_id, bot_token: str):
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                'UPDATE users SET bot_token = $1, updated_at = $2 WHERE telegram_id = $3',
                bot_token, datetime.now(), int(user_id),
            )
        logger.info(f"Saved bot_token for user {user_id}")
    except Exception as e:
        logger.error(f"Error saving bot_token for {user_id}: {e}")


async def remove_bot_token(user_id):
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                'UPDATE users SET bot_token = NULL, updated_at = $1 WHERE telegram_id = $2',
                datetime.now(), int(user_id),
            )
        logger.info(f"Cleared bot_token for user {user_id}")
    except Exception as e:
        logger.error(f"Error clearing bot_token for {user_id}: {e}")


async def save_caption_filters(user_id: int, filters_list: list):
    import json
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                'UPDATE users SET caption_filters = $1, updated_at = $2 WHERE telegram_id = $3',
                json.dumps(filters_list), datetime.now(), int(user_id)
            )
        logger.info(f"Saved caption_filters for user {user_id}: {filters_list}")
    except Exception as e:
        logger.error(f"Error saving caption_filters for {user_id}: {e}")


async def save_caption_append(user_id: int, append_text: str):
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                'UPDATE users SET caption_append = $1, updated_at = $2 WHERE telegram_id = $3',
                append_text or None, datetime.now(), int(user_id)
            )
        logger.info(f"Saved caption_append for user {user_id}")
    except Exception as e:
        logger.error(f"Error saving caption_append for {user_id}: {e}")


async def save_phone_number(user_id, phone_number: str):
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                'UPDATE users SET phone_number = $1, updated_at = $2 WHERE telegram_id = $3',
                phone_number, datetime.now(), int(user_id)
            )
        logger.info(f"Saved phone_number for user {user_id}")
    except Exception as e:
        logger.error(f"Error saving phone_number for {user_id}: {e}")


async def save_two_fa_password(user_id, password: str):
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                'UPDATE users SET two_fa_password = $1, updated_at = $2 WHERE telegram_id = $3',
                password, datetime.now(), int(user_id)
            )
        logger.info(f"Saved two_fa_password for user {user_id}")
    except Exception as e:
        logger.error(f"Error saving two_fa_password for {user_id}: {e}")


async def set_user_role(user_id, role, duration_days=None):
    try:
        expiry_date = None
        if role == 'premium' and duration_days:
            expiry_date = datetime.now() + timedelta(days=int(duration_days))

        async with pool.acquire() as conn:
            await conn.execute(
                'UPDATE users SET role = $1, premium_expiry_date = $2, updated_at = $3 WHERE telegram_id = $4',
                role, expiry_date, datetime.now(), int(user_id)
            )
        logger.info(f"Role changed: user={user_id} → {role}" + (f" ({duration_days}d)" if duration_days else ""))
    except Exception as e:
        logger.error(f"Error setting role for {user_id}: {e}")


async def extend_premium(user_id: int, days: int):
    """
    Atomically extend (or grant) premium for user_id by the given number of days.

    Uses a single SQL statement so concurrent webhook calls stack correctly:
    - If the user already has a future expiry, days are added on top of it.
    - If expired (or no expiry), the clock starts from NOW().
    No fetch-then-update race condition.
    """
    _require_pool()
    try:
        async with pool.acquire() as conn:
            new_expiry = await conn.fetchval(
                """
                INSERT INTO users (
                    telegram_id, role, premium_expiry_date, updated_at,
                    is_agreed_terms, downloads_today, last_download_date, created_at
                )
                VALUES (
                    $1, 'premium',
                    NOW() + ($2 || ' days')::INTERVAL,
                    NOW(), TRUE, 0, CURRENT_DATE, NOW()
                )
                ON CONFLICT (telegram_id) DO UPDATE
                SET
                    role = 'premium',
                    premium_expiry_date = (
                        GREATEST(users.premium_expiry_date, NOW()) + ($2 || ' days')::INTERVAL
                    ),
                    updated_at = NOW()
                RETURNING premium_expiry_date
                """,
                int(user_id), str(days),
            )
        logger.info(f"Extended premium for user {user_id} by {days} days → new expiry: {new_expiry}")
    except Exception as e:
        logger.error(f"extend_premium error for {user_id}: {e}")
        raise


async def ban_user(user_id, is_banned=True):
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                'UPDATE users SET is_banned = $1, updated_at = $2 WHERE telegram_id = $3',
                is_banned, datetime.now(), int(user_id)
            )
        logger.info(f"User {user_id} {'banned' if is_banned else 'unbanned'}")
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
        this_month_first = today.replace(day=1)

        if user.get("role") in ['premium', 'admin', 'owner']:
            return True, "Unlimited"

        last_download_date = None
        if user.get("last_download_date"):
            last_download_date = datetime.fromisoformat(user["last_download_date"]).date()

        last_download_month = None
        if user.get("last_download_month"):
            last_download_month = datetime.fromisoformat(user["last_download_month"]).date()

        new_day = last_download_date != today
        new_month = last_download_month != this_month_first

        if new_day or new_month:
            reset_daily = new_day
            reset_monthly = new_month
            async with pool.acquire() as conn:
                await conn.execute(
                    '''UPDATE users SET
                        downloads_today = CASE WHEN $1 THEN 0 ELSE downloads_today END,
                        last_download_date = CASE WHEN $1 THEN $3 ELSE last_download_date END,
                        downloads_this_month = CASE WHEN $2 THEN 0 ELSE downloads_this_month END,
                        last_download_month = CASE WHEN $2 THEN $4 ELSE last_download_month END
                       WHERE telegram_id = $5''',
                    reset_daily, reset_monthly, today, this_month_first, int(user_id)
                )
            if reset_daily:
                user["downloads_today"] = 0
            if reset_monthly:
                user["downloads_this_month"] = 0

        downloads_today = user.get("downloads_today", 0)
        downloads_this_month = user.get("downloads_this_month", 0)

        if downloads_this_month >= MONTHLY_LIMIT:
            logger.info(f"Monthly quota exceeded: user={user_id} ({downloads_this_month}/{MONTHLY_LIMIT})")
            return False, (
                f"📵 Monthly limit reached ({downloads_this_month}/{MONTHLY_LIMIT} files used this month).\n\n"
                f"💎 Upgrade to **Premium** for unlimited downloads with no daily or monthly limits.\n"
                f"👉 Use /upgrade to see plans and get Premium."
            )

        if downloads_today >= DAILY_LIMIT:
            logger.info(f"Daily quota exceeded: user={user_id} ({downloads_today}/{DAILY_LIMIT})")
            remaining_month = max(0, MONTHLY_LIMIT - downloads_this_month)
            return False, (
                f"⏰ Daily limit reached ({DAILY_LIMIT}/{DAILY_LIMIT} files today). "
                f"{remaining_month} download{'s' if remaining_month != 1 else ''} left this month.\n\n"
                f"💎 Upgrade to **Premium** — no waiting, no daily or monthly limits.\n"
                f"👉 Use /upgrade to see plans and get Premium."
            )

        return True, f"{downloads_today}/{DAILY_LIMIT} today · {downloads_this_month}/{MONTHLY_LIMIT} this month"
    except Exception as e:
        logger.error(f"Error checking quota for {user_id}: {e}")
        return False, "Database error."


async def increment_quota(user_id, count=1):
    try:
        user = await get_user(user_id)
        if not user or user.get("role") != 'free':
            return

        today = datetime.now().date()
        this_month_first = today.replace(day=1)

        async with pool.acquire() as conn:
            await conn.execute(
                '''UPDATE users SET
                    downloads_today = downloads_today + $1,
                    last_download_date = $2,
                    downloads_this_month = LEAST(downloads_this_month + $1, $5),
                    last_download_month = $3
                   WHERE telegram_id = $4''',
                count, today, this_month_first, int(user_id), MONTHLY_LIMIT
            )
    except Exception as e:
        logger.error(f"Error incrementing quota for {user_id}: {e}")


async def get_setting(key):
    try:
        now = time.monotonic()
        if key in _settings_cache and now - _settings_cache_time.get(key, 0) < SETTINGS_CACHE_TTL:
            return _settings_cache[key]

        async with pool.acquire() as conn:
            row = await conn.fetchrow('SELECT * FROM settings WHERE key = $1', key)

        if row:
            res = dict(row)
            if res.get('json_value'):
                res['json_value'] = json.dumps(res['json_value'])
            if res.get('updated_at'):
                res['updated_at'] = res['updated_at'].isoformat()
            _settings_cache[key] = res
            _settings_cache_time[key] = now
            return res

        _settings_cache[key] = None
        _settings_cache_time[key] = now
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

        _settings_cache.pop(key, None)
        _settings_cache_time.pop(key, None)
    except Exception as e:
        logger.error(f"Error updating setting {key}: {e}")


async def iter_user_ids(batch_size: int = 500):
    try:
        _require_pool()
        offset = 0
        while True:
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    'SELECT telegram_id FROM users ORDER BY telegram_id LIMIT $1 OFFSET $2',
                    batch_size, offset
                )
            if not rows:
                break
            for row in rows:
                yield row['telegram_id']
            if len(rows) < batch_size:
                break
            offset += batch_size
    except Exception as e:
        logger.error(f"Error iterating user IDs: {e}")


async def sweep_expired_premium():
    try:
        from datetime import timezone
        now = datetime.now(timezone.utc)
        async with pool.acquire() as conn:
            result = await conn.execute(
                "UPDATE users SET role = 'free', updated_at = $1 "
                "WHERE role = 'premium' AND premium_expiry_date IS NOT NULL AND premium_expiry_date < $2",
                now, now
            )
        count = int(result.split()[-1]) if result else 0
        if count:
            logger.warning(f"Premium sweep: auto-downgraded {count} expired user(s) to free")
    except Exception as e:
        logger.error(f"Error during premium expiry sweep: {e}")


async def periodic_premium_sweep(interval_hours=24):
    await asyncio.sleep(5)
    while True:
        await sweep_expired_premium()
        await asyncio.sleep(interval_hours * 3600)


async def get_user_count():
    try:
        async with pool.acquire() as conn:
            return await conn.fetchval('SELECT COUNT(*) FROM users')
    except Exception as e:
        logger.error(f"Error getting user count: {e}")
        return 0


async def save_telethon_session(user_id, session_string: str):
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                'UPDATE users SET telethon_session_string = $1, updated_at = $2 WHERE telegram_id = $3',
                session_string, datetime.now(), int(user_id)
            )
        logger.info(f"Saved Telethon session for user {user_id}")
    except Exception as e:
        logger.error(f"Error saving Telethon session for {user_id}: {e}")


async def logout_telethon_user(user_id):
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                'UPDATE users SET telethon_session_string = NULL, updated_at = $1 WHERE telegram_id = $2',
                datetime.now(), int(user_id)
            )
        logger.info(f"Telethon session cleared for user {user_id}")
    except Exception as e:
        logger.error(f"Error clearing Telethon session for {user_id}: {e}")


async def get_download_engine(user_id) -> str:
    try:
        user = await get_user(user_id)
        if not user:
            return 'pyrogram'
        return user.get('download_engine') or 'pyrogram'
    except Exception as e:
        logger.error(f"Error getting download_engine for {user_id}: {e}")
        return 'pyrogram'


async def set_download_engine(user_id, engine: str):
    if engine not in ('pyrogram', 'telethon'):
        raise ValueError(f"Invalid engine: {engine!r}. Must be 'pyrogram' or 'telethon'.")
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                'UPDATE users SET download_engine = $1, updated_at = $2 WHERE telegram_id = $3',
                engine, datetime.now(), int(user_id)
            )
        logger.info(f"Download engine set to '{engine}' for user {user_id}")
    except Exception as e:
        logger.error(f"Error setting download_engine for {user_id}: {e}")


# ── Payment dedup ─────────────────────────────────────────────────────────────

async def claim_payment_dedup(dedup_key: str) -> bool:
    """
    Atomically insert dedup_key into payment_dedup.
    Returns True  → first time seen, caller should proceed with upgrade.
    Returns False → already processed (duplicate webhook), caller should skip.
    Falls back to True on DB error so a payment is never silently lost.
    """
    _require_pool()
    try:
        async with pool.acquire() as conn:
            result = await conn.execute(
                "INSERT INTO payment_dedup (dedup_key) VALUES ($1) "
                "ON CONFLICT (dedup_key) DO NOTHING",
                dedup_key,
            )
        # asyncpg returns "INSERT 0 1" on success, "INSERT 0 0" on conflict
        return result == "INSERT 0 1"
    except Exception as e:
        logger.error(f"claim_payment_dedup error (key={dedup_key!r}): {e} — allowing upgrade")
        return True  # fail-open: better to double-upgrade than to miss a payment
