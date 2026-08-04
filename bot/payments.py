"""
Payment gateway integrations: ZapUPI (UPI/India), Oxapay (Crypto), PayPal (Card/Apple Pay).

Each create_* function retries up to 3 times with backoff so transient
gateway errors never show an error to the user.
"""
import asyncio
import os
import time
import logging
import httpx

logger = logging.getLogger(__name__)

# ── Plan definitions ──────────────────────────────────────────────────────────
PLANS: dict[str, dict] = {
    "10":  {"days": 10,  "usd": 3.00,  "inr": 10,   "label": "10 days"},
    "30":  {"days": 30,  "usd": 4.00,  "inr": 400,  "label": "30 days"},
    "60":  {"days": 60,  "usd": 8.00,  "inr": 800,  "label": "60 days"},
    "90":  {"days": 90,  "usd": 12.00, "inr": 1200, "label": "90 days"},
    "365": {"days": 365, "usd": 45.00, "inr": 4500, "label": "1 Year"},
}

# ── Env vars ──────────────────────────────────────────────────────────────────
ZAPUPI_MERCHANT_KEY   = os.environ.get("ZAPUPI_MERCHANT_KEY", "")

OXAPAY_MERCHANT_KEY   = os.environ.get("OXAPAY_MERCHANT_KEY", "")

PAYPAL_CLIENT_ID        = os.environ.get("PAYPAL_CLIENT_ID", "")
PAYPAL_CLIENT_SECRET    = os.environ.get("PAYPAL_CLIENT_SECRET", "")
PAYPAL_WEBHOOK_ID       = os.environ.get("PAYPAL_WEBHOOK_ID", "")

PAYPAL_BASE = "https://api-m.paypal.com"

WEBHOOK_BASE_URL = os.environ.get("WEBHOOK_BASE_URL", "https://wolfy004bot.duckdns.org")


def _bot_username() -> str:
    """Always return the live value set by main.py after app.start()."""
    from bot.config import BOT_USERNAME
    return BOT_USERNAME or os.environ.get("BOT_USERNAME", "")


# Keep a module-level alias for places that do `from bot.payments import BOT_USERNAME`
BOT_USERNAME = _bot_username()


# ── Order ID helpers ──────────────────────────────────────────────────────────
def make_order_id(user_id: int, days: int) -> str:
    """Unique order ID that encodes user + plan: tg_{user_id}_{days}_{ts}"""
    return f"tg_{user_id}_{days}_{int(time.time())}"


def parse_order_id(order_id: str) -> tuple[int | None, int | None]:
    """Parse order_id → (user_id, days). Returns (None, None) on failure."""
    try:
        parts = order_id.split("_")
        if parts[0] == "tg" and len(parts) >= 4:
            return int(parts[1]), int(parts[2])
    except Exception:
        pass
    return None, None


# ── ZapUPI (UPI / India) ──────────────────────────────────────────────────────
# API base: https://pay.zapupi.com  (docs: https://zapupi.com/docs)
_ZAPUPI_API_BASE = "https://pay.zapupi.com"


async def create_zapupi_payment(user_id: int, days: int) -> dict:
    plan = PLANS[str(days)]
    order_id = make_order_id(user_id, days)

    last_err = "Unknown error"
    for attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=10.0)) as client:
                resp = await client.post(
                    f"{_ZAPUPI_API_BASE}/api/create-order",
                    headers={"Content-Type": "application/json"},
                    json={
                        # ZapUPI create-order supported fields:
                        # zap_key, order_id, amount, customer_mobile, remark,
                        # cashier_id, webhook_url, success_url, failed_url, timeout_url
                        "zap_key":      ZAPUPI_MERCHANT_KEY,
                        "order_id":     order_id,
                        "amount":       float(plan["inr"]),   # API expects a float, not a string
                        "remark":       f"WolfyBot Premium {days}d | {user_id}",
                        # Per-order webhook override — ZapUPI POSTs to this URL on payment.
                        # Returning HTTP 200 immediately (no blocking sub-call) keeps us
                        # well within ZapUPI's 10-second response window.
                        "webhook_url":  f"{WEBHOOK_BASE_URL}/webhook/zapupi",
                        # Redirect user back to Telegram after payment instead of
                        # ZapUPI's panel (which shows a 404 with no success_url set).
                        "success_url":  f"https://t.me/{_bot_username()}",
                        "failed_url":   f"https://t.me/{_bot_username()}",
                        "timeout_url":  f"https://t.me/{_bot_username()}",
                    },
                )
            data = resp.json()
            logger.info(f"ZapUPI create-order response (attempt {attempt + 1}): {data}")
            # ZapUPI returns status "success" (lowercase) on success
            if data.get("status") == "success" and data.get("payment_url"):
                return {
                    "ok": True,
                    "url": data["payment_url"],
                    "order_id": order_id,
                }
            last_err = data.get("message") or data.get("error") or str(data)
            logger.warning(f"ZapUPI create-order non-success on attempt {attempt + 1}/3: {last_err}")
            if resp.status_code < 500:
                break
        except (httpx.TimeoutException, httpx.NetworkError) as e:
            last_err = f"Network error: {e}"
            logger.warning(f"ZapUPI attempt {attempt + 1}/3 network error: {e}")
        except Exception as e:
            last_err = str(e)
            logger.error(f"ZapUPI attempt {attempt + 1}/3 unexpected error: {e}")
            break

        if attempt < 2:
            await asyncio.sleep(1.5 * (attempt + 1))

    logger.error(f"ZapUPI create_payment failed after 3 attempts: {last_err}")
    return {"ok": False, "error": last_err}


