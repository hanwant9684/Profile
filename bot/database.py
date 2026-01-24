import os
import logging
from datetime import datetime, timedelta
from motor.motor_asyncio import AsyncIOMotorClient
from bot.config import MONGO_DB, OWNER_ID

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

DB_NAME = os.environ.get("DB_NAME", "telegram_downloader")

mongo_client = None
db = None
users_collection = None
settings_collection = None
premium_users_collection = None

def init_db():
    global mongo_client, db, users_collection, settings_collection, premium_users_collection
    
    if not MONGO_DB:
        print("WARNING: MONGO_DB not set.")
        return
    
    mongo_client = AsyncIOMotorClient(MONGO_DB)
    db = mongo_client[DB_NAME]
    users_collection = db["users"]
    settings_collection = db["settings"]
    premium_users_collection = db["premium_users"]
    print("MongoDB initialized.")

async def get_user(user_id):
    if users_collection is None:
        return None
    try:
        user = await users_collection.find_one({"telegram_id": str(user_id)})
        
        # Automatically promote to owner if ID matches
        if OWNER_ID and str(user_id) == str(OWNER_ID):
            if not user:
                user = await create_user(user_id)
            if user.get("role") != "owner":
                await users_collection.update_one(
                    {"telegram_id": str(user_id)},
                    {"$set": {"role": "owner"}}
                )
                user["role"] = "owner"
        return user
    except Exception as e:
        logger.error(f"Error getting user {user_id}: {e}")
        return None

async def create_user(user_id):
    if users_collection is None:
        return None
    try:
        user_data = {
            "telegram_id": str(user_id),
            "role": "free",
            "downloads_today": 0,
            "last_download_date": datetime.utcnow().date().isoformat(),
            "is_agreed_terms": False,
            "phone_session_string": None,
            "premium_expiry_date": None,
            "is_banned": False,
            "created_at": datetime.utcnow()
        }
        await users_collection.insert_one(user_data)
        return user_data
    except Exception as e:
        logger.error(f"Error creating user {user_id}: {e}")
        return None

async def update_user_terms(user_id, agreed=True):
    if users_collection is None:
        return
    try:
        await users_collection.update_one(
            {"telegram_id": str(user_id)},
            {"$set": {"is_agreed_terms": agreed}}
        )
    except Exception as e:
        logger.error(f"Error updating terms for {user_id}: {e}")

async def save_session_string(user_id, session_string):
    if users_collection is None:
        return
    try:
        await users_collection.update_one(
            {"telegram_id": str(user_id)},
            {"$set": {"phone_session_string": session_string, "updated_at": datetime.utcnow()}}
        )
        logger.info(f"Saved session for user {user_id}")
    except Exception as e:
        logger.error(f"Error saving session for {user_id}: {e}")

async def set_user_role(user_id, role, duration_days=None):
    if users_collection is None:
        return
    try:
        expiry_date = None
        if role == 'premium' and duration_days:
            expiry_date = (datetime.utcnow() + timedelta(days=int(duration_days))).isoformat()
        
        await users_collection.update_one(
            {"telegram_id": str(user_id)},
            {"$set": {"role": role, "premium_expiry_date": expiry_date}}
        )
    except Exception as e:
        logger.error(f"Error setting role for {user_id}: {e}")

async def ban_user(user_id, is_banned=True):
    if users_collection is None:
        return
    try:
        await users_collection.update_one(
            {"telegram_id": str(user_id)},
            {"$set": {"is_banned": is_banned}}
        )
    except Exception as e:
        logger.error(f"Error banning user {user_id}: {e}")

async def check_and_update_quota(user_id):
    if users_collection is None:
        return False, "Database not connected."
    try:
        user = await users_collection.find_one({"telegram_id": str(user_id)})
        if not user:
            return False, "User not found."
            
        if user.get("is_banned"):
            return False, "You are banned from using this bot."

        today = datetime.utcnow().date().isoformat()
        
        if user.get("role") == 'premium' and user.get("premium_expiry_date"):
            if user["premium_expiry_date"] < today:
                await users_collection.update_one(
                    {"telegram_id": str(user_id)},
                    {"$set": {"role": "free", "premium_expiry_date": None}}
                )
                user["role"] = "free"
                
        if user.get("role") in ['premium', 'admin', 'owner']:
            return True, "Unlimited"

        if user.get("last_download_date") != today:
            await users_collection.update_one(
                {"telegram_id": str(user_id)},
                {"$set": {"downloads_today": 0, "last_download_date": today}}
            )
            user["downloads_today"] = 0

        if user.get("downloads_today", 0) >= 5:
            return False, "Daily limit reached (5/5). Upgrade to Premium for unlimited downloads."

        return True, f"{user.get('downloads_today', 0)}/5"
    except Exception as e:
        logger.error(f"Error checking quota for {user_id}: {e}")
        return False, "Database error."

async def increment_quota(user_id):
    if users_collection is None:
        return
    try:
        await users_collection.update_one(
            {"telegram_id": str(user_id)},
            {"$inc": {"downloads_today": 1}}
        )
    except Exception as e:
        logger.error(f"Error incrementing quota for {user_id}: {e}")

async def get_setting(key):
    if settings_collection is None:
        return None
    try:
        return await settings_collection.find_one({"key": key})
    except Exception as e:
        logger.error(f"Error getting setting {key}: {e}")
        return None

async def update_setting(key, value, json_value=None):
    if settings_collection is None:
        return
    try:
        await settings_collection.update_one(
            {"key": key},
            {"$set": {"value": value, "json_value": json_value, "updated_at": datetime.utcnow()}},
            upsert=True
        )
    except Exception as e:
        logger.error(f"Error updating setting {key}: {e}")

async def get_all_users():
    if users_collection is None:
        return []
    try:
        users = await users_collection.find().to_list(length=None)
        return users
    except Exception as e:
        logger.error(f"Error getting all users: {e}")
        return []

async def get_user_count():
    if users_collection is None:
        return 0
    try:
        return await users_collection.count_documents({})
    except Exception as e:
        logger.error(f"Error getting user count: {e}")
        return 0
