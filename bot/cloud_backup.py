#!/usr/bin/env python3
"""
GitHub Backup Integration for SQLite Database
Automatically backs up database to GitHub repository
"""

import os
import shutil
import sqlite3
import logging
from datetime import datetime
import threading
import aiohttp
import asyncio
import base64
import json

logger = logging.getLogger(__name__)

DB_PATH = os.getenv("DATABASE_PATH", "telegram_bot.db")

_backup_lock = threading.Lock()
_backup_in_progress = False

def _create_temp_backup():
    """Create a temporary backup of the database for GitHub upload (internal use only)"""
    if not os.path.exists(DB_PATH):
        logger.warning(f"Database file not found: {DB_PATH}")
        return None

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    temp_path = f"temp_backup_{timestamp}.db"

    try:
        conn = sqlite3.connect(DB_PATH)
        backup_conn = sqlite3.connect(temp_path)

        with backup_conn:
            conn.backup(backup_conn)

        conn.close()
        backup_conn.close()

        return temp_path
    except Exception as e:
        logger.error(f"Failed to create temp backup: {e}")
        return None

def _restore_from_temp(backup_path):
    """Restore database from a temporary backup file (internal use only)"""
    if not os.path.exists(backup_path):
        logger.error(f"Backup file not found: {backup_path}")
        return False

    try:
        if os.path.exists(DB_PATH):
            backup_current = f"{DB_PATH}.before_restore"
            shutil.copy2(DB_PATH, backup_current)
            logger.info(f"Current database backed up to: {backup_current}")

        shutil.copy2(backup_path, DB_PATH)
        logger.info(f"Database restored from: {backup_path}")
        return True
    except Exception as e:
        logger.error(f"Restore failed: {e}")
        return False

async def cleanup_old_github_backups_async(token, repo, keep_count=2):
    """Delete old backups from GitHub, keeping only the newest ones"""
    try:
        list_url = f"https://api.github.com/repos/{repo}/contents/backups"
        headers = {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github.v3+json"
        }

        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.get(list_url) as response:
                if response.status != 200:
                    return
                backups = await response.json()

            if not backups or len(backups) <= keep_count:
                return

            sorted_backups = sorted(backups, key=lambda x: x['name'], reverse=True)
            backups_to_delete = sorted_backups[keep_count:]

            for backup in backups_to_delete:
                try:
                    delete_url = f"https://api.github.com/repos/{repo}/contents/{backup['path']}"
                    async with session.get(delete_url) as resp:
                        file_data = await resp.json()

                    data = {
                        "message": f"Cleanup: Remove old backup {backup['name']}",
                        "sha": file_data['sha']
                    }
                    async with session.delete(delete_url, json=data) as response:
                        if response.status == 200:
                            logger.info(f"Deleted old backup: {backup['name']}")
                except Exception as e:
                    logger.warning(f"Failed to delete {backup['name']}: {e}")
    except Exception as e:
        logger.warning(f"Cleanup failed: {e}")

async def backup_to_github_async():
    """Upload database backup to GitHub repository"""
    try:
        token = os.getenv("GITHUB_TOKEN")
        repo = os.getenv("GITHUB_BACKUP_REPO")

        if not token or not repo:
            logger.error("GITHUB_TOKEN or GITHUB_BACKUP_REPO not set")
            return False

        temp_backup = _create_temp_backup()
        if not temp_backup:
            return False

        try:
            with open(temp_backup, "rb") as f:
                content = base64.b64encode(f.read()).decode()
        finally:
            if os.path.exists(temp_backup):
                os.remove(temp_backup)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        file_path = f"backups/backup_{timestamp}.db"
        url = f"https://api.github.com/repos/{repo}/contents/{file_path}"
        data = {"message": f"Automated backup - {timestamp}", "content": content}
        headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json"}

        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.put(url, json=data) as response:
                if response.status == 201:
                    logger.info(f"Uploaded to GitHub: {file_path}")
                    await cleanup_old_github_backups_async(token, repo, keep_count=2)
                    return True
                else:
                    logger.error(f"GitHub upload failed: {response.status}")
                    return False
    except Exception as e:
        logger.error(f"GitHub backup failed: {e}")
        return False

