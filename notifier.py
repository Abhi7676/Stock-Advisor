"""
notifier.py — Telegram Alert System for F&O Signal Advisor
Sends BUY CALL / BUY PUT alerts instantly to your phone via Telegram Bot.

SETUP (set these as Render Environment Variables or in .env):
  TELEGRAM_BOT_TOKEN  — from @BotFather on Telegram  (/newbot → copy token)
  TELEGRAM_CHAT_ID    — your chat ID  (message @userinfobot → copy the ID number)

After creating the bot, send it /start once so it can message you.
"""

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


def _build_telegram_message(signal: dict) -> str:
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

    is_call  = sig == "BUY_CALL"
    emoji    = "🟢" if is_call else "🔴"
    dir_text = "📈 BUY CALL \\(CE\\)" if is_call else "📉 BUY PUT \\(PE\\)"

    # MarkdownV2 requires escaping: . - + ( ) ! = > < |
    def esc(v):
        return str(v).replace(".", "\\.").replace("-", "\\-").replace("+", "\\+").replace("(", "\\(").replace(")", "\\)").replace("!", "\\!").replace("=", "\\=")

    bias_str = esc(f"{bias:+}")
    ltp_str  = esc(f"{ltp:,}")
    prem_str = esc(str(prem))
    tgt_str  = esc(str(target))
    sl_str   = esc(str(sl))
    conf_str = esc(str(conf))
    strike_str = esc(str(strike))
    expiry_str = esc(str(expiry))

    sl_max   = getattr(config, "STOP_LOSS_AMOUNTS", {}).get(sym, 1600)

    return (
        f"{emoji} *F\\&O SIGNAL ALERT — {esc(sym)}*\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"*{dir_text}*\n"
        f"Strike: `{strike_str} {otype}` \\| Expiry: `{expiry_str}`\n"
        f"\n"
        f"💰 Entry Premium: `₹{prem_str}`\n"
        f"🎯 Target: `₹{tgt_str}` \\(\\+13% Min Profit\\)\n"
        f"🛑 Stop Loss: `₹{sl_str}` \\(₹{sl_max} max loss\\)\n"
        f"\n"
        f"📊 Spot: `₹{ltp_str}` \\| Bias: `{bias_str}/±10` \\| Conf: `{conf_str}%`\n"
        f"⏱ {esc(now)}\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"_Enter on Groww F\\&O\\. Exit at \\+13% min profit or 1:30 PM IST cutoff\\._"
    )


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
        text = _build_telegram_message(signal)
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "MarkdownV2"},
            timeout=12,
        )
        if resp.ok:
            logger.info(
                f"✅ Telegram alert sent: {signal.get('symbol')} "
                f"{signal.get('signal')} @ ₹{signal.get('entry_premium')}"
            )
        elif resp.status_code == 400:
            logger.warning(f"Telegram MarkdownV2 parse failed: {resp.text[:200]} — retrying plain text")
            sym = signal.get("symbol", "?")
            sig = signal.get("signal", "?")
            strike = signal.get("strike", "?")
            otype = signal.get("option_type", "?")
            prem = signal.get("entry_premium") or 0
            tgt = signal.get("target_premium") or 0
            sl = signal.get("stop_loss_premium") or 0
            plain_text = (
                f"🔔 F&O SIGNAL ALERT — {sym}\n"
                f"{sig}: {strike} {otype}\n"
                f"Entry: ₹{prem} | Target: ₹{tgt} (+13%) | SL: ₹{sl}\n"
                f"Spot: ₹{signal.get('ltp', 0):,} | Conf: {signal.get('confidence', 0)}%\n"
                f"Time: {_ist_now()}"
            )
            fallback_resp = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": plain_text},
                timeout=12,
            )
            if fallback_resp.ok:
                logger.info(f"✅ Telegram plain-text alert delivered successfully: {sym} {sig}")
            else:
                logger.warning(f"Telegram plain-text fallback failed: {fallback_resp.text[:300]}")
        else:
            logger.warning(f"Telegram alert failed: HTTP {resp.status_code} — {resp.text[:300]}")
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
            "strike": 24800,
            "option_type": "CE",
            "entry_premium": 150.0,
            "target_premium": 169.5,
            "stop_loss_premium": 130.0,
            "confidence": 85,
            "nearest_expiry": "Weekly",
            "bias_score": 6,
            "ltp": 24820.5,
        }
        text = (
            "🔔 *F\\&O Signal Advisor \\(Test Alert\\)*\n\n"
            + _build_telegram_message(sample_signal)
        )
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "MarkdownV2"},
            timeout=12,
        )
        if resp.ok:
            return {"status": "ok", "message": "Test alert delivered to Telegram successfully!"}
        return {"status": "error", "message": f"Telegram API error {resp.status_code}: {resp.text}"}
    except Exception as e:
        return {"status": "error", "message": str(e)}
