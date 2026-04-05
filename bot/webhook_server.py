import asyncio
import json
import logging
from flask import Flask, request, jsonify, abort

logger = logging.getLogger(__name__)

flask_app = Flask(__name__)


def _get_bot_loop():
    from bot import config as _cfg
    return getattr(_cfg, "bot_event_loop", None)


def _run_async(coro):
    loop = _get_bot_loop()
    if loop is None or loop.is_closed():
        raise RuntimeError("Bot event loop is not available")
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result(timeout=30)


async def _auto_upgrade(telegram_id: int, days: int, payment_id: str, provider: str):
    from bot.database import set_user_role
    from bot.payments_db import is_payment_processed, mark_payment_complete

    if await is_payment_processed(payment_id):
        logger.info(f"[{provider}] Payment {payment_id} already processed — skipping")
        return

    await set_user_role(telegram_id, "premium", days)
    await mark_payment_complete(payment_id, telegram_id, provider, days)
    logger.info(f"[{provider}] Upgraded user {telegram_id} → {days} days (payment {payment_id})")

    try:
        from bot.config import app
        await app.send_message(
            telegram_id,
            f"🎉 **Payment Confirmed!**\n\n"
            f"Your account has been upgraded to **Premium** for **{days} days**.\n"
            f"You now have unlimited downloads! 🚀\n\n"
            f"Provider: `{provider.upper()}`\n"
            f"Payment ID: `{payment_id}`",
        )
    except Exception as e:
        logger.error(f"Failed to notify user {telegram_id}: {e}")


# ── PayPal: Return URL (after buyer approval) ─────────────────────────────────

@flask_app.route("/paypal/return")
def paypal_return():
    order_id = request.args.get("token")
    if not order_id:
        return "<h1>Missing order ID</h1>", 400

    try:
        from bot.payments import capture_paypal_order
        capture_data = _run_async(capture_paypal_order(order_id))

        if capture_data.get("status") != "COMPLETED":
            logger.error(f"PayPal capture not COMPLETED: {capture_data}")
            return (
                "<html><body style='font-family:Arial;text-align:center;padding:50px'>"
                "<h1>❌ Payment not completed.</h1>"
                "<p>Please contact support if you were charged.</p>"
                "</body></html>"
            ), 400

        purchase_units = capture_data.get("purchase_units", [])
        if not purchase_units:
            return "<h1>Invalid PayPal response</h1>", 400

        custom_id = purchase_units[0].get("custom_id", "")
        captures = purchase_units[0].get("payments", {}).get("captures", [])
        payment_id = captures[0]["id"] if captures else order_id

        parts = custom_id.split("_")
        if len(parts) < 3:
            logger.error(f"Cannot parse custom_id: {custom_id!r}")
            return "<h1>Invalid payment data</h1>", 400

        telegram_id = int(parts[0])
        days = int(parts[2])
        _run_async(_auto_upgrade(telegram_id, days, payment_id, "paypal"))

        return (
            "<html><body style='font-family:Arial;text-align:center;padding:50px'>"
            "<h1>✅ Payment Successful!</h1>"
            "<p>Your premium account is now active.</p>"
            "<p>Return to the Telegram bot to start downloading!</p>"
            "</body></html>"
        ), 200

    except Exception as e:
        logger.error(f"PayPal return handler error: {e}", exc_info=True)
        return f"<h1>Error: {e}</h1>", 500


@flask_app.route("/paypal/cancel")
def paypal_cancel():
    return (
        "<html><body style='font-family:Arial;text-align:center;padding:50px'>"
        "<h1>Payment cancelled.</h1>"
        "<p>You can try again from the bot at any time.</p>"
        "</body></html>"
    ), 200


# ── PayPal Webhook (backup for captures that bypass return_url) ───────────────

