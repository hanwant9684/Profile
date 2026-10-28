"""
Auto-payment handlers: /upgrade command with inline plan selection,
payment method buttons, and link generation per gateway.
"""
import logging

from pyrogram import filters
from pyrogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    Message,
)

import bot.config as _bot_config
from bot.config import (
    app,
    ZAPUPI_MERCHANT_KEY,
    OXAPAY_MERCHANT_KEY,
    PAYPAL_CLIENT_ID,
    PAYPAL_CLIENT_SECRET,
    UPI_ID,
    UPI_QR_CODE_URL,
    BINANCE_ID,
    BINANCE_QR_CODE_URL,
    BINANCE_GIFT_CARD_URL,
)
from bot.payments import PLANS, create_zapupi_payment, create_oxapay_invoice, create_paypal_order, paypal_total
from bot.database import get_user


def _support_link() -> str:
    """Always return the live SUPPORT_CHAT_LINK (set after bot.start())."""
    return _bot_config.SUPPORT_CHAT_LINK or f"https://t.me/{_bot_config.BOT_USERNAME}"


def _contact_rows() -> list:
    rows = []
    if _bot_config.OWNER_LINK:
        rows.append([InlineKeyboardButton("👤 Ask owner about another payment", url=_bot_config.OWNER_LINK)])
    if _support_link():
        rows.append([InlineKeyboardButton("💬 Ask support bot about payments", url=_support_link())])
    return rows


def _payment_proof_rows() -> list:
    rows = []
    if _bot_config.OWNER_LINK:
        rows.append([InlineKeyboardButton("📨 Payment Proof to Owner", url=_bot_config.OWNER_LINK)])
    if _support_link():
        rows.append([InlineKeyboardButton("💬 Payment Proof to Chatbot", url=_support_link())])
    return rows


logger = logging.getLogger(__name__)

# ── Helpers ───────────────────────────────────────────────────────────────────

def _gateway_ok(gw: str) -> bool:
    """Return True if the gateway has its API key(s) configured."""
    if gw == "zapupi":
        return bool(ZAPUPI_MERCHANT_KEY)
    if gw == "oxapay":
        return bool(OXAPAY_MERCHANT_KEY)
    if gw == "paypal":
        return bool(PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET)
    return False


def _manual_methods_available() -> bool:
    return any((
        UPI_ID,
        UPI_QR_CODE_URL,
        BINANCE_ID,
        BINANCE_QR_CODE_URL,
        BINANCE_GIFT_CARD_URL,
    ))


def _plan_keyboard() -> InlineKeyboardMarkup:
    """Inline keyboard showing all available plans."""
    buttons = []
    for key, p in PLANS.items():
        label = f"{'🔥 ' if key == '365' else '⚡ '}{p['label']}  —  ${p['usd']:.2f}  /  ₹{p['inr']}"
        buttons.append([InlineKeyboardButton(label, callback_data=f"upg_plan_{key}")])
    buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="upg_cancel")])
    return InlineKeyboardMarkup(buttons)


def _method_keyboard(days: str) -> InlineKeyboardMarkup:
    """Inline keyboard showing payment methods for a given plan (days key)."""
    p = PLANS[days]
    rows = []

    # UPI / India — ZapUPI (supports GPay, PhonePe, Paytm, BHIM, net banking)
    if _gateway_ok("zapupi"):
        rows.append([InlineKeyboardButton(
            f"🇮🇳 UPI / India — ₹{p['inr']}",
            callback_data=f"pay_zapupi_{days}"
        )])

    # Crypto — Oxapay
    if _gateway_ok("oxapay"):
        rows.append([InlineKeyboardButton(
            f"🪙 Crypto — ${p['usd']:.2f}  (BTC/ETH/USDT…)",
            callback_data=f"pay_oxapay_{days}"
        )])

    # PayPal / Card / Apple Pay — one PayPal checkout covers all
    if _gateway_ok("paypal"):
        rows.append([InlineKeyboardButton(
            f"💲 PayPal — ${p['usd']:.2f}",
            callback_data=f"pay_paypal_{days}"
        )])
        rows.append([InlineKeyboardButton(
            f"💳 Credit/Debit Card — ${p['usd']:.2f}",
            callback_data=f"pay_card_{days}"
        )])
        rows.append([InlineKeyboardButton(
            f"🍎 Apple Pay — ${p['usd']:.2f}",
            callback_data=f"pay_apple_{days}"
        )])

    rows.append([
        InlineKeyboardButton("◀ Back", callback_data="upg_back"),
        InlineKeyboardButton("❌ Cancel", callback_data="upg_cancel"),
    ])
    if _manual_methods_available():
        rows.insert(-1, [InlineKeyboardButton(
            "✨ Other Payment Options",
            callback_data=f"other_payments_{days}",
        )])
    rows[-1:-1] = _contact_rows()
    return InlineKeyboardMarkup(rows)


