"""Fetch and save options chain snapshot for NIFTY/BANKNIFTY.

Run this during market hours to collect real NSE option-chain JSON.

Usage:
  python tools/fetch_chain_snapshot.py NIFTY
  python tools/fetch_chain_snapshot.py BANKNIFTY

Saves to `nse_snapshots/<symbol>_<YYYYmmdd_HHMMSS>.json`.
"""
import os
import sys
import json
from datetime import datetime
import nse_data


OUT_DIR = os.path.join(os.path.dirname(__file__), '..', 'nse_snapshots')
os.makedirs(OUT_DIR, exist_ok=True)


def fetch(symbol: str):
    print(f"Fetching options chain for {symbol}...")
    try:
        data = nse_data.get_options_chain(symbol)
        if not data:
            print("No data returned (NSE may be closed or blocked).")
            return False
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        fname = os.path.join(OUT_DIR, f"{symbol}_{ts}.json")
        with open(fname, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        # also write a 'latest' pointer
        latest = os.path.join(OUT_DIR, f"{symbol}_latest.json")
        with open(latest, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        print(f"Saved snapshot to {fname}")
        return True
    except Exception as e:
        print("Fetch failed:", e)
        return False


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("Provide symbol: NIFTY or BANKNIFTY")
        sys.exit(1)
    sym = sys.argv[1].upper()
    if sym not in ("NIFTY", "BANKNIFTY"):
        print("Symbol must be NIFTY or BANKNIFTY")
        sys.exit(1)
    ok = fetch(sym)
    if not ok:
        sys.exit(2)
