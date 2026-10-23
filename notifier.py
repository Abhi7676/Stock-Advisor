"""
notifier.py — Telegram Alert System for F&O Signal Advisor
Sends BUY CALL / BUY PUT alerts instantly to your phone via Telegram Bot.

SETUP (set these as Render Environment Variables or in .env):
  TELEGRAM_BOT_TOKEN  — from @BotFather on Telegram  (/newbot → copy token)
  TELEGRAM_CHAT_ID    — your chat ID  (message @userinfobot → copy the ID number)

After creating the bot, send it /start once so it can message you.
"""

import html
import logging
import os
import threading
from datetime import datetime, timezone, timedelta
import config

logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

# ─── Multi-Recipient Configuration ───────────────────────────────────────────
# Add additional recipients by setting TELEGRAM_BOT_TOKEN_2 / TELEGRAM_CHAT_ID_2
# (and optionally _3, _4, ...) in your Render environment variables.
# If TELEGRAM_BOT_TOKEN_2 is empty, the primary bot token is reused for that recipient.
TELEGRAM_RECIPIENTS = [
    {
        "name":    "Primary (You)",
        "token":   os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(),
        "chat_id": os.environ.get("TELEGRAM_CHAT_ID", "").strip(),
    },
    {
        "name":    "Sammed",
        "token":   (os.environ.get("TELEGRAM_BOT_TOKEN_2") or os.environ.get("TELEGRAM_BOT_TOKEN", "")).strip(),
        "chat_id": os.environ.get("TELEGRAM_CHAT_ID_2", "").strip(),
    },
]
# Filter out empty recipients (not yet configured)
_ACTIVE_RECIPIENTS = [
    r for r in TELEGRAM_RECIPIENTS
    if r["token"] and r["chat_id"]
]


def _ist_now() -> str:
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%d %b %Y %I:%M %p IST")
    except Exception:
        return datetime.now(timezone(timedelta(hours=5, minutes=30))).strftime("%d %b %Y %I:%M %p IST")


def _build_telegram_html(signal: dict) -> str:
    sym    = html.escape(str(signal.get("symbol", "?")))
    sig    = signal.get("signal", "?")
    strike = signal.get("strike", "?")
    otype  = signal.get("option_type", "?")
    prem   = signal.get("entry_premium") or 0
    target = signal.get("target_premium") or 0
    sl     = signal.get("stop_loss_premium") or 0
    conf   = signal.get("confidence", 0)
    expiry = html.escape(str(signal.get("nearest_expiry", "?")))
    bias   = signal.get("bias_score", 0)
    ltp    = signal.get("ltp", 0)
    now    = _ist_now()

    is_call      = sig == "BUY_CALL"
    header_emoji = "🟢" if is_call else "🔴"
    dir_emoji    = "📈" if is_call else "📉"
    dir_label    = "BUY CALL (CE)" if is_call else "BUY PUT (PE)"
    conf_badge   = "🔥 High Conviction" if conf >= 80 else "⚡ Moderate Conviction"

    sl_max    = getattr(config, "STOP_LOSS_AMOUNTS", {}).get(signal.get("symbol"), 2200)
    bias_sign = f"+{bias}" if bias > 0 else f"{bias}"

    # Extract short 1-line key technical rationale
    reasoning = signal.get("reasoning") or ""
    rationale_line = ""
    if reasoning:
        for line in reasoning.split("\n"):
            line_s = line.strip()
            if ("Supertrend" in line_s and "MACD" in line_s) or line_s.startswith("• 5-min Trend") or line_s.startswith("• Technical Indicators"):
                clean = line_s.replace("• ", "").replace("✅", "").strip()
                rationale_line = html.escape(clean)
                break
        if not rationale_line:
            bullets = [l.strip().replace("• ", "") for l in reasoning.split("\n") if l.strip().startswith("•")]
            if bullets:
                rationale_line = html.escape(bullets[0][:80])

    lines = [
        f"{header_emoji} <b>F&amp;O SIGNAL ALERT — {sym}</b>",
        f"━━━━━━━━━━━━━━━━━━━━━",
        f"{dir_emoji} <b>{dir_label}</b>",
        f"🎯 <b>Strike:</b> <code>{strike} {otype}</code> | <b>Expiry:</b> <code>{expiry}</code>",
        f"",
        f"💰 <b>Entry Premium:</b> <code>₹{prem}</code>",
        f"🎯 <b>Target (+13%):</b> <code>₹{target}</code>",
        f"🛑 <b>Stop Loss:</b> <code>₹{sl}</code> <i>(₹{sl_max:,} max loss cap)</i>",
        f"",
        f"🤖 <b>AI Confidence:</b> <b>{conf}%</b> <i>({conf_badge})</i>",
        f"📊 <b>Spot:</b> ₹{ltp:,.2f} | <b>Bias:</b> {bias_sign}/±5",
    ]
    if rationale_line:
        lines.append(f"🧠 <b>AI Rationale:</b> <i>{rationale_line}</i>")

    lines.extend([
        f"⏱ <b>Time:</b> {now}",
        f"━━━━━━━━━━━━━━━━━━━━━",
        f"📱 <i>Enter on Groww F&amp;O. Exit at +13% min profit or 3:40 PM IST square-off.</i>",
    ])
    return "\n".join(lines)


