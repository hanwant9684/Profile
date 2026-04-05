# Telegram Downloader Bot

## Overview

A Telegram bot built with Python that enables users to download content from Telegram links. The bot features a role-based access system (free/premium), daily download quotas, user authentication via Telegram sessions, and admin controls for bot management.

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
- **Framework**: Kurigram (Pyrogram fork) via `pyrotgfork` for Telegram Bot API interaction
- **Entry Point**: `main.py` initializes the database and starts the bot
- **Modular Design**: Handlers are split across multiple files and imported to register with the bot client

### Module Structure
| Module | Purpose |
|--------|---------|
| `bot/config.py` | Environment variables, bot client initialization, global state (semaphores, active downloads) |
| `bot/database.py` | SQLite database connection and user/settings CRUD operations |
| `bot/handlers.py` | Main download link processing and force-subscribe verification |
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

## Automatic Payment System

The bot supports three payment gateways. Once configured, premium upgrades are fully automatic — no manual steps required.

### How It Works
1. User runs `/upgrade` → selects a plan → selects payment method
2. Bot generates a **unique payment link** with the user's Telegram ID embedded
3. User pays → gateway sends a signed webhook to the bot's server
4. Bot verifies the signature, then upgrades the user and sends a Telegram notification

### Gateways

| Gateway | Use For | Webhook URL |
|---------|---------|-------------|
| PayPal | Credit Card, Apple Pay, PayPal | `https://yourdomain.com/webhook/paypal` |
| Razorpay | UPI (India) | `https://yourdomain.com/webhook/razorpay` |
| OxaPay | Crypto (BTC, ETH, USDT…) | `https://yourdomain.com/webhook/oxapay` |

PayPal payments are captured at `https://yourdomain.com/paypal/return` (no webhook needed for basic flow).

### New Secrets Required

| Secret | Purpose | Where to get it |
|--------|---------|----------------|
| `WEBHOOK_BASE_URL` | Your VPS public URL (no trailing slash) | e.g. `https://bot.example.com` |
| `WEBHOOK_PORT` | Port for webhook server (default: `8080`) | Set to `8080` or any free port |
| `PAYPAL_CLIENT_ID` | PayPal app client ID | developer.paypal.com → My Apps |
| `PAYPAL_CLIENT_SECRET` | PayPal app client secret | developer.paypal.com → My Apps |
| `PAYPAL_WEBHOOK_ID` | PayPal webhook ID (for signature verification) | PayPal Developer Dashboard → Webhooks |
| `PAYPAL_MODE` | `live` or `sandbox` | Set to `live` for production |
| `RAZORPAY_KEY_ID` | Razorpay API key | dashboard.razorpay.com → Settings → API Keys |
| `RAZORPAY_KEY_SECRET` | Razorpay API secret | dashboard.razorpay.com → Settings → API Keys |
| `RAZORPAY_WEBHOOK_SECRET` | Razorpay webhook secret | dashboard.razorpay.com → Settings → Webhooks |
| `OXAPAY_MERCHANT_API_KEY` | OxaPay merchant key | oxapay.com → Dashboard → Merchant API |

### VPS Nginx Config (expose webhook server)
```nginx
server {
    listen 443 ssl;
    server_name yourdomain.com;
    # ... ssl certs ...
    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```

### Security
- PayPal: RSA X.509 certificate signature verification (local, no API call)
- Razorpay: HMAC-SHA256 with webhook secret
- OxaPay: HMAC-SHA512 with merchant API key
- Idempotency: each payment ID is stored; duplicate webhooks are ignored

### New Files
| File | Purpose |
|------|---------|
| `bot/payments.py` | Creates payment orders/links/invoices for all three gateways |
| `bot/webhook_server.py` | Flask server handling webhook callbacks securely |
| `bot/payments_db.py` | `payment_sessions` table — tracks processed payments |

## Dependencies (Python)
- `pyrotgfork` — Pyrogram fork (Kurigram) for Telegram
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