def _manual_method_keyboard(days: str) -> InlineKeyboardMarkup:
    rows = []
    if UPI_ID or UPI_QR_CODE_URL:
        rows.append([InlineKeyboardButton("🇮🇳 UPI", callback_data=f"manual_detail_upi_{days}")])
    if BINANCE_ID or BINANCE_QR_CODE_URL:
        rows.append([InlineKeyboardButton("🟡 Binance Pay", callback_data=f"manual_detail_binance_{days}")])
    if BINANCE_GIFT_CARD_URL:
        rows.append([InlineKeyboardButton(
            "🎁 Binance Gift Card",
            callback_data=f"manual_detail_giftcard_{days}",
        )])
    rows.extend([
        [InlineKeyboardButton("◀ Back to payment methods", callback_data=f"upg_plan_{days}")],
        [InlineKeyboardButton("❌ Cancel", callback_data="upg_cancel")],
    ])
    return InlineKeyboardMarkup(rows)


def _manual_detail(days: str, method: str) -> tuple[str, list]:
    plan = PLANS[days]
    price = f"₹{plan['inr']}" if method == "upi" else f"${plan['usd']:.2f}"
    button_rows = []

    if method == "upi":
        text = f"Manual UPI payment for {plan['label']} — {price}\n\n"
        if UPI_ID:
            text += f"UPI ID: {UPI_ID}\n"
        if UPI_QR_CODE_URL:
            button_rows.append([InlineKeyboardButton("View UPI QR code", url=UPI_QR_CODE_URL)])
        text += "\nAfter paying, message the owner with your transaction reference. Payment is verified manually; Premium is not activated automatically."
    elif method == "binance":
        text = f"Manual Binance Pay for {plan['label']} — {price}\n\n"
        if BINANCE_ID:
            text += f"Binance Pay ID: {BINANCE_ID}\n"
        if BINANCE_QR_CODE_URL:
            button_rows.append([InlineKeyboardButton("View Binance QR code", url=BINANCE_QR_CODE_URL)])
        text += "\nAfter paying, message the owner with your transaction reference. Payment is verified manually; Premium is not activated automatically."
    else:
        text = (
            f"Binance Gift Card for {plan['label']} — {price}\n\n"
            "Open the gift-card site and message the owner to confirm the amount and instructions before purchasing. "
            "After purchase, share the receipt with the owner. Do not send gift-card redemption codes in chat."
        )
        button_rows.append([InlineKeyboardButton("Open Binance Gift Cards", url=BINANCE_GIFT_CARD_URL)])

    button_rows.extend(_payment_proof_rows())
    button_rows.append([InlineKeyboardButton("◀ Back", callback_data=f"other_payments_{days}")])
    return text, button_rows


def _plan_text() -> str:
    return (
        "💎 **Premium Plans**\n\n"
        "⚡ **Standard**\n"
        f"🔸 {PLANS['10']['label']} — ${PLANS['10']['usd']:.2f} / ₹{PLANS['10']['inr']}\n"
        f"🔸 {PLANS['30']['label']} — ${PLANS['30']['usd']:.2f} / ₹{PLANS['30']['inr']}\n"
        f"🔸 {PLANS['60']['label']} — ${PLANS['60']['usd']:.2f} / ₹{PLANS['60']['inr']}\n"
        f"🔸 {PLANS['90']['label']} — ${PLANS['90']['usd']:.2f} / ₹{PLANS['90']['inr']}\n\n"
        f"🔥 **{PLANS['365']['label']} — ${PLANS['365']['usd']:.2f} / ₹{PLANS['365']['inr']}**\n\n"
        "✅ **What you get:**\n"
        "• ♾ Unlimited downloads\n"
        "• 📦 Batch up to 50 files\n"
        "• ⚡ Fast download engine\n"
        "• 🏷 Caption tools (/capadd · /caprem)\n"
        "• 🎯 Priority support\n\n"
        "👇 **Select a plan to continue:**"
    )


