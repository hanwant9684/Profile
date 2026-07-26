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
async def _upgrade_and_notify(user_id: int, days: int, gateway: str, dedup_key: str, order_id: str | None = None) -> None:
    """
    Runs on the bot's main event loop (scheduled via _schedule).
    1. For ZaPuPi: optionally double-confirms via order-status API (async, non-blocking).
    2. Validates plan, deduplicates, extends premium — all via DB.
    3. Sends Telegram notification.
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

        # For ZaPuPi: async order-status double-check (runs here on the bot loop,
        # so it never blocks the Flask webhook response / ZapUPI's 10-second window)
        if gateway == "zapupi" and order_id:
            from bot.payments import check_zapupi_order_status
            confirmed = await check_zapupi_order_status(order_id)
            logger.info(f"ZapUPI order-status response for {order_id}: {confirmed}")
            confirmed_status = confirmed.get("status", "").lower()
            if confirmed_status != "success":
                logger.warning(
                    f"ZapUPI order-status check returned {confirmed.get('status')!r} "
                    f"for {order_id} — skipping upgrade"
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
                    f"Use /myinfo to check your status.\n"
                    f"Thank you for your support! 🙏"
                ),
                _bot_loop,
            )
    except Exception as e:
        logger.error(f"_upgrade_and_notify error user={user_id}: {e}")


# ── Revoke + notify ───────────────────────────────────────────────────────────
async def _revoke_and_notify(user_id: int, reason: str, dedup_key: str) -> None:
    """
    Demote user to free and send a Telegram notification.
    Runs on the bot's main event loop (scheduled via _schedule).
    reason: "refunded" or "reversed" — shown in the Telegram message.
    """
    from bot.database import revoke_premium, claim_payment_dedup, get_user
    try:
        claimed = await claim_payment_dedup(dedup_key)
        if not claimed:
            logger.info(f"Duplicate revocation ignored: {dedup_key}")
            return

        user = await get_user(user_id)
        if not user:
            logger.warning(f"PayPal {reason}: user {user_id} not in DB — nothing to revoke")
            return

        await revoke_premium(user_id)
        logger.info(f"Premium revoked: user={user_id} reason={reason} key={dedup_key}")

        if _bot_loop and _bot_client:
            verb = "refunded" if reason == "refunded" else "reversed by PayPal"
            asyncio.run_coroutine_threadsafe(
                _bot_client.send_message(
                    user_id,
                    f"⚠️ **Premium Access Revoked**\n\n"
                    f"Your PayPal payment was {verb}.\n"
                    f"Your account has been downgraded to the free plan.\n\n"
                    f"If you believe this is a mistake, please contact support.",
                ),
                _bot_loop,
            )
    except Exception as e:
        logger.error(f"_revoke_and_notify error user={user_id}: {e}")


# ── PayPal order-lookup for revocation (REFUNDED / REVERSED fallback) ─────────
async def _paypal_lookup_and_revoke(order_id: str, dedup_suffix: str, reason: str) -> None:
    """
    Fetch the PayPal order by ID to recover custom_id, then revoke premium.
    Used when custom_id is absent from the REVERSED/REFUNDED webhook resource.
    """
    from bot.payments import get_paypal_token, PAYPAL_BASE
    import httpx as _httpx
    try:
        token = await get_paypal_token()
        if not token:
            logger.error(f"_paypal_lookup_and_revoke: could not get token for order={order_id}")
            return
        async with _httpx.AsyncClient(timeout=_httpx.Timeout(15.0, connect=10.0)) as client:
            resp = await client.get(
                f"{PAYPAL_BASE}/v2/checkout/orders/{order_id}",
                headers={"Authorization": f"Bearer {token}"},
            )
        units = resp.json().get("purchase_units", [])
        if not units:
            logger.error(f"_paypal_lookup_and_revoke: no purchase_units for order={order_id}")
            return
        custom_id = units[0].get("custom_id", "")
        parts = custom_id.split("_")
        if len(parts) >= 3 and parts[0] == "tg":
            user_id = int(parts[1])
            dedup_key = f"paypal_revoke_{reason}_{dedup_suffix}"
            await _revoke_and_notify(user_id, reason, dedup_key)
        else:
            logger.error(f"_paypal_lookup_and_revoke: unrecognised custom_id={custom_id!r}")
    except Exception as e:
        logger.error(f"_paypal_lookup_and_revoke error for order={order_id}: {e}")


# ── PayPal capture-lookup for revocation (REFUNDED only, when order_id absent) ─
async def _paypal_capture_lookup_and_revoke(capture_id: str, refund_id: str) -> None:
    """
    For PAYMENT.CAPTURE.REFUNDED when supplementary_data lacks order_id:
    fetch GET /v2/payments/captures/{id}, extract the parent order_id from the
    "up" HATEOAS link, then call _paypal_lookup_and_revoke.
    """
    from bot.payments import get_paypal_token, PAYPAL_BASE
    import httpx as _httpx
    try:
        token = await get_paypal_token()
        if not token:
            logger.error(f"_paypal_capture_lookup_and_revoke: no token for capture={capture_id}")
            return
        async with _httpx.AsyncClient(timeout=_httpx.Timeout(15.0, connect=10.0)) as client:
            resp = await client.get(
                f"{PAYPAL_BASE}/v2/payments/captures/{capture_id}",
                headers={"Authorization": f"Bearer {token}"},
            )
        capture_data = resp.json()
        order_id = ""
        for lnk in capture_data.get("links", []):
            if lnk.get("rel") == "up":
                order_id = lnk.get("href", "").rstrip("/").split("/")[-1]
                break
        if order_id:
            await _paypal_lookup_and_revoke(order_id, refund_id, "refunded")
        else:
            logger.error(
                f"_paypal_capture_lookup_and_revoke: no 'up' link for capture={capture_id}"
            )
    except Exception as e:
        logger.error(f"_paypal_capture_lookup_and_revoke error capture={capture_id}: {e}")


# ── PayPal order-lookup fallback ─────────────────────────────────────────────
async def _paypal_lookup_and_upgrade(order_id: str, capture_id: str, gateway: str) -> None:
    """
    Fetch the PayPal order by ID to recover custom_id when it's absent from the
    PAYMENT.CAPTURE.COMPLETED webhook payload, then call _upgrade_and_notify.
    Runs on the bot's main event loop (scheduled via _schedule).
    """
    from bot.payments import get_paypal_token, PAYPAL_BASE
    import httpx as _httpx

    try:
        token = await get_paypal_token()
        if not token:
            logger.error(f"_paypal_lookup_and_upgrade: could not get PayPal token for order={order_id}")
            return

        async with _httpx.AsyncClient(timeout=_httpx.Timeout(15.0, connect=10.0)) as client:
            resp = await client.get(
                f"{PAYPAL_BASE}/v2/checkout/orders/{order_id}",
                headers={"Authorization": f"Bearer {token}"},
            )
        data = resp.json()
        units = data.get("purchase_units", [])
        if not units:
            logger.error(f"_paypal_lookup_and_upgrade: no purchase_units for order={order_id}")
            return

        custom_id = units[0].get("custom_id", "")
        if not custom_id:
            logger.error(f"_paypal_lookup_and_upgrade: custom_id still empty after order lookup for order={order_id}")
            return

        parts = custom_id.split("_")
        if len(parts) >= 3 and parts[0] == "tg":
            user_id = int(parts[1])
            days = int(parts[2])
            dedup_key = f"paypal_{capture_id}" if capture_id else f"paypal_order_{order_id}"
            await _upgrade_and_notify(user_id, days, gateway, dedup_key)
        else:
            logger.error(f"_paypal_lookup_and_upgrade: unrecognised custom_id={custom_id!r} for order={order_id}")
    except Exception as e:
        logger.error(f"_paypal_lookup_and_upgrade error for order={order_id}: {e}")


# ── PayPal CHECKOUT.ORDER.APPROVED capture ────────────────────────────────────
async def _paypal_approved_capture_and_upgrade(order_id: str) -> None:
    """
    Handle CHECKOUT.ORDER.APPROVED: capture the order then grant premium.
    Runs on the bot's main event loop (scheduled via _schedule).

    This fires when the user approves payment on PayPal but their browser closes
    before the redirect to /paypal/return lands — leaving the order in APPROVED
    state indefinitely. Without this handler, the user is charged but gets nothing.

    Uses the same dedup key pattern as the other PayPal paths so a subsequent
    PAYMENT.CAPTURE.COMPLETED webhook (or a late /paypal/return redirect) cannot
    double-upgrade the same user.
    """
    from bot.payments import capture_paypal_order

    try:
        result = await capture_paypal_order(order_id)

        if not result.get("ok"):
            # already_captured: /paypal/return won the race — nothing to do.
            if result.get("already_captured"):
                logger.info(
                    f"PayPal CHECKOUT.ORDER.APPROVED: order {order_id} "
                    f"already captured — skipping (handled by return URL)"
                )
                return
            logger.error(
                f"PayPal CHECKOUT.ORDER.APPROVED: capture failed for "
                f"order={order_id}: {result}"
            )
            return

        # PENDING: PayPal is holding the capture for review.
        # Do NOT upgrade here — PAYMENT.CAPTURE.COMPLETED will fire when it
        # settles and grant premium through the normal webhook path (which also
        # does full amount validation before upgrading).
        if result.get("pending"):
            logger.info(
                f"PayPal CHECKOUT.ORDER.APPROVED: capture PENDING for "
                f"order={order_id} — waiting for PAYMENT.CAPTURE.COMPLETED"
            )
            return

        custom_id = result.get("custom_id", "")
        capture_id = result.get("capture_id", "")

        parts = custom_id.split("_")
        if len(parts) >= 3 and parts[0] == "tg":
            user_id = int(parts[1])
            days = int(parts[2])
            dedup_key = f"paypal_{capture_id}" if capture_id else f"paypal_order_{order_id}"
            logger.info(
                f"PayPal CHECKOUT.ORDER.APPROVED: captured order={order_id} "
                f"user={user_id} days={days} dedup={dedup_key}"
            )
            await _upgrade_and_notify(user_id, days, "paypal_approved", dedup_key)
        else:
            logger.error(
                f"PayPal CHECKOUT.ORDER.APPROVED: unrecognised custom_id={custom_id!r} "
                f"for order={order_id} — upgrade skipped"
            )
    except Exception as e:
        logger.error(f"_paypal_approved_capture_and_upgrade error for order={order_id}: {e}")


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
    try:
        raw_body = request.get_data()
        data = json.loads(raw_body) if raw_body else {}

        from bot.payments import ZAPUPI_MERCHANT_KEY, PLANS, parse_order_id, check_zapupi_order_status

        if not ZAPUPI_MERCHANT_KEY:
            logger.error("ZapUPI webhook received but ZAPUPI_MERCHANT_KEY not set")
            return jsonify({"status": "ok"})

        status      = data.get("status", "")
        order_id    = data.get("order_id", "")
        txn_id      = data.get("txn_id", "")
        paid_amount = data.get("pay_amount") or data.get("amount", 0)  # pay_amount = actual amount paid
        utr         = data.get("utr", "")
        environment = data.get("environment", "")

        logger.info(
            f"ZapUPI webhook: status={status!r} order={order_id!r} "
            f"txn={txn_id!r} utr={utr!r} env={environment!r} amount={paid_amount}"
        )

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

        # Amount sanity check using the webhook payload (fast, no extra HTTP call)
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
                pass

        dedup_key = f"zapupi_{txn_id or order_id}"
        # Schedule upgrade BEFORE returning so ZapUPI gets HTTP 200 within its
        # 10-second window. The order-status double-check runs async inside
        # _upgrade_and_notify on the bot's loop — it never blocks this response.
        _schedule(_upgrade_and_notify(user_id, days, "zapupi", dedup_key, order_id))
        return jsonify({"status": "ok"})
    except Exception as e:
        logger.error(f"ZapUPI webhook exception: {e}", exc_info=True)
        return jsonify({"status": "ok"})


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

        # Normalize header keys to uppercase before passing to the verifier.
        # dict(request.headers) produces title-cased keys (e.g. "Paypal-Auth-Algo")
        # because Werkzeug reconstructs them from the WSGI environ, but our verifier
        # (and PayPal's own docs) use ALL-CAPS names like "PAYPAL-AUTH-ALGO".
        # A plain-dict lookup is case-sensitive, so without this normalization every
        # header read returns "" and PayPal's API returns FAILURE for every webhook.
        normalized_headers = {k.upper(): v for k, v in request.headers.items()}
        if not _verify_paypal_webhook_sync(normalized_headers, raw_body, PAYPAL_WEBHOOK_ID, PAYPAL_BASE):
            logger.warning("PayPal webhook signature verification failed — rejected")
            return jsonify({"status": "rejected"}), 400

        if event_type == "CHECKOUT.ORDER.APPROVED":
            # User approved payment but browser closed before /paypal/return redirect.
            # Capture the order here so premium is still granted.
            resource = data.get("resource", {})
            order_id = resource.get("id", "")
            if not order_id:
                logger.warning("PayPal CHECKOUT.ORDER.APPROVED: no order ID in resource — skipped")
                return jsonify({"status": "ok"})
            logger.info(f"PayPal CHECKOUT.ORDER.APPROVED: scheduling capture for order={order_id}")
            _schedule(_paypal_approved_capture_and_upgrade(order_id))
            return jsonify({"status": "ok"})

        # ── Refund / reversal — revoke premium ───────────────────────────────
        if event_type == "PAYMENT.CAPTURE.REVERSED":
            # resource IS the capture object (status=REVERSED), same shape as
            # PAYMENT.CAPTURE.COMPLETED — custom_id is usually present directly.
            resource = data.get("resource", {})
            capture_id = resource.get("id", "")
            custom_id = resource.get("custom_id", "")

            if not custom_id:
                # Fallback: supplementary_data or HATEOAS "up" → parent order
                order_id_for_lookup = (
                    resource.get("supplementary_data", {})
                            .get("related_ids", {}).get("order_id", "")
                )
                if not order_id_for_lookup:
                    for lnk in resource.get("links", []):
                        if lnk.get("rel") == "up":
                            order_id_for_lookup = lnk.get("href", "").rstrip("/").split("/")[-1]
                            break
                if order_id_for_lookup:
                    logger.info(f"PayPal REVERSED: resolving custom_id via order={order_id_for_lookup}")
                    _schedule(_paypal_lookup_and_revoke(order_id_for_lookup, capture_id, "reversed"))
                else:
                    logger.warning(f"PayPal REVERSED: could not resolve custom_id for capture={capture_id!r}")
                return jsonify({"status": "ok"})

            parts = custom_id.split("_")
            if len(parts) >= 3 and parts[0] == "tg":
                user_id = int(parts[1])
                dedup_key = f"paypal_revoke_reversed_{capture_id}" if capture_id else f"paypal_revoke_rcid_{custom_id}"
                logger.info(f"PayPal REVERSED: scheduling revoke user={user_id} capture={capture_id}")
                _schedule(_revoke_and_notify(user_id, "reversed", dedup_key))
            else:
                logger.warning(f"PayPal REVERSED: unrecognised custom_id={custom_id!r}")
            return jsonify({"status": "ok"})

        if event_type == "PAYMENT.CAPTURE.REFUNDED":
            # resource is the REFUND object (not the capture).
            # supplementary_data usually carries both order_id and capture_id.
            resource = data.get("resource", {})
            refund_id = resource.get("id", "")
            related = resource.get("supplementary_data", {}).get("related_ids", {})

            order_id_for_lookup = related.get("order_id", "")
            if order_id_for_lookup:
                logger.info(f"PayPal REFUNDED: resolving via order={order_id_for_lookup} refund={refund_id}")
                _schedule(_paypal_lookup_and_revoke(order_id_for_lookup, refund_id, "refunded"))
                return jsonify({"status": "ok"})

            # Fallback: get capture_id → fetch capture → "up" link → order
            capture_id = related.get("capture_id", "")
            if not capture_id:
                for lnk in resource.get("links", []):
                    if lnk.get("rel") == "up":
                        capture_id = lnk.get("href", "").rstrip("/").split("/")[-1]
                        break
            if capture_id:
                logger.info(f"PayPal REFUNDED: resolving via capture={capture_id} refund={refund_id}")
                _schedule(_paypal_capture_lookup_and_revoke(capture_id, refund_id))
            else:
                logger.warning(f"PayPal REFUNDED: could not resolve order for refund={refund_id!r}")
            return jsonify({"status": "ok"})

        if event_type != "PAYMENT.CAPTURE.COMPLETED":
            return jsonify({"status": "ok"})

        resource = data.get("resource", {})
        capture_id = resource.get("id", "")
        custom_id = resource.get("custom_id", "")

        # Fallback: some PayPal webhook payloads omit custom_id on the capture
        # resource even though it was set on purchase_units at order creation.
        # In that case, look it up via the Orders API using the order ID from
        # supplementary_data or the links in the resource.
        if not custom_id:
            order_id_for_lookup = (
                resource.get("supplementary_data", {})
                        .get("related_ids", {})
                        .get("order_id", "")
            )
            if not order_id_for_lookup:
                # Try HATEOAS links: rel="up" points to the parent order
                for lnk in resource.get("links", []):
                    if lnk.get("rel") == "up":
                        href = lnk.get("href", "")
                        order_id_for_lookup = href.rstrip("/").split("/")[-1]
                        break

            if order_id_for_lookup:
                logger.info(
                    f"PayPal webhook: custom_id missing — fetching order "
                    f"{order_id_for_lookup} to resolve"
                )
                _schedule(_paypal_lookup_and_upgrade(
                    order_id_for_lookup, capture_id, "paypal_webhook"
                ))
                return jsonify({"status": "ok"})

            logger.warning(
                f"PayPal webhook: no custom_id and could not find order ID "
                f"for capture {capture_id!r} — upgrade skipped"
            )
            return jsonify({"status": "ok"})

        parts = custom_id.split("_")
        if len(parts) >= 3 and parts[0] == "tg":
            user_id = int(parts[1])
            days = int(parts[2])

            # Amount validation — prevent someone paying $0.01 and getting premium.
            # The captured gross_amount (or amount.value) must be >= expected plan price.
            from bot.payments import PLANS, paypal_total
            if str(days) in PLANS:
                expected_usd = paypal_total(PLANS[str(days)]["usd"])
                # PayPal puts captured amount at resource.seller_receivable_breakdown
                # or resource.amount.value (gross); check both.
                try:
                    gross = float(
                        resource.get("seller_receivable_breakdown", {}).get("gross_amount", {}).get("value")
                        or resource.get("amount", {}).get("value", 0)
                    )
                    if gross < expected_usd - 0.10:   # 10¢ tolerance for rounding
                        logger.error(
                            f"PayPal webhook amount mismatch: expected ${expected_usd:.2f} "
                            f"but captured ${gross:.2f} for user={user_id} days={days} "
                            f"capture={capture_id} — upgrade rejected"
                        )
                        return jsonify({"status": "ok"})
                except (TypeError, ValueError):
                    pass  # If we can't parse the amount, allow it through — don't block legit payments

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
    # PayerID is present in the old application_context flow but may be absent
    # with payment_source.paypal. Only the order token is needed to capture.
    if not paypal_order_id:
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

    # ORDER_ALREADY_CAPTURED: the CHECKOUT.ORDER.APPROVED webhook handler captured
    # the order before this redirect arrived (race condition). The upgrade is already
    # scheduled — just show success so the user doesn't see a false error page.
    if result.get("already_captured"):
        logger.info(f"PayPal return: order {paypal_order_id} already captured by webhook — showing success page")
        return _html_page(
            "✅ Payment Successful!",
            "Your Premium is being activated. Return to Telegram — you'll get a confirmation message shortly.",
            BOT_USERNAME
        ), 200

    custom_id = result.get("custom_id", "")
    capture_id = result.get("capture_id", "")
    parts = custom_id.split("_")
    days_str = "?"

    if len(parts) >= 3 and parts[0] == "tg":
        user_id = int(parts[1])
        days = int(parts[2])
        days_str = str(days)

        if result.get("pending"):
            # Capture is pending review (fraud check, new account, etc.).
            # Do NOT schedule an upgrade here — PAYMENT.CAPTURE.COMPLETED webhook
            # will fire when PayPal clears the payment and grant premium then.
            logger.info(
                f"PayPal return: capture pending for order={paypal_order_id} "
                f"user={user_id} days={days} — waiting for PAYMENT.CAPTURE.COMPLETED"
            )
            return _html_page(
                "⏳ Payment Under Review",
                f"Your payment for <strong>{days_str} days</strong> Premium is being reviewed by PayPal. "
                f"This usually takes a few minutes. Return to Telegram — "
                f"you'll get a confirmation message automatically once it clears.",
                BOT_USERNAME
            ), 200

        # Normal completed capture — schedule upgrade immediately.
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
