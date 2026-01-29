# Telegram Downloader Bot

## Overview

A Telegram bot built with Python that enables users to download content from Telegram links. The bot features a role-based access system (free/premium), daily download quotas, user authentication via Telegram sessions, and admin controls for bot management.

## Quick Setup (New Replit Import)

When importing to a new Replit:
1. Dependencies auto-install from `pyproject.toml`
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

## Recent Changes

- **2026-01-29**: Added GitHub cloud backup integration for SQLite database (auto-restore on restart, periodic backups every 10 minutes, backup on critical changes)
- **2026-01-29**: Migrated database from MongoDB to SQLite for simplicity and portability. No external database setup required.

## System Architecture

### Bot Framework
- **Framework**: Kurigram (Pyrogram fork) for Telegram Bot API interaction
- **Entry Point**: `main.py` initializes the database and starts the bot
- **Modular Design**: Handlers are split across multiple files and imported to register with the bot client

### Module Structure
| Module | Purpose |
|--------|---------|
| `config.py` | Environment variables, bot client initialization, global state (semaphores, active downloads) |
| `database.py` | SQLite database connection and user/settings CRUD operations |
| `handlers.py` | Main download link processing and force-subscribe verification |
| `login.py` | User onboarding, terms acceptance, and Telegram session authentication |
| `admin.py` | Owner-only commands for stats, user management, and process control |
| `info.py` | User info and quota display commands |
| `cloud_backup.py` | GitHub cloud backup - auto restore on startup, periodic backups, critical change backups |

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
- **Quota-Aware Downloading**: Free users are limited by their remaining daily quota. If a media group has more files than remaining quota, only partial download occurs with an upgrade prompt
- **Video Streaming**: Videos are uploaded with proper thumbnail, duration, width/height for streaming playback
- **Progress Tracking**: Real-time progress bars show download/upload status for each file

### Data Models (SQLite)
Users table stores:
- `telegram_id`, `role`, `downloads_today`, `last_download_date`
- `is_agreed_terms`, `phone_session_string`, `premium_expiry_date`
- `is_banned`, `created_at`

Settings table stores key-value pairs (e.g., `force_sub_channel`)

## External Dependencies

### Database
- **SQLite**: Local file-based database (`telegram_bot.db`)
- No external database setup required - works out of the box

### Telegram API
- **Kurigram Client**: Requires `API_ID`, `API_HASH`, `BOT_TOKEN` environment variables
- User session strings stored for authenticated downloads
