from sqlalchemy import create_engine, Column, String, Integer, Boolean, DateTime, Date
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
import datetime
import os

DATABASE_URL = os.environ.get("DATABASE_URL")
if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

class User(Base):
    __tablename__ = "users"
    telegram_id = Column(String, primary_key=True, index=True)
    role = Column(String, default="free")
    downloads_today = Column(Integer, default=0)
    last_download_date = Column(Date, default=datetime.date.today)
    is_agreed_terms = Column(Boolean, default=False)
    phone_session_string = Column(String, nullable=True)
    premium_expiry_date = Column(Date, nullable=True)
    is_banned = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

class Setting(Base):
    __tablename__ = "settings"
    key = Column(String, primary_key=True, index=True)
    value = Column(String, nullable=True)
    json_value = Column(String, nullable=True)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow)

def init_db():
    Base.metadata.create_all(bind=engine)
    print("PostgreSQL initialized.")

def get_db():
    return SessionLocal()

# --- Users ---

def get_user(user_id):
    db = get_db()
    try:
        return db.query(User).filter(User.telegram_id == str(user_id)).first()
    finally:
        db.close()

def create_user(user_id):
    db = get_db()
    try:
        user = User(telegram_id=str(user_id))
        db.add(user)
        db.commit()
        db.refresh(user)
        return user
    finally:
        db.close()

def update_user_terms(user_id, agreed=True):
    db = get_db()
    try:
        db.query(User).filter(User.telegram_id == str(user_id)).update({"is_agreed_terms": agreed})
        db.commit()
    finally:
        db.close()

def save_session_string(user_id, session_string):
    db = get_db()
    try:
        db.query(User).filter(User.telegram_id == str(user_id)).update({"phone_session_string": session_string})
        db.commit()
    finally:
        db.close()

def set_user_role(user_id, role, duration_days=None):
    db = get_db()
    try:
        expiry_date = None
        if role == 'premium' and duration_days:
            expiry_date = datetime.date.today() + datetime.timedelta(days=int(duration_days))
        
        db.query(User).filter(User.telegram_id == str(user_id)).update({
            "role": role, 
            "premium_expiry_date": expiry_date
        })
        db.commit()
    finally:
        db.close()

def ban_user(user_id, is_banned=True):
    db = get_db()
    try:
        db.query(User).filter(User.telegram_id == str(user_id)).update({"is_banned": is_banned})
        db.commit()
    finally:
        db.close()

# --- Quota ---

def check_and_update_quota(user_id):
    db = get_db()
    try:
        user = db.query(User).filter(User.telegram_id == str(user_id)).first()
        if not user:
            return False, "User not found."
            
        if user.is_banned:
            return False, "You are banned from using this bot."

        today = datetime.date.today()
        
        # Check Premium Expiry
        if user.role == 'premium' and user.premium_expiry_date:
            if user.premium_expiry_date < today:
                user.role = "free"
                user.premium_expiry_date = None
                db.commit()
                
        # Bypass for privileged roles
        if user.role in ['premium', 'admin', 'owner']:
            return True, "Unlimited"

        # Reset if new day
        if user.last_download_date < today:
            user.downloads_today = 0
            user.last_download_date = today
            db.commit()

        # Check limit (5 per day for free)
        if user.downloads_today >= 5:
            return False, "Daily limit reached (5/5). Upgrade to Premium for unlimited downloads."

        return True, f"{user.downloads_today}/5"
    finally:
        db.close()

def increment_quota(user_id):
    db = get_db()
    try:
        db.query(User).filter(User.telegram_id == str(user_id)).update({"downloads_today": User.downloads_today + 1})
        db.commit()
    finally:
        db.close()

# --- Settings ---

def get_setting(key):
    db = get_db()
    try:
        return db.query(Setting).filter(Setting.key == key).first()
    finally:
        db.close()

def update_setting(key, value, json_value=None):
    db = get_db()
    try:
        setting = db.query(Setting).filter(Setting.key == key).first()
        if setting:
            setting.value = value
            setting.json_value = json_value
            setting.updated_at = datetime.datetime.utcnow()
        else:
            setting = Setting(key=key, value=value, json_value=json_value)
            db.add(setting)
        db.commit()
    finally:
        db.close()
