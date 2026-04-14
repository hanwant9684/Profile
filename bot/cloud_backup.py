import os
import logging
import asyncio
import aiohttp
import base64
import json
from datetime import datetime
import subprocess

logger = logging.getLogger(__name__)

DATABASE_URL = os.getenv("DATABASE_URL")

async def migrate_sqlite_to_postgres(sqlite_db_path):
    """
    Migrate data from SQLite backup file to PostgreSQL.
    This is a one-time helper to move data from a .db file to your Postgres database.
    """
    import sqlite3
    import asyncpg
    
    if not os.path.exists(sqlite_db_path):
        logger.error(f"SQLite file not found: {sqlite_db_path}")
        return False
        
    try:
        sqlite_conn = sqlite3.connect(sqlite_db_path)
        sqlite_conn.row_factory = sqlite3.Row
        
        pg_conn = await asyncpg.connect(DATABASE_URL)
        
        # Migrate Users
        users = sqlite_conn.execute("SELECT * FROM users").fetchall()
        for user in users:
            user_dict = dict(user)
            # Ensure telegram_id is an integer
            try:
                tel_id = int(user_dict.get('telegram_id'))
            except (ValueError, TypeError):
                logger.warning(f"Skipping user with invalid ID: {user_dict.get('telegram_id')}")
                continue

            await pg_conn.execute('''
                INSERT INTO users (telegram_id, role, downloads_today, last_download_date, 
                                   is_agreed_terms, phone_session_string, premium_expiry_date, is_banned)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                ON CONFLICT (telegram_id) DO UPDATE SET
                    role = EXCLUDED.role,
                    downloads_today = EXCLUDED.downloads_today,
                    last_download_date = EXCLUDED.last_download_date,
                    is_agreed_terms = EXCLUDED.is_agreed_terms,
                    phone_session_string = EXCLUDED.phone_session_string,
                    premium_expiry_date = EXCLUDED.premium_expiry_date,
                    is_banned = EXCLUDED.is_banned
            ''', 
            tel_id,
            user_dict.get('role', 'free'),
            user_dict.get('downloads_today', 0),
            datetime.strptime(user_dict['last_download_date'], '%Y-%m-%d').date() if user_dict.get('last_download_date') else None,
            bool(user_dict.get('is_agreed_terms')),
            user_dict.get('phone_session_string'),
            datetime.fromisoformat(user_dict['premium_expiry_date']) if user_dict.get('premium_expiry_date') else None,
            bool(user_dict.get('is_banned'))
            )
            
        # Migrate Settings
        settings = sqlite_conn.execute("SELECT * FROM settings").fetchall()
        for setting in settings:
            s_dict = dict(setting)
            await pg_conn.execute('''
                INSERT INTO settings (key, value)
                VALUES ($1, $2)
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
            ''', s_dict.get('key'), s_dict.get('value'))
            
        await pg_conn.close()
        sqlite_conn.close()
        logger.info("Successfully migrated SQLite data to PostgreSQL")
        return True
    except Exception as e:
        logger.error(f"Migration failed: {e}")
        return False

async def backup_to_github_async():
    """Backup PostgreSQL database to GitHub"""
    try:
        token = os.getenv("GITHUB_TOKEN")
        repo = os.getenv("GITHUB_BACKUP_REPO")
        
        if not token or not repo:
            logger.error("GITHUB_TOKEN or GITHUB_BACKUP_REPO not set")
            return False

        # Use pg_dump to create a backup
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_file = f"backup_{timestamp}.sql"
        
        # pg_dump -d $DATABASE_URL -f $backup_file
        process = await asyncio.create_subprocess_exec(
            'pg_dump', '-d', DATABASE_URL, '-f', backup_file,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await process.communicate()
        
        if process.returncode != 0:
            logger.error(f"pg_dump failed: {stderr.decode()}")
            return False

        try:
            with open(backup_file, "rb") as f:
                content = base64.b64encode(f.read()).decode()
        finally:
            if os.path.exists(backup_file):
                os.remove(backup_file)

        file_path = f"backups/{backup_file}"
        url = f"https://api.github.com/repos/{repo}/contents/{file_path}"
        data = {"message": f"Automated PostgreSQL backup - {timestamp}", "content": content}
        headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json"}

        async with aiohttp.ClientSession() as session:
            async with session.put(url, json=data, headers=headers) as response:
                if response.status == 201:
                    logger.info(f"Uploaded PostgreSQL backup to GitHub: {file_path}")
                    return True
                else:
                    logger.error(f"GitHub upload failed: {response.status}")
                    return False
    except Exception as e:
        logger.error(f"GitHub backup failed: {e}")
        return False

async def restore_from_github_async():
    """Restore PostgreSQL database from latest GitHub backup"""
    try:
        token = os.getenv("GITHUB_TOKEN")
        repo = os.getenv("GITHUB_BACKUP_REPO")
        
        if not token or not repo:
            logger.error("GITHUB_TOKEN or GITHUB_BACKUP_REPO not set")
            return False

        headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json"}
        
        async with aiohttp.ClientSession() as session:
            list_url = f"https://api.github.com/repos/{repo}/contents/backups"
            async with session.get(list_url, headers=headers) as response:
                if response.status != 200: return False
                backups = await response.json()
            
            if not backups: return False
            latest = sorted(backups, key=lambda x: x['name'], reverse=True)[0]
            download_url = latest['download_url']
            
            async with session.get(download_url, headers=headers) as response:
                if response.status != 200: return False
                backup_content = await response.read()

        temp_path = "temp_restore.sql"
        try:
            with open(temp_path, "wb") as f:
                f.write(backup_content)
            
            # Use psql to restore
            # psql -d $DATABASE_URL -f $temp_path
            process = await asyncio.create_subprocess_exec(
                'psql', '-d', DATABASE_URL, '-f', temp_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await process.communicate()
            
            if process.returncode != 0:
                logger.error(f"psql restore failed: {stderr.decode()}")
                return False
            
            logger.info("Database restored from GitHub")
            return True
        finally:
            if os.path.exists(temp_path): os.remove(temp_path)
    except Exception as e:
        logger.error(f"GitHub restore failed: {e}")
        return False

async def periodic_cloud_backup(interval_minutes=10):
    backup_service = os.getenv("CLOUD_BACKUP_SERVICE", "").lower()
    if backup_service != "github": return
    while True:
        await asyncio.sleep(interval_minutes * 60)
        await backup_to_github_async()
