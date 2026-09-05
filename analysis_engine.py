"""
analysis_engine.py — Multi-Factor Signal Analysis Engine
Computes technical indicators, OI analysis, PCR interpretation,
support/resistance, and produces a structured market signal summary.
"""

import logging
import numpy as np
import pandas as pd

import config
from nse_data import get_index_candles, get_options_chain, get_index_quote

logger = logging.getLogger(__name__)


def analyze_index(symbol: str) -> dict:
    """
    Master analysis function. Combines:
      1. Live index quote
      2. Options chain (PCR, Max Pain, OI buildup)
      3. Technical indicators (RSI, MACD, EMA, Supertrend)
      4. Support & Resistance levels
    Returns a single structured dict for the Ollama advisor.
    """

    # ── 1. Live Quote ──────────────────────────────────────
    quote = get_index_quote(symbol)
    ltp = quote.get("ltp", 0)

    # ── 2. Options Chain Data ──────────────────────────────
    chain_data = get_options_chain(symbol)
    pcr = chain_data.get("pcr", 1.0)
    max_pain = chain_data.get("max_pain", ltp)
    atm_strike = chain_data.get("atm_strike", round(ltp, -2))
    chain = chain_data.get("chain", [])
    total_call_oi = chain_data.get("total_call_oi", 0)
    total_put_oi = chain_data.get("total_put_oi", 0)
    nearest_expiry = chain_data.get("nearest_expiry", "N/A")

    # ── 3. PCR Interpretation ──────────────────────────────
    pcr_signal = _interpret_pcr(pcr)

    # ── 4. OI Buildup Analysis ─────────────────────────────
    oi_analysis = _analyze_oi_buildup(chain, atm_strike, symbol)

    # ── 5. Technical Indicators ────────────────────────────
    candles = get_index_candles(symbol)
    ta_signals = _compute_ta(candles)

    # ── 6. Support & Resistance ────────────────────────────
    sr_levels = _compute_support_resistance(candles, ltp)

    # ── 7. ATM Premium & Budget Analysis ──────────────────
    atm_info = _get_atm_premium(chain, atm_strike, symbol)

    # ── 8. Composite Bias ─────────────────────────────────
    bias_score, bias_factors = _compute_composite_bias(
        pcr_signal, oi_analysis, ta_signals, quote
    )

    return {
        "symbol": symbol,
        "display_name": config.INDICES[symbol]["display_name"],
        "ltp": ltp,
        "change": quote.get("change", 0),
        "change_pct": quote.get("change_pct", 0),
        "data_source": chain_data.get("data_source", "unknown"),
        "timestamp": chain_data.get("timestamp", ""),
        "nearest_expiry": nearest_expiry,
        # Options data
        "pcr": pcr,
        "pcr_signal": pcr_signal,
        "max_pain": max_pain,
        "atm_strike": atm_strike,
        "total_call_oi": total_call_oi,
        "total_put_oi": total_put_oi,
        "oi_analysis": oi_analysis,
        # Technicals
        "ta": ta_signals,
        # Support & Resistance
        "support": sr_levels["support"],
        "resistance": sr_levels["resistance"],
        "pivot": sr_levels["pivot"],
        # ATM option premiums
        "atm_call_ltp": atm_info.get("call_ltp", 0),
        "atm_put_ltp": atm_info.get("put_ltp", 0),
        "atm_call_iv": atm_info.get("call_iv", 0),
        "atm_put_iv": atm_info.get("put_iv", 0),
        # Budget info
        "budget_advice": _budget_advice(atm_info, symbol),
        # Composite signal
        "bias_score": bias_score,      # -10 (strong bearish) to +10 (strong bullish)
        "bias_factors": bias_factors,
        "preliminary_bias": (
            "BULLISH" if bias_score >= 2 else
            "BEARISH" if bias_score <= -2 else
            "NEUTRAL"
        ),
        # Top OI strikes for dashboard
        "chain_summary": _chain_summary(chain, atm_strike),
    }


# ─── PCR Interpretation ───────────────────────────────────────────────────────

def _interpret_pcr(pcr: float) -> str:
    t = config.PCR_THRESHOLDS
    if pcr >= t["STRONG_BULLISH"]:
        return "STRONG_BULLISH"
    elif pcr >= t["BULLISH"]:
        return "BULLISH"
    elif pcr >= t["NEUTRAL_HIGH"]:
        return "NEUTRAL_BULLISH"
    elif pcr >= t["NEUTRAL_LOW"]:
        return "NEUTRAL"
    elif pcr >= t["BEARISH"]:
        return "NEUTRAL_BEARISH"
    elif pcr >= t["STRONG_BEARISH"]:
        return "BEARISH"
    else:
        return "STRONG_BEARISH"


