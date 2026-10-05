"""
notifier.py — Instant Alert System for F&O Signal Advisor
Sends BUY CALL / BUY PUT alerts the moment a new signal fires.

SUPPORTED CHANNELS (both free, both work on Render free tier):
  1. Telegram Bot — Instant phone notification with full signal details
  2. Gmail Email  — Rich HTML email with strike, premium, target, SL

SETUP (set these as Render Environment Variables or in .env):
  Telegram:
    TELEGRAM_BOT_TOKEN  — from @BotFather (create a bot → /newbot)
    TELEGRAM_CHAT_ID    — your personal chat ID (message @userinfobot to get it)

  Gmail:
    ALERT_EMAIL_FROM     — your Gmail address (sender)
    ALERT_EMAIL_TO       — recipient email (can be same Gmail or phone carrier email for SMS)
    ALERT_EMAIL_PASSWORD — Gmail App Password (NOT your login password)
                           Go to: myaccount.google.com → Security → 2-Step Verification → App passwords
"""

import logging
import os
import smtplib
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

# ─── Environment Variables ────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN  = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID    = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

ALERT_EMAIL_FROM     = os.environ.get("ALERT_EMAIL_FROM", "").strip()
ALERT_EMAIL_TO       = os.environ.get("ALERT_EMAIL_TO", "").strip()
ALERT_EMAIL_PASSWORD = os.environ.get("ALERT_EMAIL_PASSWORD", "").strip()


# ─── Message Builder ──────────────────────────────────────────────────────────

def _ist_now() -> str:
    return datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%d %b %Y %I:%M %p IST")


def _build_message(signal: dict) -> dict:
    """Build Telegram text + HTML email body from a signal dict."""
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
    dir_text = "📈 BUY CALL (CE)" if is_call else "📉 BUY PUT (PE)"
    clr      = "#10b981" if is_call else "#ef4444"

    # ── Telegram (Markdown) ──────────────────────────────────────────────────
    tg_text = (
        f"{emoji} *F\\&O SIGNAL ALERT — {sym}*\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"*{dir_text}*\n"
        f"Strike: `{strike} {otype}` | Expiry: `{expiry}`\n"
        f"\n"
        f"💰 Entry Premium: `₹{prem}`\n"
        f"🎯 Target: `₹{target}` \\(\\+13%\\)\n"
        f"🛑 Stop Loss: `₹{sl}` \\(₹1300 max loss\\)\n"
        f"\n"
        f"📊 Spot LTP: `₹{ltp:,}` | Bias: `{bias:+}/±10` | Conf: `{conf}%`\n"
        f"⏱ {now}\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"_Enter on Groww F\\&O\\. Exit at \\+13% or 3:00 PM IST auto square\\-off\\._"
    )

    # ── Email Subject ────────────────────────────────────────────────────────
    subject = f"{emoji} {sym} {dir_text} @ ₹{prem} — {now}"

    # ── Email HTML Body ──────────────────────────────────────────────────────
    html = f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#0d1117;font-family:'Segoe UI',Arial,sans-serif;">