def _build_telegram_plain(signal: dict) -> str:
    sym    = signal.get("symbol", "?")
    sig    = signal.get("signal", "?")
    strike = signal.get("strike", "?")
    otype  = signal.get("option_type", "?")
    prem   = signal.get("entry_premium") or 0
    target = signal.get("target_premium") or 0
    sl     = signal.get("stop_loss_premium") or 0
    conf   = signal.get("confidence", 0)
    expiry = signal.get("nearest_expiry", "?")
    bias   = signal.get("bias_score", 0)
    ltp    = signal.get("ltp", 0)
    now    = _ist_now()

    is_call      = sig == "BUY_CALL"
    header_emoji = "🟢" if is_call else "🔴"
    dir_emoji    = "📈" if is_call else "📉"
    dir_label    = "BUY CALL (CE)" if is_call else "BUY PUT (PE)"
    conf_badge   = "High Conviction" if conf >= 80 else "Moderate Conviction"
    sl_max       = getattr(config, "STOP_LOSS_AMOUNTS", {}).get(sym, 2200)
    bias_sign    = f"+{bias}" if bias > 0 else f"{bias}"

    # Extract short 1-line key technical rationale
    reasoning = signal.get("reasoning") or ""
    rationale_line = ""
    if reasoning:
        for line in reasoning.split("\n"):
            line_s = line.strip()
            if ("Supertrend" in line_s and "MACD" in line_s) or line_s.startswith("• 5-min Trend") or line_s.startswith("• Technical Indicators"):
                rationale_line = line_s.replace("• ", "").replace("✅", "").strip()
                break
        if not rationale_line:
            bullets = [l.strip().replace("• ", "") for l in reasoning.split("\n") if l.strip().startswith("•")]
            if bullets:
                rationale_line = bullets[0][:80]

    lines = [
        f"{header_emoji} F&O SIGNAL ALERT — {sym}",
        f"━━━━━━━━━━━━━━━━━━━━━",
        f"{dir_emoji} {dir_label}",
        f"🎯 Strike: {strike} {otype} | Expiry: {expiry}",
        f"",
        f"💰 Entry Premium: ₹{prem}",
        f"🎯 Target (+13%): ₹{target}",
        f"🛑 Stop Loss: ₹{sl} (₹{sl_max:,} max loss cap)",
        f"",
        f"🤖 AI Confidence: {conf}% ({conf_badge})",
        f"📊 Spot: ₹{ltp:,.2f} | Bias: {bias_sign}/±5",
    ]
    if rationale_line:
        lines.append(f"🧠 AI Rationale: {rationale_line}")

    lines.extend([
        f"⏱ Time: {now}",
        f"━━━━━━━━━━━━━━━━━━━━━",
        f"📱 Enter on Groww F&O. Exit at +13% min profit or 3:40 PM IST square-off.",
    ])
    return "\n".join(lines)


# Backward compatibility alias
_build_telegram_message = _build_telegram_html