def backup_to_github():
    """Synchronous wrapper for backup (if needed for threads)"""
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    
    if loop.is_running():
        # If the loop is already running, we can't use run_until_complete.
        # This shouldn't happen in the thread worker, but for safety:
        asyncio.create_task(backup_to_github_async())
        return True
    return loop.run_until_complete(backup_to_github_async())

def trigger_backup_on_session(user_id):
    """Trigger backup when new user session is created (non-blocking, thread-safe)"""
    global _backup_in_progress

    backup_service = os.getenv("CLOUD_BACKUP_SERVICE", "").lower()
    if backup_service != "github":
        return False

    with _backup_lock:
        if _backup_in_progress:
            logger.debug(f"Backup already in progress, skipping trigger for user {user_id}")
            return False

        _backup_in_progress = True

    def _backup_worker():
        global _backup_in_progress
        try:
            logger.info(f"New session created for user {user_id}, triggering backup...")
            backup_to_github()
        except Exception as e:
            logger.error(f"Session backup failed: {e}")
        finally:
            with _backup_lock:
                _backup_in_progress = False

    thread = threading.Thread(target=_backup_worker, daemon=True, name=f"SessionBackup-{user_id}")
    thread.start()
    return True

def trigger_backup_on_critical_change(operation_name, user_id=None):
    """
    Trigger backup when critical database changes occur (non-blocking, thread-safe)
    """
    global _backup_in_progress

    backup_service = os.getenv("CLOUD_BACKUP_SERVICE", "").lower()
    if backup_service != "github":
        return False

    with _backup_lock:
        if _backup_in_progress:
            logger.debug(f"Backup already in progress, skipping trigger for {operation_name}")
            return False

        _backup_in_progress = True

    def _backup_worker():
        global _backup_in_progress
        try:
            user_info = f" (user {user_id})" if user_id else ""
            logger.info(f"Critical change detected: {operation_name}{user_info}, triggering backup...")
            backup_to_github()
        except Exception as e:
            logger.error(f"Critical change backup failed: {e}")
        finally:
            with _backup_lock:
                _backup_in_progress = False

    thread = threading.Thread(target=_backup_worker, daemon=True, name=f"CriticalBackup-{operation_name}")
    thread.start()
    return True

async def restore_from_github_async(backup_name=None):
    try:
        token = os.getenv("GITHUB_TOKEN")
        repo = os.getenv("GITHUB_BACKUP_REPO")
        if not token or not repo:
            logger.error("GITHUB_TOKEN or GITHUB_BACKUP_REPO not set")
            return False

        headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json"}
        async with aiohttp.ClientSession(headers=headers) as session:
            if not backup_name:
                list_url = f"https://api.github.com/repos/{repo}/contents/backups"
                async with session.get(list_url) as response:
                    if response.status != 200: return False
                    backups = await response.json()
                if not backups: return False
                latest = sorted(backups, key=lambda x: x['name'], reverse=True)[0]
                backup_name = latest['name']
                download_url = latest['download_url']
            else:
                download_url = f"https://raw.githubusercontent.com/{repo}/main/backups/{backup_name}"

            async with session.get(download_url) as response:
                if response.status != 200: return False
                backup_content = await response.read()

        temp_path = "temp_restore.db"
        try:
            with open(temp_path, "wb") as f:
                f.write(backup_content)
            return _restore_from_temp(temp_path)
        finally:
            if os.path.exists(temp_path): os.remove(temp_path)
    except Exception as e:
        logger.error(f"GitHub restore failed: {e}")
        return False

def restore_from_github(backup_name=None):
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop.run_until_complete(restore_from_github_async(backup_name))

async def periodic_cloud_backup(interval_minutes=10):
    backup_service = os.getenv("CLOUD_BACKUP_SERVICE", "").lower()
    if backup_service != "github": return
    while True:
        await asyncio.sleep(interval_minutes * 60)
        await backup_to_github_async()

async def restore_latest_from_cloud():
    backup_service = os.getenv("CLOUD_BACKUP_SERVICE", "").lower()
    if backup_service != "github": return False
    return await restore_from_github_async()

if __name__ == "__main__":
    print("=" * 60)
    print("GitHub Backup Utility")
    print("=" * 60)
    print("\n1. Backup to GitHub")
    print("2. Restore from GitHub")

    choice = input("\nEnter choice (1-2): ").strip()

    if choice == "1":
        backup_to_github()
    elif choice == "2":
        import asyncio
        asyncio.run(restore_latest_from_cloud())