# ─── OI Buildup Analysis ──────────────────────────────────────────────────────

def _analyze_oi_buildup(chain: list, atm_strike: int, symbol: str) -> dict:
    """
    Identifies max OI strikes on call and put side (resistance / support walls),
    and detects OI buildup / unwinding patterns.
    """
    if not chain:
        return {"call_resistance": atm_strike, "put_support": atm_strike,
                "call_wall": "N/A", "put_wall": "N/A", "oi_bias": "NEUTRAL",
                "call_buildup": False, "put_buildup": False}

    step = 50 if symbol == "NIFTY" else 100

    above_atm = [r for r in chain if r["strike"] >= atm_strike]
    below_atm = [r for r in chain if r["strike"] <= atm_strike]

    # Max call OI above ATM = resistance
    call_resistance_row = max(above_atm, key=lambda r: r["call_oi"]) if above_atm else chain[-1]
    # Max put OI below ATM = support
    put_support_row = max(below_atm, key=lambda r: r["put_oi"]) if below_atm else chain[0]

    call_resistance = call_resistance_row["strike"]
    put_support = put_support_row["strike"]

    # OI buildup: recent addition of OI in the direction
    call_additions = sum(r["call_chg_oi"] for r in above_atm if r["call_chg_oi"] > 0)
    put_additions = sum(r["put_chg_oi"] for r in below_atm if r["put_chg_oi"] > 0)

    # Determine OI bias
    if put_additions > call_additions * 1.3:
        oi_bias = "BEARISH"  # More puts being added — bears active
    elif call_additions > put_additions * 1.3:
        oi_bias = "BULLISH"  # More calls being added — bulls active
    else:
        oi_bias = "NEUTRAL"

    # Range from support to resistance
    oi_range = f"{put_support} - {call_resistance}"

    return {
        "call_resistance": call_resistance,
        "put_support": put_support,
        "call_wall": f"{call_resistance} CE (OI: {call_resistance_row['call_oi']:,})",
        "put_wall": f"{put_support} PE (OI: {put_support_row['put_oi']:,})",
        "call_oi_addition": int(call_additions),
        "put_oi_addition": int(put_additions),
        "oi_bias": oi_bias,
        "oi_range": oi_range,
        "call_buildup": call_additions > 5000,
        "put_buildup": put_additions > 5000,
    }


# ─── Technical Indicators ─────────────────────────────────────────────────────

def _compute_ta(df: pd.DataFrame) -> dict:
    """Computes RSI, MACD, EMA, and Supertrend on candle data."""
    if df.empty or len(df) < 20:
        return _default_ta()

    closes = df["Close"].astype(float)
    highs = df["High"].astype(float)
    lows = df["Low"].astype(float)

    try:
        # RSI
        rsi_val = _rsi(closes, config.TA_PARAMS["RSI_PERIOD"])

        # EMA
        ema_fast = closes.ewm(span=config.TA_PARAMS["EMA_FAST"], adjust=False).mean().iloc[-1]
        ema_slow = closes.ewm(span=config.TA_PARAMS["EMA_SLOW"], adjust=False).mean().iloc[-1]

        # MACD
        ema_12 = closes.ewm(span=config.TA_PARAMS["MACD_FAST"], adjust=False).mean()
        ema_26 = closes.ewm(span=config.TA_PARAMS["MACD_SLOW"], adjust=False).mean()
        macd_line = ema_12 - ema_26
        signal_line = macd_line.ewm(span=config.TA_PARAMS["MACD_SIGNAL"], adjust=False).mean()
        macd_val = float(macd_line.iloc[-1])
        signal_val = float(signal_line.iloc[-1])
        histogram = macd_val - signal_val

        # Supertrend (simplified)
        st_signal, st_trend = _supertrend(highs, lows, closes)

        # Candle structure
        last_close = float(closes.iloc[-1])
        prev_close = float(closes.iloc[-2]) if len(closes) > 1 else last_close
        candle_bias = "GREEN" if last_close > prev_close else "RED"

        # EMA crossover
        ema_crossover = "GOLDEN" if ema_fast > ema_slow else "DEATH"

        return {
            "rsi": round(rsi_val, 2),
            "rsi_signal": (
                "OVERBOUGHT" if rsi_val >= 70 else
                "OVERSOLD" if rsi_val <= 30 else
                "BULLISH_MOMENTUM" if rsi_val >= 55 else
                "BEARISH_MOMENTUM" if rsi_val <= 45 else
                "NEUTRAL"
            ),
            "ema_fast": round(ema_fast, 2),
            "ema_slow": round(ema_slow, 2),
            "ema_crossover": ema_crossover,
            "macd": round(macd_val, 4),
            "macd_signal": round(signal_val, 4),
            "macd_histogram": round(histogram, 4),
            "macd_bias": "BULLISH" if macd_val > signal_val else "BEARISH",
            "supertrend_signal": st_signal,
            "supertrend_trend": st_trend,
            "candle_bias": candle_bias,
            "last_close": round(last_close, 2),
        }
    except Exception as e:
        logger.warning(f"TA computation failed: {e}")
        return _default_ta()


