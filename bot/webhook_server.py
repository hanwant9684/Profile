"""
Flask webhook server — receives payment callbacks from Razorpay, Oxapay, PayPal
and auto-upgrades users via the running Pyrogram bot.

Security fixes applied:
- Dedup is DB-backed (survives restarts, no memory leak)
- PayPal webhooks are signature-verified via PayPal's verify API
- Razorpay rejects requests when no webhook secret is configured
- PayPal /return and /webhook use the same capture_id dedup key (no double-upgrade)
"""
import asyncio
import hashlib
import hmac
import json
import logging
import os
import threading

from flask import Flask, request, jsonify

logger = logging.getLogger(__name__)

flask_app = Flask(__name__)

# Set by start_webhook_thread() after the bot is running
_bot_loop: asyncio.AbstractEventLoop | None = None
_bot_client = None


# ── Init ──────────────────────────────────────────────────────────────────────
def init_webhook(loop: asyncio.AbstractEventLoop, client) -> None:
    global _bot_loop, _bot_client
    _bot_loop = loop
    _bot_client = client
    logger.info("Webhook server: bot context registered")


def _schedule(coro) -> None:
    """Fire-and-forget a coroutine on the bot's event loop."""
    if _bot_loop and _bot_client:
        asyncio.run_coroutine_threadsafe(coro, _bot_loop)
    else:
        logger.warning("Webhook: bot context not ready, upgrade skipped")


# ── Core upgrade logic ────────────────────────────────────────────────────────
async def _upgrade_and_notify(user_id: int, days: int, gateway: str, dedup_key: str) -> None:
    from bot.database import extend_premium, get_user, claim_payment_dedup
    from bot.payments import PLANS
    try:
        # Validate days against known plans — reject injected or tampered values
        if str(days) not in PLANS:
            logger.error(f"Webhook upgrade rejected: days={days} not in PLANS (gateway={gateway}, key={dedup_key})")
            return

        # DB-backed atomic dedup — safe across restarts and concurrent webhooks
        claimed = await claim_payment_dedup(dedup_key)
        if not claimed:
            logger.info(f"Duplicate webhook ignored: {dedup_key}")
            return

        user = await get_user(user_id)
        if not user:
            logger.info(f"Webhook upgrade: user {user_id} not in DB — auto-creating")
            from bot.database import create_user
            await create_user(user_id)

        await extend_premium(user_id, days)
        logger.info(f"Auto-upgraded user={user_id} days={days} gateway={gateway} key={dedup_key}")

        await _bot_client.send_message(
            user_id,
            f"🎉 **Payment Confirmed!**\n\n"
            f"✅ Your account has been upgraded to **Premium** for **{days} days**.\n\n"
            f"You now have:\n"
            f"• ♾ Unlimited downloads\n"
            f"• 📦 Batch up to 50 files\n"
            f"• ⚡ Fast download engine\n"
            f"• 🏷 Caption tools (/capadd · /caprem)\n\n"
            f"Use /myinfo to check your status.\n"
            f"Thank you for your support! 🙏"
        )
    except Exception as e:
        logger.error(f"_upgrade_and_notify error user={user_id}: {e}")


# ── Health check ──────────────────────────────────────────────────────────────
@flask_app.route("/health")
def health():
    return jsonify({"status": "ok", "bot_ready": _bot_client is not None})


