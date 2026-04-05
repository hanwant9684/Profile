import os
import hmac
import hashlib
import base64
import zlib
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

# ── Pricing Plans ─────────────────────────────────────────────────────────────

PREMIUM_PLANS = {
    "10d":  {"days": 10,  "usd": 2.00,  "inr": 200,  "label": "10 Days  — $2 / ₹200"},
    "30d":  {"days": 30,  "usd": 3.00,  "inr": 300,  "label": "30 Days  — $3 / ₹300"},
    "60d":  {"days": 60,  "usd": 6.00,  "inr": 600,  "label": "60 Days  — $6 / ₹600"},
    "365d": {"days": 365, "usd": 30.00, "inr": 3000, "label": "1 Year   — $30 / ₹3000"},
}

# ── PayPal ────────────────────────────────────────────────────────────────────

PAYPAL_CLIENT_ID     = os.environ.get("PAYPAL_CLIENT_ID", "")
PAYPAL_CLIENT_SECRET = os.environ.get("PAYPAL_CLIENT_SECRET", "")
PAYPAL_WEBHOOK_ID    = os.environ.get("PAYPAL_WEBHOOK_ID", "")
PAYPAL_MODE          = os.environ.get("PAYPAL_MODE", "live")  # "sandbox" or "live"


def _paypal_base_url():
    if PAYPAL_MODE == "sandbox":
        return "https://api-m.sandbox.paypal.com"
    return "https://api-m.paypal.com"


async def _paypal_get_token(session) -> str:
    import aiohttp
    url = f"{_paypal_base_url()}/v1/oauth2/token"
    async with session.post(
        url,
        data={"grant_type": "client_credentials"},
        auth=aiohttp.BasicAuth(PAYPAL_CLIENT_ID, PAYPAL_CLIENT_SECRET),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    ) as resp:
        data = await resp.json()
        if "access_token" not in data:
            raise Exception(f"PayPal token error: {data}")
        return data["access_token"]


async def create_paypal_order(
    telegram_id: int,
    plan_key: str,
    days: int,
    amount_usd: float,
    return_url: str,
    cancel_url: str,
) -> dict:
    from bot.config import get_shared_session
    session = await get_shared_session()
    token = await _paypal_get_token(session)

    # Gross-up: customer covers PayPal's 4.4% + $0.50 fee so you receive full plan amount
    paypal_amount = round((amount_usd + 0.50) / (1 - 0.044), 2)

    payload = {
        "intent": "CAPTURE",
        "purchase_units": [{
            "custom_id": f"{telegram_id}_{plan_key}_{days}",
            "description": f"Premium {days} days subscription",
            "amount": {
                "currency_code": "USD",
                "value": f"{paypal_amount:.2f}",
            },
        }],
        "application_context": {
            "return_url": return_url,
            "cancel_url": cancel_url,
            "brand_name": "Premium Bot",
            "user_action": "PAY_NOW",
            "landing_page": "BILLING",
        },
    }

    async with session.post(
        f"{_paypal_base_url()}/v2/checkout/orders",
        json=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    ) as resp:
        data = await resp.json()

    if resp.status != 201:
        raise Exception(f"PayPal order creation failed ({resp.status}): {data}")

    approval_url = next(
        (link["href"] for link in data.get("links", []) if link["rel"] == "approve"),
        None,
    )
    return {"order_id": data["id"], "approval_url": approval_url}


async def capture_paypal_order(order_id: str) -> dict:
    from bot.config import get_shared_session
    session = await get_shared_session()
    token = await _paypal_get_token(session)

    async with session.post(
        f"{_paypal_base_url()}/v2/checkout/orders/{order_id}/capture",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        json={},
    ) as resp:
        return await resp.json()