<div style="max-width:500px;margin:30px auto;background:#161b22;border-radius:14px;overflow:hidden;border:2px solid {clr};">

  <!-- Header -->
  <div style="background:{clr};padding:18px 22px;">
    <div style="font-size:22px;font-weight:800;color:#fff;">{emoji} {dir_text}</div>
    <div style="font-size:13px;color:rgba(255,255,255,.8);margin-top:4px;">{sym} &nbsp;|&nbsp; {now}</div>
  </div>

  <!-- Body -->
  <div style="padding:22px;">
    <table style="width:100%;border-collapse:collapse;font-size:14px;">
      <tr style="border-bottom:1px solid rgba(255,255,255,.07);">
        <td style="padding:10px 0;color:#8b949e;">Strike</td>
        <td style="padding:10px 0;font-weight:700;color:#e6edf3;">{strike} {otype} &nbsp;<span style="color:{clr};font-size:12px;">{expiry}</span></td>
      </tr>
      <tr style="border-bottom:1px solid rgba(255,255,255,.07);">
        <td style="padding:10px 0;color:#8b949e;">Entry Premium</td>
        <td style="padding:10px 0;font-weight:700;color:#e6edf3;font-size:18px;">₹{prem}</td>
      </tr>
      <tr style="border-bottom:1px solid rgba(255,255,255,.07);">
        <td style="padding:10px 0;color:#8b949e;">🎯 Target (+13%)</td>
        <td style="padding:10px 0;font-weight:700;color:#10b981;font-size:16px;">₹{target}</td>
      </tr>
      <tr style="border-bottom:1px solid rgba(255,255,255,.07);">
        <td style="padding:10px 0;color:#8b949e;">🛑 Stop Loss</td>
        <td style="padding:10px 0;font-weight:700;color:#ef4444;font-size:16px;">₹{sl} &nbsp;<span style="font-size:12px;color:#8b949e;">(₹1300 max loss)</span></td>
      </tr>
      <tr style="border-bottom:1px solid rgba(255,255,255,.07);">
        <td style="padding:10px 0;color:#8b949e;">Spot LTP</td>
        <td style="padding:10px 0;font-weight:700;color:#e6edf3;">₹{ltp:,}</td>
      </tr>
      <tr style="border-bottom:1px solid rgba(255,255,255,.07);">
        <td style="padding:10px 0;color:#8b949e;">Bias Score</td>
        <td style="padding:10px 0;font-weight:700;color:#e6edf3;">{bias:+} / ±10</td>
      </tr>
      <tr>
        <td style="padding:10px 0;color:#8b949e;">AI Confidence</td>
        <td style="padding:10px 0;font-weight:700;color:{clr};">{conf}%</td>
      </tr>
    </table>

    <!-- Note -->
    <div style="margin-top:18px;padding:12px 14px;background:rgba(255,255,255,.04);border-radius:8px;font-size:12px;color:#8b949e;line-height:1.6;">
      Enter on <strong style="color:#e6edf3;">Groww F&amp;O</strong>. Set a limit order within the bid-ask spread.
      Exit at <strong style="color:#10b981;">+13% profit target</strong> or auto square-off at <strong style="color:#e6edf3;">3:00 PM IST</strong>.
      Stop loss at <strong style="color:#ef4444;">₹1,300 max loss</strong> per trade.
    </div>
  </div>

  <!-- Footer -->
  <div style="padding:12px 22px;border-top:1px solid rgba(255,255,255,.07);font-size:11px;color:#484f58;text-align:center;">
    AI F&amp;O Signal Advisor • Powered by Google Gemini + NSE Live Data
  </div>
</div>
</body></html>"""

    return {"tg": tg_text, "subject": subject, "html": html}


# ─── Telegram ─────────────────────────────────────────────────────────────────

def send_telegram_alert(signal: dict) -> bool:
    """Send signal alert via Telegram Bot. Returns True if successfully sent."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.debug("Telegram not configured — skipping (set TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID)")
        return False
    try:
        import requests
        msg = _build_message(signal)
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        resp = requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": msg["tg"],
            "parse_mode": "MarkdownV2",
        }, timeout=12)
        if resp.ok:
            logger.info(
                f"✅ Telegram alert sent: {signal.get('symbol')} {signal.get('signal')} "
                f"@ ₹{signal.get('entry_premium')}"
            )
            return True
        logger.warning(f"Telegram alert failed: HTTP {resp.status_code} — {resp.text[:300]}")
    except Exception as e:
        logger.warning(f"Telegram alert error: {e}")
    return False


# ─── Gmail Email ──────────────────────────────────────────────────────────────

def send_email_alert(signal: dict) -> bool:
    """Send signal alert via Gmail SMTP. Returns True if successfully sent."""
    if not ALERT_EMAIL_FROM or not ALERT_EMAIL_PASSWORD or not ALERT_EMAIL_TO:
        logger.debug("Email not configured — skipping (set ALERT_EMAIL_FROM/TO/PASSWORD)")
        return False
    try:
        msg = _build_message(signal)
        mail = MIMEMultipart("alternative")
        mail["Subject"] = msg["subject"]
        mail["From"]    = f"F&O Signal Advisor <{ALERT_EMAIL_FROM}>"
        mail["To"]      = ALERT_EMAIL_TO
        mail.attach(MIMEText(msg["html"], "html", "utf-8"))

        with smtplib.SMTP("smtp.gmail.com", 587, timeout=20) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.login(ALERT_EMAIL_FROM, ALERT_EMAIL_PASSWORD)
            smtp.send_message(mail)

        logger.info(
            f"✅ Email alert sent to {ALERT_EMAIL_TO}: "
            f"{signal.get('symbol')} {signal.get('signal')} "
            f"@ ₹{signal.get('entry_premium')}"
        )
        return True
    except Exception as e:
        logger.warning(f"Email alert error: {e}")
    return False


# ─── Main Entry Point ─────────────────────────────────────────────────────────

def send_signal_alert(signal: dict):
    """
    Send BUY CALL / BUY PUT alert to all configured channels simultaneously.
    Called by app.py after a new trade signal is confirmed and logged to the DB.
    WAIT signals are silently ignored — only actual trade entries trigger alerts.
    """
    if signal.get("signal") not in ("BUY_CALL", "BUY_PUT"):
        return  # Only alert on actual trade recommendations

    import threading
    # Fire alerts in background threads so they never slow down the main API response
    threading.Thread(target=send_telegram_alert, args=(signal,), daemon=True).start()
    threading.Thread(target=send_email_alert,    args=(signal,), daemon=True).start()
