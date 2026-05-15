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
- **Framework**: pyrofork (Pyrogram fork by Mayuri-Chan) — imports as `pyrogram` namespace, installed as `pyrofork` package
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

### Per-User Bot Upload Architecture
For all restricted/private content downloads, **each user must register their own @BotFather bot** via `/setbot <token>`. Their bot performs the actual upload — the shared owner bot only routes commands and never uploads bytes. This isolates each user's Telegram per-bot quota from other users (no shared FloodWait propagation), keeps the owner's bot account permanently uncapped, and avoids ever shifting heavy upload load onto the user's irreplaceable userbot account.

- `/setbot <token>` — register/replace the user's upload bot. The token is validated by spinning up a probe Client; `bot_token` column persists in the `users` table.
- `/rembot` — stop and evict the cached bot Client and clear the saved token.
- Public-link `msg.copy()` / `copy_media_group()` paths still use the shared owner bot (no bytes flow through it — Telegram's servers do the copy).
- Per-user bot Clients are lazily instantiated on first upload and cached in `bot.config.user_bots`. They run with `no_updates=True` (no update loop), `max_concurrent_transmissions=10`, `sleep_threshold=30`.
- The Cloud Storage channel resolver adds the **user's own bot** as channel admin (not the owner's bot) via the user's userbot.
- Fallback to the user's userbot for upload only triggers on **size-limit** / `FILE_PARTS_INVALID` errors (i.e. >2 GB files where bots physically can't upload). FloodWait and PEER_FLOOD are absorbed by the user's bot — never shifted to their userbot account.

### Data Models (SQLite)
Users table stores:
- `telegram_id`, `role`, `downloads_today`, `last_download_date`
- `is_agreed_terms`, `phone_session_string`, `premium_expiry_date`
- `is_banned`, `created_at`

Settings table stores key-value pairs (e.g., `force_sub_channel`)

## Dependencies (Python)
- `pyrofork` — Pyrogram fork (Mayuri-Chan) for Telegram, imports as `pyrogram` namespace.
- `aiofiles`, `aiohttp` — async file/HTTP operations
- `asyncpg` — async PostgreSQL client
- `certifi` — SSL certificates
- `flask` — web server for health checks
- `psutil` — system/process utilities
- `python-dotenv` — environment variable loading
- `pyyaml` — YAML parsing
- `redis` — Redis client
- `tgcrypto-pyrofork` — fast crypto for Telegram (pyrofork-bundled fork)
- `pymediainfo-pyrofork` — media metadata (pyrofork-bundled fork)
- `uvloop` — fast event loop
