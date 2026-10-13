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

    sl_max    = getattr(config, "STOP_LOSS_AMOUNTS", {}).get(signal.get("symbol"), 1600)
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
    sl_max       = getattr(config, "STOP_LOSS_AMOUNTS", {}).get(sym, 1600)
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
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    return token, chat_id


def _send_telegram(signal: dict):
    """Internal — sends the Telegram message. Runs in a background thread."""
    token, chat_id = _get_credentials()
    if not token or not chat_id:
        logger.debug("Telegram not configured — set TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID")
        return
    try:
        import requests
        html_text = _build_telegram_html(signal)
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": html_text, "parse_mode": "HTML"},
            timeout=12,
        )
        if resp.ok:
            logger.info(
                f"✅ Telegram HTML alert sent: {signal.get('symbol')} "
                f"{signal.get('signal')} @ ₹{signal.get('entry_premium')} (Confidence: {signal.get('confidence')}%)"
            )
            return

        logger.warning(f"Telegram HTML parse returned {resp.status_code}: {resp.text[:200]} — retrying plain text")
        plain_text = _build_telegram_plain(signal)
        fallback_resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": plain_text},
            timeout=12,
        )
        if fallback_resp.ok:
            logger.info(f"✅ Telegram plain-text alert delivered successfully: {signal.get('symbol')}")
        else:
            logger.warning(f"Telegram alert delivery failed: {fallback_resp.text[:300]}")
    except Exception as e:
        logger.warning(f"Telegram alert error: {e}")


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
    """Send a test signal alert to verify Telegram credentials."""
    token, chat_id = _get_credentials()
    if not token or not chat_id:
        return {
            "status": "error",
            "message": "TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is missing in environment variables."
        }
    try:
        import requests
        sample_signal = {
            "symbol": "NIFTY",
            "signal": "BUY_CALL",
            "strike": 22550,
            "option_type": "CE",
            "entry_premium": 35.15,
            "target_premium": 40.50,
            "stop_loss_premium": 10.53,
            "confidence": 85,
            "nearest_expiry": "13-Oct-2026",
            "bias_score": 4,
            "ltp": 22527.45,
            "reasoning": "• 5-min Trend: Supertrend BUY & MACD BULLISH both agree\n• PCR: 1.22 Bullish put writing floor",
        }
        text = _build_telegram_html(sample_signal)
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=12,
        )
        if resp.ok:
            return {"status": "ok", "message": "Test alert delivered to Telegram successfully!"}
        # Fallback test with plain text
        plain = _build_telegram_plain(sample_signal)
        f_resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": plain},
            timeout=12,
        )
        if f_resp.ok:
            return {"status": "ok", "message": "Test alert delivered using plain-text format!"}
        return {"status": "error", "message": f"Telegram API error {resp.status_code}: {resp.text}"}
    except Exception as e:
        return {"status": "error", "message": str(e)}