def verify_paypal_webhook_signature(
    transmission_id: str,
    transmission_time: str,
    webhook_id: str,
    raw_body: bytes,
    cert_url: str,
    transmission_sig: str,
    auth_algo: str = "SHA256withRSA",
) -> bool:
    import requests
    from cryptography import x509
    from cryptography.hazmat.backends import default_backend
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.exceptions import InvalidSignature

    trusted_prefixes = (
        "https://api.paypal.com",
        "https://api.sandbox.paypal.com",
        "https://api-m.paypal.com",
        "https://api-m.sandbox.paypal.com",
    )
    if not cert_url.startswith(trusted_prefixes):
        logger.error(f"Untrusted PayPal cert_url: {cert_url}")
        return False

    try:
        if not hasattr(verify_paypal_webhook_signature, "_cert_cache"):
            verify_paypal_webhook_signature._cert_cache = {}

        cache = verify_paypal_webhook_signature._cert_cache
        if cert_url not in cache:
            resp = requests.get(cert_url, timeout=10)
            resp.raise_for_status()
            cache[cert_url] = resp.content

        cert_pem = cache[cert_url]
        crc = zlib.crc32(raw_body)
        message = f"{transmission_id}|{transmission_time}|{webhook_id}|{crc}"
        signature = base64.b64decode(transmission_sig)

        cert = x509.load_pem_x509_certificate(cert_pem, default_backend())
        public_key = cert.public_key()

        algo_map = {
            "SHA256withRSA": hashes.SHA256(),
            "SHA1withRSA": hashes.SHA1(),
        }
        hash_algo = algo_map.get(auth_algo, hashes.SHA256())
        public_key.verify(signature, message.encode("utf-8"), padding.PKCS1v15(), hash_algo)
        return True
    except InvalidSignature:
        return False
    except Exception as e:
        logger.error(f"PayPal webhook verification error: {e}")
        return False


# ── Razorpay ──────────────────────────────────────────────────────────────────
RAZORPAY_KEY_ID        = os.environ.get("RAZORPAY_KEY_ID", "")
RAZORPAY_KEY_SECRET    = os.environ.get("RAZORPAY_KEY_SECRET", "")
RAZORPAY_WEBHOOK_SECRET = os.environ.get("RAZORPAY_WEBHOOK_SECRET", "")


async def create_razorpay_payment_link(
    telegram_id: int,
    plan_key: str,
    days: int,
    amount_inr: int,
) -> dict:
    import aiohttp as _aiohttp
    from bot.config import get_shared_session
    session = await get_shared_session()

    ts = int(datetime.now().timestamp())

    payload = {
        "amount": amount_inr * 100,
        "currency": "INR",
        "description": f"Premium {days} days",
        "reference_id": f"tg_{telegram_id}_{ts}",
        "notes": {
            "telegram_id": str(telegram_id),
            "plan_key": plan_key,
            "days": str(days),
        },
        "reminder_enable": False,
        "callback_method": "get",
    }
    auth = _aiohttp.BasicAuth(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET)
    async with session.post(
        "https://api.razorpay.com/v1/payment_links",
        json=payload,
        auth=auth,
    ) as resp:
        data = await resp.json()

    if resp.status != 200:
        raise Exception(f"Razorpay payment link failed ({resp.status}): {data}")
        
    return {"payment_link_id": data["id"], "short_url": data["short_url"]}
def verify_razorpay_webhook(raw_body: bytes, signature: str) -> bool:
    if not RAZORPAY_WEBHOOK_SECRET:
        logger.warning("RAZORPAY_WEBHOOK_SECRET not set — skipping verification")
        return False
    expected = hmac.new(
        RAZORPAY_WEBHOOK_SECRET.encode("utf-8"),
        raw_body,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


# ── OxaPay ────────────────────────────────────────────────────────────────────

OXAPAY_MERCHANT_API_KEY = os.environ.get("OXAPAY_MERCHANT_API_KEY", "")


async def create_oxapay_invoice(
    telegram_id: int,
    plan_key: str,
    days: int,
    amount_usd: float,
    callback_url: str,
) -> dict:
    from bot.config import get_shared_session
    session = await get_shared_session()

    payload = {
        "merchant": OXAPAY_MERCHANT_API_KEY,
        "amount": amount_usd,
        "currency": "USD",
        "lifeTime": 30,
        "feePaidByPayer": 0,
        "orderId": f"{telegram_id}_{plan_key}_{days}",
        "description": f"Premium {days} days",
        "callbackUrl": callback_url,
    }

    async with session.post(
        "https://api.oxapay.com/merchants/request",
        json=payload,
    ) as resp:
        data = await resp.json()

    if data.get("result") != 100:
        raise Exception(f"OxaPay invoice creation failed: {data}")

    return {"track_id": str(data["trackId"]), "pay_link": data["payLink"]}


def verify_oxapay_webhook(raw_body: bytes, signature: str) -> bool:
    if not OXAPAY_MERCHANT_API_KEY:
        logger.warning("OXAPAY_MERCHANT_API_KEY not set — skipping verification")
        return False
    expected = hmac.new(
        OXAPAY_MERCHANT_API_KEY.encode("utf-8"),
        raw_body,
        hashlib.sha512,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)