async def check_zapupi_order_status(order_id: str) -> dict:
    """
    Confirm a ZapUPI payment server-side via the order-status API.
    Docs: POST https://pay.zapupi.com/api/order-status

    Returns the full response dict, or {"status": "error"} on failure.
    Fields: status, amount, pay_amount, txn_id, utr, environment
    """
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=10.0)) as client:
            resp = await client.post(
                f"{_ZAPUPI_API_BASE}/api/order-status",
                headers={"Content-Type": "application/json"},
                json={"zap_key": ZAPUPI_MERCHANT_KEY, "order_id": order_id},
            )
        return resp.json()
    except Exception as e:
        logger.error(f"ZapUPI order-status check failed for {order_id}: {e}")
        return {"status": "error", "message": str(e)}


# ── Oxapay (Crypto) ───────────────────────────────────────────────────────────
async def create_oxapay_invoice(user_id: int, days: int) -> dict:
    """
    Create an Oxapay crypto invoice (BTC, ETH, USDT, and 100+ coins).
    Retries up to 3 times on network/5xx errors.
    Returns {ok, url, order_id} or {ok: False, error}.
    """
    plan = PLANS[str(days)]
    order_id = make_order_id(user_id, days)

    last_err = "Unknown error"
    for attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=10.0)) as client:
                resp = await client.post(
                    "https://api.oxapay.com/v1/payment/invoice",
                    headers={
                        "merchant_api_key": OXAPAY_MERCHANT_KEY,
                        "Content-Type": "application/json",
                    },
                    json={
                        "amount": plan["usd"],
                        "currency": "USD",
                        "lifetime": 60,
                        "callback_url": f"{WEBHOOK_BASE_URL}/webhook/oxapay",
                        "return_url": f"https://t.me/{_bot_username()}",
                        "order_id": order_id,
                        "description": f"Wolfy Bot Premium — {plan['label']}",
                        "thanks_message": "Premium activated! Return to Telegram.",
                    }
                )
            data = resp.json()
            if data.get("status") == 200:
                return {"ok": True, "url": data["data"]["payment_url"], "order_id": order_id}
            last_err = data.get("message", f"Oxapay error (status={data.get('status')})")
            if resp.status_code < 500:
                break
            logger.warning(f"Oxapay 5xx on attempt {attempt + 1}/3: {last_err}")
        except (httpx.TimeoutException, httpx.NetworkError) as e:
            last_err = f"Network error: {e}"
            logger.warning(f"Oxapay attempt {attempt + 1}/3 network error: {e}")
        except Exception as e:
            last_err = str(e)
            logger.error(f"Oxapay attempt {attempt + 1}/3 unexpected error: {e}")
            break

        if attempt < 2:
            await asyncio.sleep(1.5 * (attempt + 1))

    logger.error(f"Oxapay create_invoice failed after 3 attempts: {last_err}")
    return {"ok": False, "error": last_err}