# ── Razorpay webhook ──────────────────────────────────────────────────────────
@flask_app.route("/webhook/razorpay", methods=["POST"])
def razorpay_webhook():
    try:
        raw_body = request.get_data()
        signature = request.headers.get("X-Razorpay-Signature", "")

        from bot.payments import RAZORPAY_WEBHOOK_SECRET, PLANS
        if not RAZORPAY_WEBHOOK_SECRET:
            # Reject all requests when no secret is configured — never skip verification
            logger.error("Razorpay webhook received but RAZORPAY_WEBHOOK_SECRET is not set — rejected")
            return jsonify({"status": "rejected", "reason": "webhook secret not configured"}), 403

        expected = hmac.new(
            RAZORPAY_WEBHOOK_SECRET.encode(),
            raw_body,
            hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(expected, signature):
            logger.warning("Razorpay HMAC mismatch — rejected")
            return jsonify({"status": "rejected"}), 400

        data = json.loads(raw_body) if raw_body else {}
        event = data.get("event", "")
        logger.info(f"Razorpay webhook: {event}")

        if event != "payment_link.paid":
            return jsonify({"status": "ok"})

        payment_entity = (
            data.get("payload", {})
                .get("payment", {})
                .get("entity", {})
        )
        # Extract notes from the payment link entity
        notes = (
            data.get("payload", {})
                .get("payment_link", {})
                .get("entity", {})
                .get("notes", {})
        )
        payment_id = payment_entity.get("id", "unknown")

        user_id = int(notes.get("telegram_id", 0))
        days = int(notes.get("days", 0))

        if not user_id or not days:
            logger.warning(f"Razorpay: missing notes in payload — {notes}")
            return jsonify({"status": "ok"})

        # Verify paid amount matches the plan price — reject partial or tampered amounts
        if str(days) in PLANS:
            expected_paise = PLANS[str(days)]["inr"] * 100
            paid_amount = payment_entity.get("amount", 0)
            if paid_amount and paid_amount < expected_paise:
                logger.error(
                    f"Razorpay amount mismatch: expected ₹{PLANS[str(days)]['inr']} "
                    f"({expected_paise} paise) but got {paid_amount} paise — rejected"
                )
                return jsonify({"status": "rejected", "reason": "amount mismatch"}), 400

        _schedule(_upgrade_and_notify(user_id, days, "razorpay", f"rzp_{payment_id}"))
        return jsonify({"status": "ok"})
    except Exception as e:
        logger.error(f"Razorpay webhook exception: {e}")
        return jsonify({"status": "ok"})


# ── Oxapay webhook ────────────────────────────────────────────────────────────
@flask_app.route("/webhook/oxapay", methods=["POST"])
def oxapay_webhook():
    try:
        raw_body = request.get_data()
        hmac_header = request.headers.get("HMAC", "")

        from bot.payments import OXAPAY_MERCHANT_KEY, parse_order_id
        if not OXAPAY_MERCHANT_KEY:
            logger.error("Oxapay webhook received but OXAPAY_MERCHANT_KEY is not set — rejected")
            return jsonify({"status": "rejected", "reason": "merchant key not configured"}), 403

        expected = hmac.new(
            OXAPAY_MERCHANT_KEY.encode(),
            raw_body,
            hashlib.sha512
        ).hexdigest()
        if not hmac.compare_digest(expected, hmac_header):
            logger.warning("Oxapay HMAC mismatch — rejected")
            return jsonify({"status": "rejected"}), 400

        data = json.loads(raw_body) if raw_body else {}
        # Oxapay callback may wrap fields inside "data" key or send them top-level
        payload = data.get("data") if isinstance(data.get("data"), dict) else data
        status = payload.get("status", "")
        order_id = payload.get("order_id", "")
        track_id = payload.get("track_id", order_id)
        logger.info(f"Oxapay webhook: status={status} order={order_id} track={track_id}")

        # Accept "paid" and "manual_accept" (manually confirmed by merchant)
        if status.lower() not in ("paid", "manual_accept"):
            return jsonify({"status": "ok"})

        user_id, days = parse_order_id(order_id)
        if not user_id:
            logger.warning(f"Oxapay: unrecognised order_id={order_id!r}")
            return jsonify({"status": "ok"})

        _schedule(_upgrade_and_notify(user_id, days, "oxapay", f"oxapay_{track_id or order_id}"))
        return jsonify({"status": "ok"})
    except Exception as e:
        logger.error(f"Oxapay webhook exception: {e}")
        return jsonify({"status": "ok"})


# ── PayPal webhook ────────────────────────────────────────────────────────────

def _verify_paypal_webhook_sync(
    headers: dict,
    raw_body: bytes,
    webhook_id: str,
    paypal_base: str,
) -> bool:
    """
    Verify a PayPal webhook using PayPal's own signature-verification endpoint.
    Runs synchronously (called from Flask thread via a fresh event loop).
    Returns True if verified, False otherwise.
    """
    import httpx as _httpx
    from bot.payments import get_paypal_token

    loop = asyncio.new_event_loop()
    try:
        token = loop.run_until_complete(get_paypal_token())
        if not token:
            logger.error("PayPal webhook verify: could not obtain access token")
            return False

        payload = {
            "auth_algo":         headers.get("PAYPAL-AUTH-ALGO", ""),
            "cert_url":          headers.get("PAYPAL-CERT-URL", ""),
            "transmission_id":   headers.get("PAYPAL-TRANSMISSION-ID", ""),
            "transmission_sig":  headers.get("PAYPAL-TRANSMISSION-SIG", ""),
            "transmission_time": headers.get("PAYPAL-TRANSMISSION-TIME", ""),
            "webhook_id":        webhook_id,
            "webhook_event":     json.loads(raw_body),
        }

        async def _call():
            async with _httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    f"{paypal_base}/v1/notifications/verify-webhook-signature",
                    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                    json=payload,
                )
                return resp.json()

        result = loop.run_until_complete(_call())
        status = result.get("verification_status", "")
        if status != "SUCCESS":
            logger.warning(f"PayPal webhook signature not verified: {status} — {result}")
            return False
        return True
    except Exception as e:
        logger.error(f"PayPal webhook verification error: {e}")
        return False
    finally:
        loop.close()