def _rsi(closes: pd.Series, period: int = 14) -> float:
    delta = closes.diff().dropna()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    return float(rsi.iloc[-1]) if not rsi.empty else 50.0


def _supertrend(highs: pd.Series, lows: pd.Series, closes: pd.Series,
                period: int = 10, multiplier: float = 3.0) -> tuple:
    """Simplified Supertrend calculation. Returns (signal, trend_direction)."""
    try:
        # ATR
        tr = pd.concat([
            highs - lows,
            (highs - closes.shift()).abs(),
            (lows - closes.shift()).abs()
        ], axis=1).max(axis=1)
        atr = tr.rolling(period).mean()

        hl2 = (highs + lows) / 2
        upper_band = hl2 + (multiplier * atr)
        lower_band = hl2 - (multiplier * atr)

        final_upper = upper_band.copy()
        final_lower = lower_band.copy()

        for i in range(1, len(closes)):
            final_upper.iloc[i] = (
                min(upper_band.iloc[i], final_upper.iloc[i - 1])
                if closes.iloc[i - 1] <= final_upper.iloc[i - 1]
                else upper_band.iloc[i]
            )
            final_lower.iloc[i] = (
                max(lower_band.iloc[i], final_lower.iloc[i - 1])
                if closes.iloc[i - 1] >= final_lower.iloc[i - 1]
                else lower_band.iloc[i]
            )

        last = closes.iloc[-1]
        if last > final_upper.iloc[-1]:
            return "BUY", "UPTREND"
        elif last < final_lower.iloc[-1]:
            return "SELL", "DOWNTREND"
        else:
            return "NEUTRAL", "SIDEWAYS"
    except Exception:
        return "NEUTRAL", "SIDEWAYS"


def _default_ta() -> dict:
    return {
        "rsi": 50.0, "rsi_signal": "NEUTRAL",
        "ema_fast": 0, "ema_slow": 0, "ema_crossover": "NEUTRAL",
        "macd": 0, "macd_signal": 0, "macd_histogram": 0, "macd_bias": "NEUTRAL",
        "supertrend_signal": "NEUTRAL", "supertrend_trend": "SIDEWAYS",
        "candle_bias": "NEUTRAL", "last_close": 0,
    }


# ─── Support & Resistance ─────────────────────────────────────────────────────

def _compute_support_resistance(df: pd.DataFrame, ltp: float) -> dict:
    """Pivot-based support/resistance using recent OHLC."""
    try:
        if df.empty or len(df) < 10:
            raise ValueError("Insufficient candles")
        high = float(df["High"].max())
        low = float(df["Low"].min())
        close = float(df["Close"].iloc[-1])
        pivot = (high + low + close) / 3
        r1 = (2 * pivot) - low
        s1 = (2 * pivot) - high
        r2 = pivot + (high - low)
        s2 = pivot - (high - low)
        return {
            "pivot": round(pivot, 2),
            "support": round(s1, 2),
            "support2": round(s2, 2),
            "resistance": round(r1, 2),
            "resistance2": round(r2, 2),
        }
    except Exception:
        return {
            "pivot": round(ltp, 2),
            "support": round(ltp * 0.995, 2),
            "support2": round(ltp * 0.99, 2),
            "resistance": round(ltp * 1.005, 2),
            "resistance2": round(ltp * 1.01, 2),
        }


