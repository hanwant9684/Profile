# Telegram Downloader Bot

## Overview

A Telegram bot built with Python that enables users to download content from Telegram links. The bot features a role-based access system (free/premium), daily download quotas, user authentication via Telegram sessions, and admin controls for bot management.

For **public channel links** (e.g. `t.me/channel/123`), the bot uses direct extraction via `msg.copy()` / `copy_media_group()` — Telegram's servers handle the transfer with no download/upload needed. Private/restricted links still use the full download-then-upload path via the user's logged-in session.

## Quick Setup (New Replit Import)

When importing to a new Replit:
1. Dependencies are installed via pip from `requirements.txt`
2. Add required secrets in the Secrets tab (see below)
3. Click Run

## Required Secrets

Add these in Replit Secrets tab:
| Variable | Purpose | How to get |
|----------|---------|------------|
| `API_ID` | Telegram API ID | https://my.telegram.org |
| `API_HASH` | Telegram API Hash | https://my.telegram.org |
| `BOT_TOKEN` | Bot token | @BotFather on Telegram |
| `OWNER_ID` | Your Telegram user ID | @userinfobot on Telegram |
| `DUMP_CHANNEL_ID` | Channel ID for backups (optional) | Channel settings |

### Optional Secrets (Cloud Backup)
| Variable | Purpose | How to get |
|----------|---------|------------|
| `CLOUD_BACKUP_SERVICE` | Set to "github" to enable | Just type "github" |
| `GITHUB_TOKEN` | GitHub personal access token | GitHub Settings > Developer settings > PAT |
| `GITHUB_BACKUP_REPO` | Repository for backups (format: username/repo) | Create a private repo |

## User Preferences

Preferred communication style: Simple, everyday language.

## System Architecture

### Bot Framework
- **Framework**: pyrofork (Pyrogram fork) — imports as `pyrogram` namespace, installed as `pyrofork` package
- **Entry Point**: `main.py` initializes the database and starts the bot
- **Modular Design**: Handlers are split across multiple files and imported to register with the bot client

### Module Structure
| Module | Purpose |
|--------|---------|
| `bot/config.py` | Environment variables, bot client initialization, global state (semaphores, active downloads) |
| `bot/database.py` | SQLite database connection and user/settings CRUD operations |
| `bot/handlers.py` | Main download link processing and force-subscribe verification |
| `bot/batch.py` | `/batch` and `/mlinks` commands (Premium bulk download), `/cancelbatch` |
| `bot/login.py` | User onboarding, terms acceptance, and Telegram session authentication |
| `bot/admin.py` | Owner-only commands for stats, user management, and process control |
| `bot/info.py` | User info and quota display commands |
| `bot/cloud_backup.py` | GitHub cloud backup - auto restore on startup, periodic backups, critical change backups |
| `bot/ads.py` | Advertisement handling |
| `bot/transfer.py` | File transfer logic |
| `bot/logger.py` | Logging configuration |

### Workflow
- **Start Bot** — runs `python main.py` (console output)

### Concurrency Control
- Global semaphore limits concurrent downloads to 4 (`MAX_CONCURRENT_DOWNLOADS`)
- Active download tracking via `active_downloads` set to prevent duplicate processes per user
- Admin can kill stuck processes via `/killall` command

### User Management
- **Roles**: `free` (5 downloads/day quota) and `premium` (unlimited, with expiry date)
- **Terms Agreement**: Required before bot usage
- **Phone Session**: Users can login with their Telegram account for extended functionality
- **Ban System**: Users can be banned by admin

### Download Features
- **Media Group Support**: When a link points to a message in a media group, ALL files in that group are automatically downloaded with a single link
- **Quota-Aware Downloading**: Free users are limited by their remaining daily quota
- **Video Streaming**: Videos are uploaded with proper thumbnail, duration, width/height for streaming playback
- **Progress Tracking**: Real-time progress bars show download/upload status for each file

### Data Models (SQLite)
Users table stores:
- `telegram_id`, `role`, `downloads_today`, `last_download_date`
- `is_agreed_terms`, `phone_session_string`, `premium_expiry_date`
- `is_banned`, `created_at`

Settings table stores key-value pairs (e.g., `force_sub_channel`)

## Dependencies (Python)
- `pyrofork` — Pyrogram fork for Telegram (imports as `pyrogram` namespace)
- `aiofiles`, `aiohttp` — async file/HTTP operations
- `asyncpg` — async PostgreSQL client
- `certifi` — SSL certificates
- `flask` — web server for health checks
- `psutil` — system/process utilities
- `python-dotenv` — environment variable loading
- `pyyaml` — YAML parsing
- `redis` — Redis client
- `tgcrypto` — fast crypto for Telegram
- `uvloop` — fast event loop