# ── PayPal ────────────────────────────────────────────────────────────────────
async def get_paypal_token() -> str | None:
    """Fetch a PayPal OAuth token. Retries once on failure."""
    for attempt in range(2):
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=10.0)) as client:
                resp = await client.post(
                    f"{PAYPAL_BASE}/v1/oauth2/token",
                    auth=(PAYPAL_CLIENT_ID, PAYPAL_CLIENT_SECRET),
                    data={"grant_type": "client_credentials"},
                )
            token = resp.json().get("access_token")
            if token:
                return token
        except Exception as e:
            logger.error(f"PayPal token attempt {attempt + 1}/2 error: {e}")
        if attempt == 0:
            await asyncio.sleep(1.5)
    return None


def paypal_total(base_usd: float) -> float:
    """
    Calculate the total amount charged via PayPal including processing fees:
      $0.45 fixed fee + 10% of the base price.
    Rounded to 2 decimal places.
    """
    return round(base_usd * 1.10 + 0.45, 2)


async def create_paypal_order(user_id: int, days: int, charge_amount: float | None = None) -> dict:
    """
    Create a PayPal order (PayPal balance, credit/debit cards, Apple Pay, Google Pay).
    Retries up to 3 times on network/5xx errors.
    Returns {ok, url, paypal_order_id} or {ok: False, error}.

    charge_amount: the amount to actually charge (with fees). If None, uses plan['usd'].
    """
    plan = PLANS[str(days)]
    amount = charge_amount if charge_amount is not None else plan["usd"]
    custom_id = f"tg_{user_id}_{days}"

    last_err = "Unknown error"
    for attempt in range(3):
        try:
            token = await get_paypal_token()
            if not token:
                last_err = "PayPal authentication failed"
                await asyncio.sleep(1.5)
                continue

            async with httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=10.0)) as client:
                resp = await client.post(
                    f"{PAYPAL_BASE}/v2/checkout/orders",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "intent": "CAPTURE",
                        "purchase_units": [{
                            "amount": {
                                "currency_code": "USD",
                                "value": f"{amount:.2f}",
                            },
                            "description": f"Wolfy Bot Premium — {plan['label']}",
                            "custom_id": custom_id,
                        }],
                        "application_context": {
                            "return_url": f"{WEBHOOK_BASE_URL}/paypal/return",
                            "cancel_url": f"https://t.me/{_bot_username()}",
                            "brand_name": "Wolfy Bot Premium",
                            "user_action": "PAY_NOW",
                            "shipping_preference": "NO_SHIPPING",
                        },
                    }
                )
            data = resp.json()
            approve_url = next(
                (lnk["href"] for lnk in data.get("links", []) if lnk["rel"] == "approve"),
                None
            )
            if approve_url:
                return {"ok": True, "url": approve_url, "paypal_order_id": data["id"]}
            last_err = f"No approval URL: {data}"
            if resp.status_code < 500:
                break
            logger.warning(f"PayPal 5xx on attempt {attempt + 1}/3: {last_err}")
        except (httpx.TimeoutException, httpx.NetworkError) as e:
            last_err = f"Network error: {e}"
            logger.warning(f"PayPal attempt {attempt + 1}/3 network error: {e}")
        except Exception as e:
            last_err = str(e)
            logger.error(f"PayPal attempt {attempt + 1}/3 unexpected error: {e}")
            break

        if attempt < 2:
            await asyncio.sleep(1.5 * (attempt + 1))

    logger.error(f"PayPal create_order failed after 3 attempts: {last_err}")
    return {"ok": False, "error": last_err}


async def capture_paypal_order(paypal_order_id: str) -> dict:
    """Capture a PayPal order after the user approves it."""
    token = await get_paypal_token()
    if not token:
        return {"ok": False, "error": "PayPal auth failed"}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=10.0)) as client:
            resp = await client.post(
                f"{PAYPAL_BASE}/v2/checkout/orders/{paypal_order_id}/capture",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
            )
        data = resp.json()
        if data.get("status") == "COMPLETED":
            units = data.get("purchase_units", [{}])
            unit = units[0] if units else {}
            captures = unit.get("payments", {}).get("captures", [])
            capture = captures[0] if captures else {}
            custom_id = unit.get("custom_id", "") or capture.get("custom_id", "")
            capture_id = capture.get("id", "")
            logger.info(f"PayPal capture OK — custom_id={custom_id!r} capture_id={capture_id!r} order={paypal_order_id}")
            return {"ok": True, "custom_id": custom_id, "capture_id": capture_id}
        logger.error(f"PayPal capture non-COMPLETED: {data}")
        return {"ok": False, "data": data}
    except Exception as e:
        logger.error(f"PayPal capture error: {e}")
        return {"ok": False, "error": str(e)}