# ── /upgrade command ──────────────────────────────────────────────────────────

@app.on_message(filters.command("upgrade") & filters.private)
async def upgrade_command(client, message: Message):
    user = await get_user(message.from_user.id)
    if user and user.get("role") in ("premium", "admin", "owner"):
        from datetime import timezone
        from datetime import datetime
        expiry = user.get("premium_expiry_date")
        expiry_str = ""
        if expiry:
            try:
                expiry_str = f"\n⏳ Expires: **{expiry[:10]}**"
            except Exception:
                pass
        await message.reply(
            f"✅ You already have **Premium**!{expiry_str}\n\n"
            "To extend your subscription, pick a plan below:",
            reply_markup=_plan_keyboard(),
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
        return

    await message.reply(
        _plan_text(),
        reply_markup=_plan_keyboard(),
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )


# ── Callback: plan selected ───────────────────────────────────────────────────

@app.on_callback_query(filters.regex(r"^upg_plan_(\d+)$"))
async def on_plan_selected(client, cq: CallbackQuery):
    days = cq.matches[0].group(1)
    if days not in PLANS:
        await cq.answer("Unknown plan.", show_alert=True)
        return

    p = PLANS[days]
    payment_configured = (
        any(_gateway_ok(g) for g in ("zapupi", "oxapay", "paypal"))
        or _manual_methods_available()
    )
    if not payment_configured:
        await cq.edit_message_text(
            "No checkout is configured yet. Ask the owner or support bot whether another payment method is available.",
            reply_markup=InlineKeyboardMarkup(
                _contact_rows() + [[InlineKeyboardButton("◀ Back", callback_data="upg_back")]]
            ),
        )
        await cq.answer()
        return

    text = (
        f"💎 **{p['label']} Plan**\n\n"
        f"💵 **Price:** ${p['usd']:.2f} USD  /  ₹{p['inr']} INR\n\n"
        f"👇 **Choose your payment method:**"
    )
    if _manual_methods_available():
        text += "\n\n✨ **Other Payment Options** are listed below and contain the direct payment methods configured by the owner."
    await cq.edit_message_text(
        text,
        reply_markup=_method_keyboard(days),
    )
    await cq.answer()


@app.on_callback_query(filters.regex(r"^other_payments_(\d+)$"))
async def on_other_payment_options(client, cq: CallbackQuery):
    days = cq.matches[0].group(1)
    if days not in PLANS or not _manual_methods_available():
        await cq.answer("No manual payment methods are configured.", show_alert=True)
        return
    await cq.edit_message_text(
        "✨ **Other Payment Options**\n\n"
        "Choose a direct payment method below. The owner checks these payments manually; they are not activated automatically.",
        reply_markup=_manual_method_keyboard(days),
    )
    await cq.answer()


@app.on_callback_query(filters.regex(r"^manual_detail_(upi|binance|giftcard)_(\d+)$"))
async def on_manual_payment_detail(client, cq: CallbackQuery):
    method = cq.matches[0].group(1)
    days = cq.matches[0].group(2)
    if days not in PLANS:
        await cq.answer("Unknown plan.", show_alert=True)
        return
    available = {
        "upi": bool(UPI_ID or UPI_QR_CODE_URL),
        "binance": bool(BINANCE_ID or BINANCE_QR_CODE_URL),
        "giftcard": bool(BINANCE_GIFT_CARD_URL),
    }
    if not available[method]:
        await cq.answer("This manual payment method is not configured.", show_alert=True)
        return
    text, button_rows = _manual_detail(days, method)
    await cq.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup(button_rows),
        parse_mode=None,
    )
    await cq.answer()


# ── Callback: back to plan list ───────────────────────────────────────────────

@app.on_callback_query(filters.regex(r"^upg_back$"))
async def on_upgrade_back(client, cq: CallbackQuery):
    await cq.edit_message_text(
        _plan_text(),
        reply_markup=_plan_keyboard(),
    )
    await cq.answer()


# ── Callback: cancel ──────────────────────────────────────────────────────────