# ─── ATM Premium ──────────────────────────────────────────────────────────────

def _get_atm_premium(chain: list, atm_strike: int, symbol: str) -> dict:
    """Finds the ATM strike's call and put premiums."""
    for row in chain:
        if row.get("is_atm") or row["strike"] == atm_strike:
            return {
                "strike": row["strike"],
                "call_ltp": row.get("call_ltp", 0),
                "put_ltp": row.get("put_ltp", 0),
                "call_iv": row.get("call_iv", 0),
                "put_iv": row.get("put_iv", 0),
            }
    return {"strike": atm_strike, "call_ltp": 0, "put_ltp": 0, "call_iv": 0, "put_iv": 0}


# ─── Budget Advice ────────────────────────────────────────────────────────────

def _budget_advice(atm_info: dict, symbol: str) -> dict:
    """
    Given ₹10,000 budget, calculates how many lots of ATM CE/PE can be bought
    and what the 30% target / 40% stop-loss levels are.
    """
    lot_size = config.INDICES[symbol]["lot_size"]
    budget = config.USER_BUDGET_INR
    risk_cap = budget * config.RISK_PER_TRADE_PCT  # ₹5000 max per trade

    call_ltp = atm_info.get("call_ltp", 0)
    put_ltp = atm_info.get("put_ltp", 0)

    def _calc(premium):
        if premium <= 0:
            return {}
        cost_per_lot = premium * lot_size
        lots = max(1, int(risk_cap / cost_per_lot))
        actual_cost = lots * cost_per_lot
        target_prem = round(premium * (1 + config.PROFIT_TARGET_PCT), 2)
        sl_prem = round(premium * (1 - config.STOP_LOSS_PCT), 2)

        # Gross profit/loss (without fees/slippage)
        gross_profit = round((target_prem - premium) * lot_size * lots, 2)
        gross_loss = round((premium - sl_prem) * lot_size * lots, 2)

        # Estimated slippage and fees (one-way slippage applied on entry and exit)
        entry_slippage = premium * config.SLIPPAGE_PCT * lot_size * lots
        exit_slippage_target = target_prem * config.SLIPPAGE_PCT * lot_size * lots
        exit_slippage_stop = sl_prem * config.SLIPPAGE_PCT * lot_size * lots

        entry_fee = premium * lot_size * lots * config.TRADING_FEE_PCT
        exit_fee_target = target_prem * lot_size * lots * config.TRADING_FEE_PCT
        exit_fee_stop = sl_prem * lot_size * lots * config.TRADING_FEE_PCT

        est_fees_on_target = round(entry_fee + exit_fee_target, 2)
        est_fees_on_stop = round(entry_fee + exit_fee_stop, 2)

        est_slippage_on_target = round(entry_slippage + exit_slippage_target, 2)
        est_slippage_on_stop = round(entry_slippage + exit_slippage_stop, 2)

        net_profit = round(gross_profit - est_fees_on_target - est_slippage_on_target, 2)
        net_loss = round(gross_loss + est_fees_on_stop + est_slippage_on_stop, 2)

        return {
            "premium": premium,
            "lots": lots,
            "cost_inr": round(actual_cost, 2),
            "target_premium": target_prem,
            "sl_premium": sl_prem,
            "max_profit_inr": gross_profit,
            "max_loss_inr": gross_loss,
            "estimated_fees_inr": {
                "on_target": est_fees_on_target,
                "on_stop": est_fees_on_stop,
            },
            "estimated_slippage_inr": {
                "on_target": est_slippage_on_target,
                "on_stop": est_slippage_on_stop,
            },
            "net_max_profit_inr": net_profit,
            "net_max_loss_inr": net_loss,
        }

    return {
        "call": _calc(call_ltp),
        "put": _calc(put_ltp),
        "lot_size": lot_size,
        "budget": budget,
    }


# ─── Composite Bias Score ─────────────────────────────────────────────────────

