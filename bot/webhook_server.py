"""
Flask webhook server — receives payment callbacks from ZapUPI, Oxapay, PayPal
and auto-upgrades users via the running Pyrogram bot.

Architecture for reliability under heavy load:
- A dedicated asyncio event loop (_upgrade_loop) runs on its own thread.
  All DB operations (dedup, extend_premium) run there, completely isolated
  from the bot's main loop and its 100 download workers.
- Telegram notification is scheduled back onto the bot's main loop after the
  DB work is done — the two never block each other.
- Flask runs with threaded=True so concurrent webhook calls are handled in
  parallel instead of queuing behind each other.
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

# Set by start_webhook_thread()
_bot_loop: asyncio.AbstractEventLoop | None = None
_bot_client = None


# ── Init ──────────────────────────────────────────────────────────────────────
def init_webhook(loop: asyncio.AbstractEventLoop, client) -> None:
    global _bot_loop, _bot_client
    _bot_loop = loop
    _bot_client = client
    logger.info("Webhook server: bot context registered")


def _schedule(coro) -> None:
    """
    Schedule an upgrade coroutine on the bot's main event loop.
    The asyncpg pool is bound to this loop — DB calls MUST run here.
    Pyrogram also requires its own loop, so both DB and Telegram work go here.
    """
    if _bot_loop and _bot_loop.is_running():
        asyncio.run_coroutine_threadsafe(coro, _bot_loop)
    else:
        logger.warning("Webhook: bot loop not ready, upgrade skipped")


# ── Core upgrade logic ────────────────────────────────────────────────────────
async def _upgrade_and_notify(user_id: int, days: int, gateway: str, dedup_key: str) -> None:
    """
    Runs on the dedicated upgrade loop (never on the bot's download loop).
    1. Validates plan, deduplicates, extends premium — all via DB.
    2. Schedules the Telegram notification back on the bot's main loop.
    """
    from bot.database import extend_premium, get_user, claim_payment_dedup
    from bot.payments import PLANS
    try:
        # Validate days — reject tampered or injected values
        if str(days) not in PLANS:
            logger.error(
                f"Upgrade rejected: days={days} not in PLANS "
                f"(gateway={gateway}, key={dedup_key})"
            )
            return

        # Atomic DB dedup — safe across restarts and concurrent webhooks
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
        logger.info(
            f"Auto-upgraded user={user_id} days={days} "
            f"gateway={gateway} key={dedup_key}"
        )

        # Schedule Telegram notification on the bot's main loop
        # (Pyrogram is not thread-safe; must run on its own loop)
        if _bot_loop and _bot_client:
            asyncio.run_coroutine_threadsafe(
                _bot_client.send_message(
                    user_id,
                    f"🎉 **Payment Confirmed!**\n\n"
                    f"✅ Your account has been upgraded to **Premium** for **{days} days**.\n\n"
                    f"You now have:\n"
                    f"• ♾ Unlimited downloads\n"
                    f"• 📦 Batch up to 50 files\n"
                    f"• ⚡ Fast download engine\n"
                    f"• 🏷 Caption tools (/capadd · /caprem)\n\n"
                    f"Use /info to check your status.\n"
                    f"Thank you for your support! 🙏"
                ),
                _bot_loop,
            )
    except Exception as e:
        logger.error(f"_upgrade_and_notify error user={user_id}: {e}")


# ── Health check ──────────────────────────────────────────────────────────────
@flask_app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "bot_ready": _bot_client is not None,
        "bot_loop_running": _bot_loop is not None and _bot_loop.is_running(),
    })


# ── ZapUPI webhook ────────────────────────────────────────────────────────────
@flask_app.route("/webhook/zapupi", methods=["POST"])
def zapupi_webhook():
    """
    ZapUPI sends a POST to this endpoint when a payment status changes.

    Docs: https://zapupi.com/docs — Section B, Step 3
    ZapUPI does NOT sign webhooks with any HMAC — no signature to verify.

    Payload fields:
      order_id    — our order ID
      status      — "Success" | "Failed"  (title-case)
      txn_id      — ZapUPI transaction ID
      amount      — INR amount paid
      utr         — bank UTR reference number
      environment — "cashier" | "zappay" | "test"

    Flow: accept → confirm via order-status API → upgrade user.
    Always respond HTTP 200 + {"status":"ok"} regardless of outcome.
    """
    try:
        raw_body = request.get_data()
        data = json.loads(raw_body) if raw_body else {}

        from bot.payments import ZAPUPI_MERCHANT_KEY, PLANS, parse_order_id, check_zapupi_order_status

        if not ZAPUPI_MERCHANT_KEY:
            logger.error("ZapUPI webhook received but ZAPUPI_MERCHANT_KEY not set")
            return jsonify({"status": "ok"})  # still 200 so ZapUPI doesn't retry

        status      = data.get("status", "")
        order_id    = data.get("order_id", "")
        txn_id      = data.get("txn_id", "")
        paid_amount = data.get("amount", 0)
        utr         = data.get("utr", "")
        environment = data.get("environment", "")

        logger.info(
            f"ZapUPI webhook: status={status!r} order={order_id!r} "
            f"txn={txn_id!r} utr={utr!r} env={environment!r} amount={paid_amount}"
        )

        # ZapUPI sends "Success" (title-case) for successful payments
        if status != "Success":
            logger.info(f"ZapUPI webhook: ignoring status={status!r}")
            return jsonify({"status": "ok"})

        if not order_id:
            logger.warning("ZapUPI webhook: missing order_id")
            return jsonify({"status": "ok"})

        user_id, days = parse_order_id(order_id)
        if not user_id or not days:
            logger.warning(f"ZapUPI: unrecognised order_id={order_id!r}")
            return jsonify({"status": "ok"})

        # Double-confirm via order-status API before upgrading (docs recommend this).
        loop = asyncio.new_event_loop()
        try:
            confirmed = loop.run_until_complete(check_zapupi_order_status(order_id))
        finally:
            loop.close()

        # Order-status API returns lowercase "success" (webhook uses title-case "Success")
        confirmed_status = confirmed.get("status", "").lower()
        if confirmed_status != "success":
            logger.warning(
                f"ZapUPI order-status check returned {confirmed.get('status')!r} "
                f"for {order_id} — skipping upgrade"
            )
            return jsonify({"status": "ok"})

        # Verify paid amount matches the plan price (INR)
        if str(days) in PLANS:
            expected_inr = PLANS[str(days)]["inr"]
            try:
                if paid_amount and int(float(paid_amount)) < expected_inr:
                    logger.error(
                        f"ZapUPI amount mismatch: expected ₹{expected_inr} "
                        f"but got ₹{paid_amount} for order={order_id} — rejected"
                    )
                    return jsonify({"status": "ok"})
            except (ValueError, TypeError):
                pass  # non-numeric amount; let it through, DB has the real record

        dedup_key = f"zapupi_{txn_id or order_id}"
        _schedule(_upgrade_and_notify(user_id, days, "zapupi", dedup_key))
        return jsonify({"status": "ok"})
    except Exception as e:
        logger.error(f"ZapUPI webhook exception: {e}", exc_info=True)
        return jsonify({"status": "ok"})  # always 200 so ZapUPI doesn't retry endlessly


# ── Oxapay webhook ────────────────────────────────────────────────────────────
@flask_app.route("/webhook/oxapay", methods=["POST"])
def oxapay_webhook():
    try:
        raw_body = request.get_data()
        hmac_header = request.headers.get("HMAC", "")

        from bot.payments import OXAPAY_MERCHANT_KEY, parse_order_id
        if not OXAPAY_MERCHANT_KEY:
            logger.error("Oxapay webhook received but OXAPAY_MERCHANT_KEY not set — rejected")
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
        payload = data.get("data") if isinstance(data.get("data"), dict) else data
        status = payload.get("status", "")
        order_id = payload.get("order_id", "")
        track_id = payload.get("track_id", order_id)
        logger.info(f"Oxapay webhook: status={status} order={order_id} track={track_id}")

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
    Verify a PayPal webhook using PayPal's own signature-verification API.
    Runs synchronously on a fresh event loop (called from Flask thread).
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
            async with _httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(
                    f"{paypal_base}/v1/notifications/verify-webhook-signature",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                    },
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
            logger.error("PayPal webhook received but PAYPAL_WEBHOOK_ID not set — rejected")
            return jsonify({"status": "rejected", "reason": "webhook ID not configured"}), 403

        if not _verify_paypal_webhook_sync(dict(request.headers), raw_body, PAYPAL_WEBHOOK_ID, PAYPAL_BASE):
            logger.warning("PayPal webhook signature verification failed — rejected")
            return jsonify({"status": "rejected"}), 400

        if event_type != "PAYMENT.CAPTURE.COMPLETED":
            return jsonify({"status": "ok"})

        resource = data.get("resource", {})
        capture_id = resource.get("id", "")
        custom_id = resource.get("custom_id", "")

        if not custom_id:
            logger.warning("PayPal webhook: no custom_id in resource")
            return jsonify({"status": "ok"})

        parts = custom_id.split("_")
        if len(parts) >= 3 and parts[0] == "tg":
            user_id = int(parts[1])
            days = int(parts[2])
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
    capture_id = result.get("capture_id", "")
    parts = custom_id.split("_")
    days_str = "?"
    if len(parts) >= 3 and parts[0] == "tg":
        user_id = int(parts[1])
        days = int(parts[2])
        days_str = str(days)
        # Same dedup key as /webhook/paypal — prevents double-upgrade
        dedup_key = f"paypal_{capture_id}" if capture_id else f"paypal_return_{paypal_order_id}"
        _schedule(_upgrade_and_notify(user_id, days, "paypal_return", dedup_key))

    return _html_page(
        "✅ Payment Successful!",
        f"Your Premium for <strong>{days_str} days</strong> is being activated. "
        f"Return to Telegram — you'll get a confirmation message shortly.",
        BOT_USERNAME
    ), 200


def _html_page(title: str, body: str, bot_username: str = "DownloadRestrictedVideo_Bot") -> str:
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
    # threaded=True — each incoming webhook request gets its own thread,
    # so a slow PayPal verification call never blocks ZapUPI/Oxapay callbacks
    flask_app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False, threaded=True)


def start_webhook_thread(loop: asyncio.AbstractEventLoop, client) -> threading.Thread:
    init_webhook(loop, client)
    thread = threading.Thread(target=run_webhook_server, daemon=True, name="webhook-server")
    thread.start()
    return thread
