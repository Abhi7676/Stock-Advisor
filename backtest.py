"""
backtest.py — Simple simulation to estimate hit-rate of signals

This script uses the project's synthetic data paths to generate signals
and simulates the next 30 minutes (5-min bars) using real/synthetic
candles to see whether the recommended entry hits the +30% target or
the -40% stop-loss first.

Run: python backtest.py
"""
import statistics
import traceback
from collections import defaultdict

import config
import nse_data
import analysis_engine
import gemini_advisor
import os
import json


def simulate_for_symbol(symbol: str, max_trades: int = 200):
    df = nse_data.get_index_candles(symbol, interval="5m", period="1d")
    if df is None or df.empty:
        print(f"No candles for {symbol}")
        return {}

    total = 0
    wins = 0
    losses = 0
    no_result = 0
    pnls = []
    durations = []

    # T0 in years (approx used by analyzer/synthetic chain)
    T0 = (5.75 / 365.0) if symbol == "NIFTY" else (27.0 / 365.0)
    lot = config.INDICES[symbol]["lot_size"]

    # Check for saved NSE snapshots (collected during market hours)
    snap_dir = os.path.join(os.path.dirname(__file__), "nse_snapshots")
    latest_snapshot = os.path.join(snap_dir, f"{symbol}_latest.json")
    snapshot_data = None
    if os.path.exists(latest_snapshot):
        try:
            with open(latest_snapshot, 'r', encoding='utf-8') as f:
                snapshot_data = json.load(f)
            print(f"Using saved NSE snapshot for {symbol}: {latest_snapshot}")
        except Exception as e:
            print("Failed to load snapshot:", e)

    # We'll iterate over a subset to limit runtime
    indices = range(0, min(len(df) - 6, max_trades))

    for i in indices:
        try:
            start_price = float(df['Close'].iloc[i])

            # Monkeypatch get_index_quote to use this start_price for deterministic synthetic chain
            orig_quote = nse_data.get_index_quote
            nse_data.get_index_quote = lambda s, sp=start_price: {"symbol": s, "ltp": sp, "change": 0, "change_pct": 0, "source": "synthetic_test"}

            # If we have a saved snapshot, monkeypatch get_options_chain to return it
            orig_chain = nse_data.get_options_chain
            if snapshot_data:
                nse_data.get_options_chain = lambda s, data=snapshot_data: data

            analysis = analysis_engine.analyze_index(symbol)
            # Use rule-based signal for determinism
            signal = gemini_advisor._rule_based_signal(analysis)

            # restore
            nse_data.get_index_quote = orig_quote
            if snapshot_data:
                nse_data.get_options_chain = orig_chain

            sig_name = signal.get('signal')
            if sig_name not in ("BUY_CALL", "BUY_PUT"):
                continue

            total += 1
            entry_p = float(signal.get('entry_premium') or 0.0)
            tgt = float(signal.get('target_premium') or 0.0)
            sl = float(signal.get('stop_loss_premium') or 0.0)
            strike = int(signal.get('strike') or analysis.get('atm_strike') or 0)
            otype = signal.get('option_type') or ('CE' if sig_name == 'BUY_CALL' else 'PE')

            # Simulate next 6 bars = 30 minutes using subsequent closes
            hit = None
            elapsed_min = 0
            for j in range(1, 7):
                nxt_price = float(df['Close'].iloc[i + j])
                elapsed_min += 5
                # reduce T accordingly
                T = max(0.001, T0 - (elapsed_min / (60 * 24 * 365)))
                # For IV, use analysis atm IVs as fraction (analysis stores IV in pct sometimes)
                iv = (analysis.get('atm_call_iv') or analysis.get('atm_put_iv') or 12.8) / 100.0
                if otype == 'PE':
                    iv = (analysis.get('atm_put_iv') or analysis.get('atm_call_iv') or 10.6) / 100.0

                # Compute option price via Black-Scholes
                curr_p = nse_data.black_scholes(nxt_price, strike, T, 0.07, iv, option_type=otype)

                if curr_p >= tgt and tgt > 0:
                    hit = ('target', curr_p, elapsed_min)
                    break
                if curr_p <= sl and sl > 0:
                    hit = ('stop', curr_p, elapsed_min)
                    break

            if hit is None:
                no_result += 1
                # mark final pnl using last price
                final_p = curr_p if 'curr_p' in locals() else entry_p
                pnl_pct = ((final_p - entry_p) / entry_p) * 100 if entry_p else 0
                pnls.append(pnl_pct)
                durations.append(elapsed_min)
            else:
                kind, price_hit, mins = hit
                pnl_pct = ((price_hit - entry_p) / entry_p) * 100 if entry_p else 0
                pnls.append(pnl_pct)
                durations.append(mins)
                if kind == 'target':
                    wins += 1
                else:
                    losses += 1

        except Exception:
            traceback.print_exc()
            continue

    return {
        'symbol': symbol,
        'trades_evaluated': total,
        'wins': wins,
        'losses': losses,
        'no_result': no_result,
        'win_rate_pct': round((wins / total * 100) if total else 0, 2),
        'avg_pnl_pct': round(statistics.mean(pnls), 2) if pnls else 0.0,
        'median_pnl_pct': round(statistics.median(pnls), 2) if pnls else 0.0,
        'avg_duration_min': round(statistics.mean(durations), 1) if durations else 0,
    }


if __name__ == '__main__':
    all_res = []
    for sym in ['NIFTY', 'BANKNIFTY']:
        print(f"Running simulation for {sym}...")
        res = simulate_for_symbol(sym, max_trades=200)
        all_res.append(res)
        print(res)

    print('\nSummary:')
    for r in all_res:
        print(f"{r.get('symbol')} → Evaluated {r.get('trades_evaluated')} trades: Wins {r.get('wins')}, Losses {r.get('losses')}, NoResult {r.get('no_result')}, WinRate {r.get('win_rate_pct')}% AvgPnL {r.get('avg_pnl_pct')}%")