@app.on_callback_query(filters.regex(r"^upg_cancel$"))
async def on_upgrade_cancel(client, cq: CallbackQuery):
    await cq.edit_message_text(
        "❌ **Payment cancelled.**\n\n"
        "No charge was made. Use /upgrade anytime to try again.",
    )
    await cq.answer("Cancelled — no charge made.")


# ── Callback: generate payment link ──────────────────────────────────────────

async def _send_payment_link(cq: CallbackQuery, gateway: str, days: str):
    """Generate a payment link and edit the message with it."""
    if days not in PLANS:
        await cq.answer("Unknown plan.", show_alert=True)
        return

    if not _gateway_ok(gateway if gateway not in ("card", "apple") else "paypal"):
        await cq.answer("This payment method is not configured yet.", show_alert=True)
        return

    await cq.answer("⏳ Generating your payment link…")
    await cq.edit_message_text("⏳ Generating your payment link, please wait…")

    user_id = cq.from_user.id
    days_int = int(days)
    p = PLANS[days]
    fee_note = ""  # shown on the payment page only for PayPal

    try:
        if gateway == "zapupi":
            result = await create_zapupi_payment(user_id, days_int)
            method_label = "🇮🇳 UPI — ZapUPI"
            amount_str = f"₹{p['inr']}"

        elif gateway == "oxapay":
            result = await create_oxapay_invoice(user_id, days_int)
            method_label = "🪙 Crypto (Oxapay)"
            amount_str = f"${p['usd']:.2f}"

        elif gateway in ("paypal", "card", "apple"):
            base = p["usd"]
            total = paypal_total(base)
            result = await create_paypal_order(user_id, days_int, charge_amount=total)
            if gateway == "card":
                method_label = "💳 Credit/Debit Card (PayPal)"
            elif gateway == "apple":
                method_label = "🍎 Apple Pay (PayPal)"
            else:
                method_label = "💲 PayPal"
            amount_str = f"${base:.2f}"
            fee_note = ""

        else:
            await cq.edit_message_text("Unknown gateway.")
            return

    except Exception as e:
        logger.error(f"Payment link generation error: {e}")
        await cq.edit_message_text(
            "❌ Failed to generate payment link. Please try again or contact support.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔄 Try Again", callback_data=f"upg_plan_{days}"),
                InlineKeyboardButton("Support", url=_support_link()),
            ]])
        )
        return

    if not result.get("ok"):
        err = result.get("error", "Unknown error")
        logger.error(f"Gateway error ({gateway}): {err}")

        # Map raw gateway errors to user-friendly messages
        if gateway == "zapupi":
            user_msg = "UPI payments are temporarily unavailable. Please try another payment method or contact support."
        else:
            user_msg = "Could not generate a payment link. Please try again or contact support."

        await cq.edit_message_text(
            f"❌ {user_msg}",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔄 Try Again", callback_data=f"upg_plan_{days}"),
                InlineKeyboardButton("Support", url=_support_link()),
            ]])
        )
        return

    pay_url = result["url"]
    await cq.edit_message_text(
        f"✅ **Your Payment Link is Ready!**\n\n"
        f"📋 **Plan:** {p['label']}\n"
        f"💰 **Amount:** {amount_str}\n"
        f"💳 **Method:** {method_label}\n"
        f"{fee_note}\n"
        f"👇 **Tap the button below to pay:**\n\n"
        f"⚠️ This link expires in 1 hour and is unique to you.\n"
        f"✅ Premium is activated **automatically** once payment is confirmed.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(f"💳 Pay {amount_str} Now", url=pay_url)],
            [
                InlineKeyboardButton("◀ Change Plan", callback_data="upg_back"),
                InlineKeyboardButton("❌ Didn't Pay", callback_data="upg_cancel"),
            ],
            [
                InlineKeyboardButton("👑 Owner", url="https://t.me/Owner_wolfy"),
                InlineKeyboardButton("💬 Support", url=_support_link()),
            ],
        ]),
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )


@app.on_callback_query(filters.regex(r"^pay_(zapupi|oxapay|paypal|card|apple)_(\d+)$"))
async def on_pay_method(client, cq: CallbackQuery):
    gateway = cq.matches[0].group(1)
    days = cq.matches[0].group(2)
    await _send_payment_link(cq, gateway, days)
