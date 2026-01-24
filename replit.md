# Telegram Downloader Bot

## Overview

A Telegram bot built with Python that enables users to download content from Telegram links. The bot features a role-based access system (free/premium), daily download quotas, user authentication via Telegram sessions, and admin controls for bot management.

## User Preferences

Preferred communication style: Simple, everyday language.

## System Architecture

### Bot Framework
- **Framework**: Hydrogram (Pyrogram fork) for Telegram Bot API interaction
- **Entry Point**: `src/main.py` initializes the database and starts the bot
- **Modular Design**: Handlers are split across multiple files and imported to register with the bot client

### Module Structure
| Module | Purpose |
|--------|---------|
| `config.py` | Environment variables, bot client initialization, global state (semaphores, active downloads) |
| `database.py` | MongoDB connection and user/settings CRUD operations |
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

### Data Models
Users collection stores:
- `telegram_id`, `role`, `downloads_today`, `last_download_date`
- `is_agreed_terms`, `phone_session_string`, `premium_expiry_date`
- `is_banned`, `created_at`

Settings collection stores key-value pairs (e.g., `force_sub_channel`)

## External Dependencies

### Database
- **MongoDB**: Primary data store for users and settings
- Connection via `pymongo` with TLS certificate bypass option
- Database name extracted from connection URL or defaults to `telegram_downloader`

### Telegram API
- **Hydrogram Client**: Requires `API_ID`, `API_HASH`, `BOT_TOKEN` environment variables
- User session strings stored for authenticated downloads

### Required Environment Variables
| Variable | Purpose |
|----------|---------|
| `API_ID` | Telegram API ID |
| `API_HASH` | Telegram API Hash |
| `BOT_TOKEN` | Bot token from BotFather |
| `OWNER_ID` | Admin user's Telegram ID |
| `MONGO_URL` | MongoDB connection string |
| `DUMP_CHANNEL_ID` | Channel for dumping downloaded content |

### Note on TypeScript Files
The repository contains `src/build.ts` which appears to be from a different project (web app with Vite/Express). The active codebase is the Python Telegram bot - the TypeScript file should be ignored or removed.