def _get_credentials():
    """Returns (token, chat_id) for the primary recipient (backward compat)."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    return token, chat_id


def _get_all_recipients() -> list:
    """Returns list of all active (token, chat_id, name) tuples — primary + any extras."""
    primary_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    recipients = []
    # Always include primary if configured
    primary_chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if primary_token and primary_chat:
        recipients.append({"name": "Primary", "token": primary_token, "chat_id": primary_chat})
    # Additional recipients: _2, _3, _4 ...
    for i in range(2, 6):
        extra_token   = (os.environ.get(f"TELEGRAM_BOT_TOKEN_{i}") or primary_token or "").strip()
        extra_chat_id = os.environ.get(f"TELEGRAM_CHAT_ID_{i}", "").strip()
        extra_name    = os.environ.get(f"TELEGRAM_RECIPIENT_NAME_{i}", f"Recipient {i}").strip()
        if extra_token and extra_chat_id:
            recipients.append({"name": extra_name, "token": extra_token, "chat_id": extra_chat_id})
    return recipients


def _send_telegram(signal: dict):
    """Internal — sends the Telegram message to ALL configured recipients. Runs in a background thread."""
    import requests
    recipients = _get_all_recipients()
    if not recipients:
        logger.debug("Telegram not configured — set TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID")
        return
    html_text  = _build_telegram_html(signal)
    plain_text = _build_telegram_plain(signal)
    for r in recipients:
        token   = r["token"]
        chat_id = r["chat_id"]
        name    = r["name"]
        try:
            resp = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": html_text, "parse_mode": "HTML"},
                timeout=12,
            )
            if resp.ok:
                logger.info(
                    f"✅ Telegram alert sent to {name}: {signal.get('symbol')} "
                    f"{signal.get('signal')} @ ₹{signal.get('entry_premium')} (Confidence: {signal.get('confidence')}%)"
                )
                continue
            logger.warning(f"Telegram HTML failed for {name} ({resp.status_code}) — retrying plain text")
            fallback_resp = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": plain_text},
                timeout=12,
            )
            if fallback_resp.ok:
                logger.info(f"✅ Telegram plain-text alert delivered to {name}: {signal.get('symbol')}")
            else:
                logger.warning(f"Telegram alert delivery failed for {name}: {fallback_resp.text[:200]}")
        except Exception as e:
            logger.warning(f"Telegram alert error for {name}: {e}")



def send_signal_alert(signal: dict):
    """
    Fire a Telegram alert when a new BUY CALL or BUY PUT signal is confirmed.
    Runs in a background daemon thread — never blocks the main API response.
    WAIT signals are silently ignored.
    """
    if signal.get("signal") not in ("BUY_CALL", "BUY_PUT"):
        return
    threading.Thread(target=_send_telegram, args=(signal,), daemon=True).start()


def test_telegram_connection() -> dict:
    """Send a TEST ALERT to ALL configured recipients to verify Telegram credentials."""
    import requests
    recipients = _get_all_recipients()
    if not recipients:
        return {
            "status": "error",
            "message": "TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is missing in environment variables."
        }
    now = _ist_now()
    test_text_html = (
        "\U0001f9ea <b>\u26a0\ufe0f TEST ALERT \u2014 F&amp;O Signal Advisor</b>\n"
        "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
        "\u2705 <b>Telegram connection is working!</b>\n"
        "\U0001f4f1 This is a <b>test notification</b> triggered from the app dashboard.\n"
        "<i>Real trade signals will be sent in this same format when a BUY CALL or BUY PUT signal is confirmed.</i>\n"
        "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
        f"\u23f1 <b>Sent at:</b> {now}"
    )
    test_text_plain = (
        f"\U0001f9ea TEST ALERT \u2014 F&O Signal Advisor\n"
        f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
        f"\u2705 Telegram connection is working!\n"
        f"This is a test notification triggered from the app dashboard.\n"
        f"Real trade signals will be sent in this same format.\n"
        f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
        f"\u23f1 Sent at: {now}"
    )
    delivered = []
    failed    = []
    for r in recipients:
        token   = r["token"]
        chat_id = r["chat_id"]
        name    = r["name"]
        try:
            resp = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": test_text_html, "parse_mode": "HTML"},
                timeout=12,
            )
            if resp.ok:
                delivered.append(name)
                continue
            # Fallback plain
            f_resp = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": test_text_plain},
                timeout=12,
            )
            if f_resp.ok:
                delivered.append(f"{name} (plain text)")
            else:
                failed.append(f"{name}: {f_resp.status_code}")
        except Exception as e:
            failed.append(f"{name}: {e}")

    if delivered and not failed:
        return {"status": "ok",    "message": f"Test alert delivered to: {', '.join(delivered)}"}
    if delivered:
        return {"status": "ok",    "message": f"Delivered to {', '.join(delivered)}. Failed: {', '.join(failed)}"}
    return     {"status": "error", "message": f"All deliveries failed: {', '.join(failed)}"}

