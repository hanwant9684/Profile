"""
Payment gateway integrations: Razorpay (UPI/India), Oxapay (Crypto), PayPal (Card/Apple Pay).
"""
import os
import time
import logging
import httpx

logger = logging.getLogger(__name__)

# ── Plan definitions ──────────────────────────────────────────────────────────
PLANS: dict[str, dict] = {
    "10":  {"days": 10,  "usd": 3.00,  "inr": 300,  "label": "10 days"},
    "30":  {"days": 30,  "usd": 4.00,  "inr": 400,  "label": "30 days"},
    "60":  {"days": 60,  "usd": 8.00,  "inr": 800,  "label": "60 days"},
    "90":  {"days": 90,  "usd": 12.00, "inr": 1200, "label": "90 days"},
    "365": {"days": 365, "usd": 45.00, "inr": 4500, "label": "1 Year"},
}

# ── Env vars ──────────────────────────────────────────────────────────────────
RAZORPAY_KEY_ID        = os.environ.get("RAZORPAY_KEY_ID", "")
RAZORPAY_KEY_SECRET    = os.environ.get("RAZORPAY_KEY_SECRET", "")
RAZORPAY_WEBHOOK_SECRET= os.environ.get("RAZORPAY_WEBHOOK_SECRET", "")

OXAPAY_MERCHANT_KEY    = os.environ.get("OXAPAY_MERCHANT_KEY", "")

PAYPAL_CLIENT_ID       = os.environ.get("PAYPAL_CLIENT_ID", "")
PAYPAL_CLIENT_SECRET   = os.environ.get("PAYPAL_CLIENT_SECRET", "")
PAYPAL_WEBHOOK_ID      = os.environ.get("PAYPAL_WEBHOOK_ID", "")

# Set PAYPAL_SANDBOX=true to use PayPal sandbox (test) environment.
# Use sandbox credentials from developer.paypal.com → Sandbox → Apps & Credentials.
_paypal_sandbox        = os.environ.get("PAYPAL_SANDBOX", "false").lower() == "true"
PAYPAL_BASE = "https://api-m.sandbox.paypal.com" if _paypal_sandbox else "https://api-m.paypal.com"

WEBHOOK_BASE_URL       = os.environ.get("WEBHOOK_BASE_URL", "https://wolfy004bot.duckdns.org")

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


# ── Razorpay ──────────────────────────────────────────────────────────────────
async def create_razorpay_payment_link(user_id: int, days: int) -> dict:
    """
    Create a Razorpay Payment Link for UPI / all Indian payment methods.
    Returns {ok, url, link_id} or {ok: False, error}.

    Razorpay fees: 0% for first 90 days, then 2%.
    The link works with: UPI (GPay, PhonePe, Paytm, BHIM), Cards, Net Banking, Wallets.
    """
    plan = PLANS[str(days)]
    amount_paise = plan["inr"] * 100  # Razorpay uses paise (1 INR = 100 paise)

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                "https://api.razorpay.com/v1/payment_links",
                auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET),
                json={
                    "amount": amount_paise,
                    "currency": "INR",
                    "accept_partial": False,
                    "description": f"Wolfy Bot Premium — {plan['label']}",
                    "notes": {
                        "telegram_id": str(user_id),
                        "days": str(days),
                    },
                    "notify": {"sms": False, "email": False},
                    "reminder_enable": False,
                    "expire_by": int(time.time()) + 3600,  # 1-hour expiry
                }
            )
        data = resp.json()
        if "short_url" in data:
            return {"ok": True, "url": data["short_url"], "link_id": data["id"]}
        err = data.get("error", {}).get("description", str(data))
        return {"ok": False, "error": err}
    except Exception as e:
        logger.error(f"Razorpay create_payment_link error: {e}")
        return {"ok": False, "error": str(e)}


# ── Oxapay (Crypto) ───────────────────────────────────────────────────────────
async def create_oxapay_invoice(user_id: int, days: int) -> dict:
    """
    Create an Oxapay crypto invoice (BTC, ETH, USDT, and 100+ coins).
    Returns {ok, url, order_id} or {ok: False, error}.
    """
    plan = PLANS[str(days)]
    order_id = make_order_id(user_id, days)
    try:
        async with httpx.AsyncClient(timeout=15) as client:
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
        # Oxapay v1 API: status 200 = success, URL is in data["data"]["payment_url"]
        if data.get("status") == 200:
            return {"ok": True, "url": data["data"]["payment_url"], "order_id": order_id}
        return {"ok": False, "error": data.get("message", f"Oxapay error (status={data.get('status')})")}
    except Exception as e:
        logger.error(f"Oxapay create_invoice error: {e}")
        return {"ok": False, "error": str(e)}


# ── PayPal (PayPal balance + Credit/Debit Card + Apple Pay) ──────────────────
async def get_paypal_token() -> str | None:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{PAYPAL_BASE}/v1/oauth2/token",
                auth=(PAYPAL_CLIENT_ID, PAYPAL_CLIENT_SECRET),
                data={"grant_type": "client_credentials"},
            )
        return resp.json().get("access_token")
    except Exception as e:
        logger.error(f"PayPal token error: {e}")
        return None


async def create_paypal_order(user_id: int, days: int) -> dict:
    """
    Create a PayPal order. The approval URL opens PayPal's checkout page
    which supports: PayPal balance, credit/debit cards, Apple Pay, Google Pay.
    Returns {ok, url, paypal_order_id} or {ok: False, error}.
    """
    plan = PLANS[str(days)]
    token = await get_paypal_token()
    if not token:
        return {"ok": False, "error": "PayPal authentication failed"}

    custom_id = f"tg_{user_id}_{days}"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
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
                            "value": f"{plan['usd']:.2f}",
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
        return {"ok": False, "error": f"No approval URL: {data}"}
    except Exception as e:
        logger.error(f"PayPal create_order error: {e}")
        return {"ok": False, "error": str(e)}


async def capture_paypal_order(paypal_order_id: str) -> dict:
    """Capture a PayPal order after the user approves it."""
    token = await get_paypal_token()
    if not token:
        return {"ok": False, "error": "PayPal auth failed"}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
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
            # PayPal puts custom_id at the purchase_unit level AND/OR inside captures
            custom_id = unit.get("custom_id", "") or capture.get("custom_id", "")
            capture_id = capture.get("id", "")
            logger.info(f"PayPal capture OK — custom_id={custom_id!r} capture_id={capture_id!r} order={paypal_order_id}")
            return {"ok": True, "custom_id": custom_id, "capture_id": capture_id}
        logger.error(f"PayPal capture non-COMPLETED: {data}")
        return {"ok": False, "data": data}
    except Exception as e:
        logger.error(f"PayPal capture error: {e}")
        return {"ok": False, "error": str(e)}