def _compute_composite_bias(pcr_signal, oi_analysis, ta, quote) -> tuple:
    """
    Returns a score from -10 (very bearish) to +10 (very bullish)
    by aggregating signals from PCR, OI, technicals, and price action.
    """
    score = 0
    factors = []

    # PCR contribution
    pcr_map = {
        "STRONG_BULLISH": (+3, "PCR strongly bullish (>1.4)"),
        "BULLISH": (+2, "PCR bullish (1.1-1.4)"),
        "NEUTRAL_BULLISH": (+1, "PCR slightly bullish"),
        "NEUTRAL": (0, "PCR neutral"),
        "NEUTRAL_BEARISH": (-1, "PCR slightly bearish"),
        "BEARISH": (-2, "PCR bearish (<0.9)"),
        "STRONG_BEARISH": (-3, "PCR very bearish (<0.7)"),
    }
    pcr_pts, pcr_desc = pcr_map.get(pcr_signal, (0, "PCR neutral"))
    score += pcr_pts
    factors.append({"factor": "PCR", "signal": pcr_signal, "points": pcr_pts, "desc": pcr_desc})

    # OI contribution
    oi_bias = oi_analysis.get("oi_bias", "NEUTRAL")
    oi_map = {"BULLISH": (+2, "OI buildup bullish"), "BEARISH": (-2, "OI buildup bearish"), "NEUTRAL": (0, "OI neutral")}
    oi_pts, oi_desc = oi_map.get(oi_bias, (0, "OI neutral"))
    score += oi_pts
    factors.append({"factor": "OI_BUILDUP", "signal": oi_bias, "points": oi_pts, "desc": oi_desc})

    # RSI contribution
    rsi_sig = ta.get("rsi_signal", "NEUTRAL")
    rsi_map = {
        "OVERSOLD": (+2, "RSI oversold — bounce likely"),
        "BULLISH_MOMENTUM": (+1, "RSI showing bullish momentum"),
        "NEUTRAL": (0, "RSI neutral"),
        "BEARISH_MOMENTUM": (-1, "RSI showing bearish momentum"),
        "OVERBOUGHT": (-2, "RSI overbought — correction risk"),
    }
    rsi_pts, rsi_desc = rsi_map.get(rsi_sig, (0, "RSI neutral"))
    score += rsi_pts
    factors.append({"factor": "RSI", "signal": rsi_sig, "points": rsi_pts, "desc": rsi_desc})

    # MACD contribution
    macd_bias = ta.get("macd_bias", "NEUTRAL")
    macd_pts = +1 if macd_bias == "BULLISH" else -1 if macd_bias == "BEARISH" else 0
    factors.append({"factor": "MACD", "signal": macd_bias, "points": macd_pts, "desc": f"MACD {macd_bias.lower()}"})
    score += macd_pts

    # Supertrend contribution
    st = ta.get("supertrend_signal", "NEUTRAL")
    st_pts = +1 if st == "BUY" else -1 if st == "SELL" else 0
    factors.append({"factor": "SUPERTREND", "signal": st, "points": st_pts, "desc": f"Supertrend {ta.get('supertrend_trend','')}"})
    score += st_pts

    # Price momentum
    change_pct = float(quote.get("change_pct", 0))
    if change_pct > 0.5:
        score += 1
        factors.append({"factor": "PRICE_MOMENTUM", "signal": "POSITIVE", "points": 1, "desc": f"Up {change_pct:.2f}% today"})
    elif change_pct < -0.5:
        score -= 1
        factors.append({"factor": "PRICE_MOMENTUM", "signal": "NEGATIVE", "points": -1, "desc": f"Down {abs(change_pct):.2f}% today"})
    else:
        factors.append({"factor": "PRICE_MOMENTUM", "signal": "FLAT", "points": 0, "desc": "Flat day"})

    return round(score, 1), factors


# ─── Chain Summary for UI ─────────────────────────────────────────────────────

def _chain_summary(chain: list, atm_strike: int) -> list:
    """Returns top ±8 strikes around ATM formatted for the dashboard table."""
    rows = []
    max_call_oi = max((r["call_oi"] for r in chain), default=1) or 1
    max_put_oi = max((r["put_oi"] for r in chain), default=1) or 1

    for r in chain:
        rows.append({
            "strike": r["strike"],
            "is_atm": r.get("is_atm", False),
            "call_oi": r["call_oi"],
            "call_oi_pct": round(r["call_oi"] / max_call_oi * 100),
            "call_chg_oi": r["call_chg_oi"],
            "call_ltp": r["call_ltp"],
            "call_iv": r["call_iv"],
            "put_ltp": r["put_ltp"],
            "put_iv": r["put_iv"],
            "put_oi": r["put_oi"],
            "put_oi_pct": round(r["put_oi"] / max_put_oi * 100),
            "put_chg_oi": r["put_chg_oi"],
        })
    return rows
