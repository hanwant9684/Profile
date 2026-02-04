import os
import sqlite3
import logging
import asyncio
from datetime import datetime, timedelta
from typing import Optional, Dict, List
from bot.config import OWNER_ID

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

DATABASE_PATH = os.environ.get("DATABASE_PATH", "telegram_bot.db")

db_lock = asyncio.Lock()
_db_initialized = False

def _get_connection():
    conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA cache_size=-64000")
    conn.execute("PRAGMA temp_store=MEMORY")
    return conn

def init_db():
    global _db_initialized
    if _db_initialized:
        return
    
    try:
        conn = _get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                telegram_id TEXT PRIMARY KEY,
                role TEXT DEFAULT 'free',
                downloads_today INTEGER DEFAULT 0,
                last_download_date TEXT,
                is_agreed_terms INTEGER DEFAULT 0,
                phone_session_string TEXT,
                premium_expiry_date TEXT,
                is_banned INTEGER DEFAULT 0,
                ads_today INTEGER DEFAULT 0,
                last_ad_date TEXT,
                created_at TEXT,
                updated_at TEXT
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT,
                json_value TEXT,
                updated_at TEXT
            )
        ''')
        
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_users_role ON users(role)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_users_banned ON users(is_banned)')
        
        conn.commit()
        conn.close()
            
        _db_initialized = True
        logger.info(f"SQLite database initialized: {DATABASE_PATH}")
    except Exception as e:
        logger.error(f"SQLite initialization error: {e}")
        raise

async def get_user(user_id) -> Optional[Dict]:
    try:
        async with db_lock:
            conn = _get_connection()
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM users WHERE telegram_id = ?', (str(user_id),))
            row = cursor.fetchone()
            conn.close()
        
        if row:
            user = dict(row)
            user['is_banned'] = bool(user['is_banned'])
            user['is_agreed_terms'] = bool(user['is_agreed_terms'])
            
            return user
        
        if OWNER_ID and str(user_id) == str(OWNER_ID):
            user = await create_user(user_id)
            if user:
                await set_user_role(user_id, "owner")
                user["role"] = "owner"
            return user
        
        return None
    except Exception as e:
        logger.error(f"Error getting user {user_id}: {e}")
        return None

async def create_user(user_id) -> Optional[Dict]:
    try:
        now = datetime.utcnow().isoformat()
        today = datetime.utcnow().date().isoformat()
        
        async with db_lock:
            conn = _get_connection()
            cursor = conn.cursor()
            
            cursor.execute('SELECT 1 FROM users WHERE telegram_id = ?', (str(user_id),))
            if cursor.fetchone():
                conn.close()
                # Instead of calling get_user which acquires lock again, 
                # we return the user dict structure directly if it exists.
                # In a real app we might want to fetch, but for registration 
                # this is fine or we should fetch without lock.
                return {
                    "telegram_id": str(user_id),
                    "role": "free", # Default, will be updated by caller if needed
                    "downloads_today": 0,
                    "last_download_date": today,
                    "is_agreed_terms": False,
                    "phone_session_string": None,
                    "premium_expiry_date": None,
                    "is_banned": False,
                    "created_at": now
                }
            
            cursor.execute('''
                INSERT INTO users (telegram_id, role, downloads_today, last_download_date, 
                                   is_agreed_terms, is_banned, ads_today, created_at, updated_at)
                VALUES (?, 'free', 0, ?, 0, 0, 0, ?, ?)
            ''', (str(user_id), today, now, now))
            conn.commit()
            conn.close()
        
        return {
            "telegram_id": str(user_id),
            "role": "free",
            "downloads_today": 0,
            "last_download_date": today,
            "is_agreed_terms": False,
            "phone_session_string": None,
            "premium_expiry_date": None,
            "is_banned": False,
            "created_at": now
        }
    except Exception as e:
        logger.error(f"Error creating user {user_id}: {e}")
        return None

async def update_user_terms(user_id, agreed=True):
    try:
        async with db_lock:
            conn = _get_connection()
            cursor = conn.cursor()
            cursor.execute('UPDATE users SET is_agreed_terms = ?, updated_at = ? WHERE telegram_id = ?',
                           (1 if agreed else 0, datetime.utcnow().isoformat(), str(user_id)))
            conn.commit()
            conn.close()
    except Exception as e:
        logger.error(f"Error updating terms for {user_id}: {e}")

async def save_session_string(user_id, session_string):
    try:
        async with db_lock:
            conn = _get_connection()
            cursor = conn.cursor()
            cursor.execute('UPDATE users SET phone_session_string = ?, updated_at = ? WHERE telegram_id = ?',
                           (session_string, datetime.utcnow().isoformat(), str(user_id)))
            conn.commit()
            conn.close()
        logger.info(f"Saved session for user {user_id}")
    except Exception as e:
        logger.error(f"Error saving session for {user_id}: {e}")

async def logout_user(user_id):
    try:
        async with db_lock:
            conn = _get_connection()
            cursor = conn.cursor()
            cursor.execute('UPDATE users SET phone_session_string = NULL, updated_at = ? WHERE telegram_id = ?',
                           (datetime.utcnow().isoformat(), str(user_id)))
            conn.commit()
            conn.close()
        logger.info(f"User {user_id} logged out")
    except Exception as e:
        logger.error(f"Error logging out user {user_id}: {e}")

async def set_user_role(user_id, role, duration_days=None):
    try:
        expiry_date = None
        if role == 'premium' and duration_days:
            expiry_date = (datetime.utcnow() + timedelta(days=int(duration_days))).isoformat()
        
        async with db_lock:
            conn = _get_connection()
            cursor = conn.cursor()
            cursor.execute('UPDATE users SET role = ?, premium_expiry_date = ?, updated_at = ? WHERE telegram_id = ?',
                           (role, expiry_date, datetime.utcnow().isoformat(), str(user_id)))
            conn.commit()
            conn.close()
    except Exception as e:
        logger.error(f"Error setting role for {user_id}: {e}")

async def ban_user(user_id, is_banned=True):
    try:
        async with db_lock:
            conn = _get_connection()
            cursor = conn.cursor()
            cursor.execute('UPDATE users SET is_banned = ?, updated_at = ? WHERE telegram_id = ?',
                           (1 if is_banned else 0, datetime.utcnow().isoformat(), str(user_id)))
            conn.commit()
            conn.close()
    except Exception as e:
        logger.error(f"Error banning user {user_id}: {e}")

async def check_and_update_quota(user_id):
    try:
        user = await get_user(user_id)
        if not user:
            return False, "User not found."
        
        if user.get("is_banned"):
            return False, "You are banned from using this bot."
        
        today = datetime.utcnow().date().isoformat()
        
        if user.get("role") == 'premium' and user.get("premium_expiry_date"):
            if user["premium_expiry_date"] < today:
                await set_user_role(user_id, "free")
                user["role"] = "free"
        
        if user.get("role") in ['premium', 'admin', 'owner']:
            return True, "Unlimited"
        
        if user.get("last_download_date") != today:
            async with db_lock:
                conn = _get_connection()
                cursor = conn.cursor()
                cursor.execute('UPDATE users SET downloads_today = 0, last_download_date = ? WHERE telegram_id = ?',
                               (today, str(user_id)))
                conn.commit()
                conn.close()
            user["downloads_today"] = 0
        
        if user.get("downloads_today", 0) >= 5:
            return False, "Daily limit reached (5/5). Upgrade to Premium for unlimited downloads."
        
        return True, f"{user.get('downloads_today', 0)}/5"
    except Exception as e:
        logger.error(f"Error checking quota for {user_id}: {e}")
        return False, "Database error."

async def increment_quota(user_id, count=1):
    try:
        async with db_lock:
            conn = _get_connection()
            cursor = conn.cursor()
            cursor.execute('UPDATE users SET downloads_today = downloads_today + ? WHERE telegram_id = ?',
                           (count, str(user_id)))
            conn.commit()
            conn.close()
    except Exception as e:
        logger.error(f"Error incrementing quota for {user_id}: {e}")

async def increment_ad_count(user_id):
    try:
        today = datetime.utcnow().date().isoformat()
        async with db_lock:
            conn = _get_connection()
            cursor = conn.cursor()
            cursor.execute('UPDATE users SET ads_today = ads_today + 1, last_ad_date = ? WHERE telegram_id = ?',
                           (today, str(user_id)))
            conn.commit()
            conn.close()
    except Exception as e:
        logger.error(f"Error incrementing ad count for {user_id}: {e}")

async def get_ad_count_today(user_id):
    try:
        user = await get_user(user_id)
        if not user:
            return 0
        
        today = datetime.utcnow().date().isoformat()
        if user.get("last_ad_date") != today:
            async with db_lock:
                conn = _get_connection()
                cursor = conn.cursor()
                cursor.execute('UPDATE users SET ads_today = 0, last_ad_date = ? WHERE telegram_id = ?',
                               (today, str(user_id)))
                conn.commit()
                conn.close()
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
        
        today = datetime.utcnow().date().isoformat()
        downloads_today = user.get("downloads_today", 0)
        
        if user.get("last_download_date") != today:
            downloads_today = 0
        
        remaining = max(0, 5 - downloads_today)
        return remaining, False
    except Exception as e:
        logger.error(f"Error getting remaining quota for {user_id}: {e}")
        return 0, False

async def get_setting(key):
    try:
        async with db_lock:
            conn = _get_connection()
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM settings WHERE key = ?', (key,))
            row = cursor.fetchone()
            conn.close()
        
        if row:
            return dict(row)
        return None
    except Exception as e:
        logger.error(f"Error getting setting {key}: {e}")
        return None

async def update_setting(key, value, json_value=None):
    try:
        async with db_lock:
            conn = _get_connection()
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO settings (key, value, json_value, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = ?, json_value = ?, updated_at = ?
            ''', (key, value, json_value, datetime.utcnow().isoformat(),
                  value, json_value, datetime.utcnow().isoformat()))
            conn.commit()
            conn.close()
    except Exception as e:
        logger.error(f"Error updating setting {key}: {e}")

async def get_all_users() -> List[Dict]:
    try:
        async with db_lock:
            conn = _get_connection()
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM users')
            rows = cursor.fetchall()
            conn.close()
        
        users = []
        for row in rows:
            user = dict(row)
            user['is_banned'] = bool(user['is_banned'])
            user['is_agreed_terms'] = bool(user['is_agreed_terms'])
            users.append(user)
        return users
    except Exception as e:
        logger.error(f"Error getting all users: {e}")
        return []

async def get_user_count():
    try:
        async with db_lock:
            conn = _get_connection()
            cursor = conn.cursor()
            cursor.execute('SELECT COUNT(*) FROM users')
            count = cursor.fetchone()[0]
            conn.close()
        return count
    except Exception as e:
        logger.error(f"Error getting user count: {e}")
        return 0
