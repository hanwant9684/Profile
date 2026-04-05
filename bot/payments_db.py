import logging
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)


async def _get_pool():
    from bot.database import pool
    if pool is None:
        raise RuntimeError("Database pool not initialized")
    return pool


async def init_payments_table():
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS payment_sessions (
                payment_id   TEXT PRIMARY KEY,
                telegram_id  BIGINT NOT NULL,
                provider     TEXT NOT NULL,
                plan_days    INTEGER NOT NULL,
                processed    BOOLEAN DEFAULT FALSE,
                created_at   TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                processed_at TIMESTAMP WITH TIME ZONE
            )
        """)
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_ps_telegram ON payment_sessions(telegram_id)"
        )
    logger.info("payment_sessions table ready")


async def is_payment_processed(payment_id: str) -> bool:
    try:
        pool = await _get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT processed FROM payment_sessions WHERE payment_id = $1",
                payment_id,
            )
        return bool(row and row["processed"])
    except Exception as e:
        logger.error(f"is_payment_processed error: {e}")
        return False


async def mark_payment_complete(
    payment_id: str,
    telegram_id: int,
    provider: str,
    plan_days: int,
) -> None:
    try:
        pool = await _get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO payment_sessions
                    (payment_id, telegram_id, provider, plan_days, processed, processed_at)
                VALUES ($1, $2, $3, $4, TRUE, $5)
                ON CONFLICT (payment_id) DO UPDATE
                    SET processed = TRUE, processed_at = $5
                """,
                payment_id,
                telegram_id,
                provider,
                plan_days,
                datetime.now(),
            )
    except Exception as e:
        logger.error(f"mark_payment_complete error: {e}")
