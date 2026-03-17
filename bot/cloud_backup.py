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
            'pg_dump', '-d', str(DATABASE_URL), '-f', backup_file,
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
                    # Keep only 2 latest files
                    try:
                        list_url = f"https://api.github.com/repos/{repo}/contents/backups"
                        async with session.get(list_url, headers=headers) as list_resp:
                            if list_resp.status == 200:
                                backups = await list_resp.json()
                                if len(backups) > 2:
                                    # Sort by name (which has timestamp) and delete older ones
                                    backups.sort(key=lambda x: x['name'], reverse=True)
                                    for old_backup in backups[2:]:
                                        del_url = f"https://api.github.com/repos/{repo}/contents/{old_backup['path']}"
                                        del_data = {
                                            "message": f"Deleting old backup: {old_backup['name']}",
                                            "sha": old_backup['sha']
                                        }
                                        async with session.delete(del_url, json=del_data, headers=headers) as del_resp:
                                            if del_resp.status == 200:
                                                logger.info(f"Deleted old backup: {old_backup['name']}")
                    except Exception as e:
                        logger.error(f"Failed to clean up old backups: {e}")
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
                'psql', '-d', str(DATABASE_URL), '-f', temp_path,
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