@flask_app.route("/webhook/paypal", methods=["POST"])
def paypal_webhook():
    raw_body = request.get_data()

    t_id   = request.headers.get("PAYPAL-TRANSMISSION-ID", "")
    t_time = request.headers.get("PAYPAL-TRANSMISSION-TIME", "")
    t_sig  = request.headers.get("PAYPAL-TRANSMISSION-SIG", "")
    cert   = request.headers.get("PAYPAL-CERT-URL", "")
    algo   = request.headers.get("PAYPAL-AUTH-ALGO", "SHA256withRSA")

    if not all([t_id, t_time, t_sig, cert]):
        abort(400)

    from bot.payments import verify_paypal_webhook_signature, PAYPAL_WEBHOOK_ID
    if PAYPAL_WEBHOOK_ID:
        if not verify_paypal_webhook_signature(t_id, t_time, PAYPAL_WEBHOOK_ID, raw_body, cert, t_sig, algo):
            logger.warning("PayPal webhook: invalid signature")
            abort(403)

    event = json.loads(raw_body)
    event_type = event.get("event_type", "")

    if event_type == "PAYMENT.CAPTURE.COMPLETED":
        try:
            resource    = event.get("resource", {})
            payment_id  = resource.get("id", "")
            custom_id   = resource.get("custom_id", "")

            if not custom_id:
                for pu in resource.get("purchase_units", []):
                    custom_id = pu.get("custom_id", "")
                    if custom_id:
                        break

            if custom_id and payment_id:
                parts = custom_id.split("_")
                if len(parts) >= 3:
                    telegram_id = int(parts[0])
                    days = int(parts[2])
                    _run_async(_auto_upgrade(telegram_id, days, payment_id, "paypal"))
        except Exception as e:
            logger.error(f"PayPal webhook processing error: {e}", exc_info=True)

    return jsonify({"status": "ok"}), 200


# ── Razorpay Webhook ──────────────────────────────────────────────────────────
@flask_app.route("/webhook/razorpay", methods=["POST"])
def razorpay_webhook():
    raw_body  = request.get_data()
    signature = request.headers.get("X-Razorpay-Signature", "")
    from bot.payments import verify_razorpay_webhook
    if not verify_razorpay_webhook(raw_body, signature):
        logger.warning("Razorpay webhook: invalid signature")
        abort(403)

    event      = json.loads(raw_body)
    event_type = event.get("event", "")
    
    if event_type == "payment_link.paid":
        try:
            payload      = event.get("payload", {})
            plink_entity = payload.get("payment_link", {}).get("entity", {})
            pay_entity   = payload.get("payment",      {}).get("entity", {})
            notes       = plink_entity.get("notes", {})
            telegram_id = int(notes.get("telegram_id", 0))
            days        = int(notes.get("days", 0))
            payment_id  = pay_entity.get("id", "")
            if telegram_id and days and payment_id:
                _run_async(_auto_upgrade(telegram_id, days, payment_id, "razorpay"))
        except Exception as e:
            logger.error(f"Razorpay webhook error: {e}", exc_info=True)

    return jsonify({"status": "ok"}), 200


# ── OxaPay Webhook ────────────────────────────────────────────────────────────

@flask_app.route("/webhook/oxapay", methods=["POST"])
def oxapay_webhook():
    raw_body  = request.get_data()
    signature = request.headers.get("HMAC", "")

    from bot.payments import verify_oxapay_webhook
    if not verify_oxapay_webhook(raw_body, signature):
        logger.warning("OxaPay webhook: invalid signature")
        abort(403)

    event      = json.loads(raw_body)
    status     = event.get("status", "")
    event_type = event.get("type", "")

    if event_type == "invoice" and status == "Paid":
        try:
            order_id   = event.get("orderId", "")
            track_id   = str(event.get("trackId", ""))
            parts      = order_id.split("_")
            if len(parts) >= 3:
                telegram_id = int(parts[0])
                days        = int(parts[2])
                _run_async(_auto_upgrade(telegram_id, days, track_id, "oxapay"))
        except Exception as e:
            logger.error(f"OxaPay webhook error: {e}", exc_info=True)

    return "OK", 200


# ── Health Check ──────────────────────────────────────────────────────────────

@flask_app.route("/health")
def health():
    return jsonify({"status": "ok"}), 200


# ── Server Start ──────────────────────────────────────────────────────────────

def _kill_port(port: int):
    """Kill any process currently holding the port so we can bind cleanly."""
    try:
        import subprocess
        result = subprocess.run(
            ["lsof", "-ti", f":{port}"],
            capture_output=True, text=True
        )
        pids = result.stdout.strip().split()
        for pid in pids:
            if pid:
                subprocess.run(["kill", "-9", pid], capture_output=True)
                logger.info(f"Killed old process {pid} on port {port}")
    except Exception:
        pass


def start_webhook_server(host: str = "0.0.0.0", port: int = 8080):
    import threading
    import time

    logging.getLogger("werkzeug").setLevel(logging.ERROR)

    _kill_port(port)
    time.sleep(1)

    thread = threading.Thread(
        target=lambda: flask_app.run(host=host, port=port, debug=False, use_reloader=False),
        daemon=True,
        name="webhook-server",
    )
    thread.start()
    logger.info(f"Webhook server started on {host}:{port}")
    return thread
