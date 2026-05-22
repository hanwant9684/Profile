import os
import logging
import asyncio
import base64
from datetime import datetime
from bot.config import get_shared_client

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

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_file = f"backup_{timestamp}.sql"

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

        client = await get_shared_client()
        response = await client.put(url, json=data, headers=headers)
        if response.status_code == 201:
            logger.info(f"Uploaded PostgreSQL backup to GitHub: {file_path}")
            try:
                list_url = f"https://api.github.com/repos/{repo}/contents/backups"
                list_resp = await client.get(list_url, headers=headers)
                if list_resp.status_code == 200:
                    backups = list_resp.json()
                    if len(backups) > 2:
                        backups.sort(key=lambda x: x['name'], reverse=True)
                        for old_backup in backups[2:]:
                            del_url = f"https://api.github.com/repos/{repo}/contents/{old_backup['path']}"
                            del_data = {
                                "message": f"Deleting old backup: {old_backup['name']}",
                                "sha": old_backup['sha']
                            }
                            del_resp = await client.request("DELETE", del_url, json=del_data, headers=headers)
                            if del_resp.status_code == 200:
                                logger.info(f"Deleted old backup: {old_backup['name']}")
            except Exception as e:
                logger.error(f"Failed to clean up old backups: {e}")
            return True
        else:
            logger.error(f"GitHub upload failed: {response.status_code}")
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

        client = await get_shared_client()
        list_url = f"https://api.github.com/repos/{repo}/contents/backups"
        response = await client.get(list_url, headers=headers)
        if response.status_code != 200:
            return False
        backups = response.json()

        if not backups:
            return False
        latest = sorted(backups, key=lambda x: x['name'], reverse=True)[0]
        download_url = latest['download_url']

        response = await client.get(download_url, headers=headers)
        if response.status_code != 200:
            return False
        backup_content = response.content

        temp_path = "temp_restore.sql"
        try:
            with open(temp_path, "wb") as f:
                f.write(backup_content)

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
            if os.path.exists(temp_path):
                os.remove(temp_path)
    except Exception as e:
        logger.error(f"GitHub restore failed: {e}")
        return False


async def periodic_cloud_backup(interval_minutes=60):
    backup_service = os.getenv("CLOUD_BACKUP_SERVICE", "").lower()
    if backup_service != "github":
        return
    while True:
        await asyncio.sleep(interval_minutes * 60)
        await backup_to_github_async()
