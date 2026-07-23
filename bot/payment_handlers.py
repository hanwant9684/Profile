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
)
from bot.payments import PLANS, create_zapupi_payment, create_oxapay_invoice, create_paypal_order
from bot.database import get_user


def _support_link() -> str:
    """Always return the live SUPPORT_CHAT_LINK (set after bot.start())."""
    return _bot_config.SUPPORT_CHAT_LINK or f"https://t.me/{_bot_config.BOT_USERNAME}"

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


def _plan_keyboard() -> InlineKeyboardMarkup:
    """Inline keyboard showing all available plans."""
    buttons = []
    for key, p in PLANS.items():
        label = f"{'🔥 ' if key == '365' else '⚡ '}{p['label']}  —  ${p['usd']:.0f}  /  ₹{p['inr']}"
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
            f"🪙 Crypto — ${p['usd']:.0f}  (BTC/ETH/USDT…)",
            callback_data=f"pay_oxapay_{days}"
        )])

    # PayPal / Card / Apple Pay — one PayPal checkout covers all
    if _gateway_ok("paypal"):
        rows.append([InlineKeyboardButton(
            f"💲 PayPal — ${p['usd']:.0f}",
            callback_data=f"pay_paypal_{days}"
        )])
        rows.append([InlineKeyboardButton(
            f"💳 Credit/Debit Card — ${p['usd']:.0f}",
            callback_data=f"pay_card_{days}"
        )])
        rows.append([InlineKeyboardButton(
            f"🍎 Apple Pay — ${p['usd']:.0f}",
            callback_data=f"pay_apple_{days}"
        )])

    rows.append([
        InlineKeyboardButton("◀ Back", callback_data="upg_back"),
        InlineKeyboardButton("❌ Cancel", callback_data="upg_cancel"),
    ])
    return InlineKeyboardMarkup(rows)


def _plan_text() -> str:
    return (
        "💎 **Premium Plans**\n\n"
        "⚡ **Standard**\n"
        "🔸 10 days — $3 / ₹300\n"
        "🔸 30 days — $4 / ₹400\n"
        "🔸 60 days — $8 / ₹800\n"
        "🔸 90 days — $12 / ₹1200\n\n"
        "🔥 **1 Year — $45 / ₹4500**\n\n"
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
    any_configured = any(_gateway_ok(g) for g in ("zapupi", "oxapay", "paypal"))
    if not any_configured:
        await cq.answer(
            "No payment gateways are configured yet. Contact the owner.",
            show_alert=True
        )
        return

    text = (
        f"💎 **{p['label']} Plan**\n\n"
        f"💵 **Price:** ${p['usd']:.0f} USD  /  ₹{p['inr']} INR\n\n"
        f"👇 **Choose your payment method:**"
    )
    await cq.edit_message_text(
        text,
        reply_markup=_method_keyboard(days),
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

    try:
        if gateway == "zapupi":
            result = await create_zapupi_payment(user_id, days_int)
            method_label = "🇮🇳 UPI — ZapUPI"
            amount_str = f"₹{p['inr']}"

        elif gateway == "oxapay":
            result = await create_oxapay_invoice(user_id, days_int)
            method_label = "🪙 Crypto (Oxapay)"
            amount_str = f"${p['usd']:.0f}"

        elif gateway in ("paypal", "card", "apple"):
            result = await create_paypal_order(user_id, days_int)
            if gateway == "card":
                method_label = "💳 Credit/Debit Card (PayPal)"
            elif gateway == "apple":
                method_label = "🍎 Apple Pay (PayPal)"
            else:
                method_label = "💲 PayPal"
            amount_str = f"${p['usd']:.0f}"

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
        await cq.edit_message_text(
            f"❌ Payment link failed: {err}\n\nPlease try again or contact support.",
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
        f"💳 **Method:** {method_label}\n\n"
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
