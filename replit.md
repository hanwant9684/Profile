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
| `MONGO_DB` | MongoDB connection string | MongoDB Atlas (free tier) |
| `DB_NAME` | Database name (optional) | Default: telegram_downloader |
| `DUMP_CHANNEL_ID` | Channel ID for backups (optional) | Channel settings |

## User Preferences

Preferred communication style: Simple, everyday language.

## System Architecture

### Bot Framework
- **Framework**: Hydrogram (Pyrogram fork) for Telegram Bot API interaction
- **Entry Point**: `main.py` initializes the database and starts the bot
- **Modular Design**: Handlers are split across multiple files and imported to register with the bot client

### Module Structure
| Module | Purpose |
|--------|---------|
| `config.py` | Environment variables, bot client initialization, global state (semaphores, active downloads) |
| `database.py` | MongoDB connection and user/settings CRUD operations via Motor (async driver) |
| `handlers.py` | Main download link processing and force-subscribe verification |
| `login.py` | User onboarding, terms acceptance, and Telegram session authentication |
| `admin.py` | Owner-only commands for stats, user management, and process control |
| `info.py` | User info and quota display commands |

### Concurrency Control
- Global semaphore limits concurrent downloads to 5 (`MAX_CONCURRENT_DOWNLOADS`)
- Active download tracking via `active_downloads` set to prevent duplicate processes per user
- Admin can kill stuck processes via `/kill` command

### User Management
- **Roles**: `free` (5 downloads/day quota) and `premium` (unlimited, with expiry date)
- **Terms Agreement**: Required before bot usage
- **Phone Session**: Users can login with their Telegram account for extended functionality
- **Ban System**: Users can be banned by admin

### Data Models (MongoDB)
Users collection stores:
- `telegram_id`, `role`, `downloads_today`, `last_download_date`
- `is_agreed_terms`, `phone_session_string`, `premium_expiry_date`
- `is_banned`, `created_at`

Settings collection stores key-value pairs (e.g., `force_sub_channel`)

## External Dependencies

### Database
- **MongoDB**: Primary data store for users and settings
- Connection via Motor (async MongoDB driver)
- Get free MongoDB: https://www.mongodb.com/cloud/atlas

### Telegram API
- **Hydrogram Client**: Requires `API_ID`, `API_HASH`, `BOT_TOKEN` environment variables
- User session strings stored for authenticated downloads