@flask_app.route("/webhook/paypal", methods=["POST"])
def paypal_webhook():
    try:
        raw_body = request.get_data()
        data = json.loads(raw_body) if raw_body else {}
        event_type = data.get("event_type", "")
        logger.info(f"PayPal webhook: {event_type}")

        from bot.payments import PAYPAL_WEBHOOK_ID, PAYPAL_BASE
        if not PAYPAL_WEBHOOK_ID:
            logger.error("PayPal webhook received but PAYPAL_WEBHOOK_ID is not set — rejected")
            return jsonify({"status": "rejected", "reason": "webhook ID not configured"}), 403

        if not _verify_paypal_webhook_sync(dict(request.headers), raw_body, PAYPAL_WEBHOOK_ID, PAYPAL_BASE):
            logger.warning("PayPal webhook signature verification failed — rejected")
            return jsonify({"status": "rejected"}), 400

        if event_type != "PAYMENT.CAPTURE.COMPLETED":
            return jsonify({"status": "ok"})

        resource = data.get("resource", {})
        # Use the capture ID as the canonical dedup key — same key used by /paypal/return
        capture_id = resource.get("id", "")
        custom_id = resource.get("custom_id", "")

        if not custom_id:
            logger.warning("PayPal webhook: no custom_id in resource")
            return jsonify({"status": "ok"})

        parts = custom_id.split("_")
        if len(parts) >= 3 and parts[0] == "tg":
            user_id = int(parts[1])
            days = int(parts[2])
            # dedup key uses capture_id — matches /paypal/return to prevent double-upgrade
            dedup_key = f"paypal_{capture_id}" if capture_id else f"paypal_cid_{custom_id}"
            _schedule(_upgrade_and_notify(user_id, days, "paypal_webhook", dedup_key))

        return jsonify({"status": "ok"})
    except Exception as e:
        logger.error(f"PayPal webhook exception: {e}")
        return jsonify({"status": "ok"})


# ── PayPal return URL ─────────────────────────────────────────────────────────
@flask_app.route("/paypal/return")
def paypal_return():
    from bot.payments import capture_paypal_order, _bot_username
    BOT_USERNAME = _bot_username()

    paypal_order_id = request.args.get("token")
    payer_id = request.args.get("PayerID")

    if not paypal_order_id or not payer_id:
        return _html_page("❌ Payment Cancelled", "No payment data received."), 400

    loop = asyncio.new_event_loop()
    try:
        result = loop.run_until_complete(capture_paypal_order(paypal_order_id))
    finally:
        loop.close()

    if not result.get("ok"):
        logger.error(f"PayPal capture failed: {result}")
        return _html_page(
            "⚠️ Capture Issue",
            "Your payment may have been received. If you were charged, contact support.",
            BOT_USERNAME
        ), 500

    custom_id = result.get("custom_id", "")
    # Use the same capture_id-based dedup key as /webhook/paypal — prevents double-upgrade
    capture_id = result.get("capture_id", "")
    parts = custom_id.split("_")
    days_str = "?"
    if len(parts) >= 3 and parts[0] == "tg":
        user_id = int(parts[1])
        days = int(parts[2])
        days_str = str(days)
        dedup_key = f"paypal_{capture_id}" if capture_id else f"paypal_return_{paypal_order_id}"
        _schedule(_upgrade_and_notify(user_id, days, "paypal_return", dedup_key))

    return _html_page(
        "✅ Payment Successful!",
        f"Your Premium for <strong>{days_str} days</strong> is being activated. "
        f"Return to Telegram — you'll get a confirmation message shortly.",
        BOT_USERNAME
    ), 200


def _html_page(title: str, body: str, bot_username: str = "Wolfy004bot") -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}</title>
  <style>
    body{{font-family:Arial,sans-serif;text-align:center;padding:60px 20px;background:#0e0e0e;color:#fff}}
    h2{{font-size:2em}} p{{font-size:1.1em;color:#ccc;max-width:500px;margin:20px auto}}
    a{{display:inline-block;margin-top:30px;padding:12px 28px;background:#2196F3;
       color:#fff;border-radius:8px;text-decoration:none;font-size:1em}}
  </style>
</head>
<body>
  <h2>{title}</h2><p>{body}</p>
  <a href="https://t.me/{bot_username}">↩ Back to Bot</a>
</body></html>"""


# ── Server startup ────────────────────────────────────────────────────────────
def run_webhook_server() -> None:
    port = int(os.environ.get("WEBHOOK_PORT", 8080))
    logger.info(f"Webhook server listening on 0.0.0.0:{port}")
    flask_app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)


def start_webhook_thread(loop: asyncio.AbstractEventLoop, client) -> threading.Thread:
    init_webhook(loop, client)
    thread = threading.Thread(target=run_webhook_server, daemon=True, name="webhook-server")
    thread.start()
    return thread
