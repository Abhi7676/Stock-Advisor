"""
Stock Market Data Engineering Pipeline
Handles Extraction (yfinance / synthetic generator fallback),
Transformation (Technical Indicators calculation), and Loading (DuckDB).
"""

import math
import os
import random
import sys
from datetime import datetime, timedelta
import numpy as np
import pandas as pd

try:
    import duckdb
except ImportError:
    duckdb = None

try:
    import yfinance as yf
except ImportError:
    yf = None

import config


class StockDataPipeline:

    def __init__(self, db_path=config.DB_FILE):
        self.db_path = db_path
        self.tickers = config.DEFAULT_TICKERS

    def get_db_connection(self):
        if duckdb:
            return duckdb.connect(self.db_path)
        return None

    def fetch_stock_data(
        self, symbol: str, period: str = "1y", interval: str = "1d"
    ) -> pd.DataFrame:
        """Fetch stock data from yfinance or generate realistic time-series fallback."""
        df = None
        # Try yfinance with fast timeout fallback
        if yf:
            try:
                # Use fast yfinance fetch
                ticker = yf.Ticker(symbol)
                df = ticker.history(period=period, interval=interval, timeout=3)
                if df is not None and not df.empty:
                    df = df.reset_index()
                    date_col = "Date" if "Date" in df.columns else df.columns[0]
                    df = df.rename(
                        columns={
                            date_col: "Date",
                            "Open": "Open",
                            "High": "High",
                            "Low": "Low",
                            "Close": "Close",
                            "Volume": "Volume",
                        }
                    )
                    df["Date"] = pd.to_datetime(df["Date"]).dt.strftime("%Y-%m-%d")
                    df["Symbol"] = symbol
                    return df[["Symbol", "Date", "Open", "High", "Low", "Close", "Volume"]]
            except Exception:
                pass

        # Fallback: Instant synthetic market data generator
        return self._generate_synthetic_data(symbol, days=365)

    def _generate_synthetic_data(self, symbol: str, days: int = 365) -> pd.DataFrame:
        """Generates realistic market time series with volatility, intraday high/low, and realistic volume."""
        np.random.seed(hash(symbol) % 2**32)
        end_date = datetime.now()
        dates = [
            (end_date - timedelta(days=i)).strftime("%Y-%m-%d")
            for i in range(days, -1, -1)
        ]

        # Base prices per ticker
        base_prices = {
            "AAPL": 225.0,
            "NVDA": 125.0,
            "MSFT": 440.0,
            "GOOGL": 175.0,
            "AMZN": 185.0,
            "TSLA": 210.0,
            "JPM": 215.0,
            "JNJ": 160.0,
        }
        start_price = base_prices.get(symbol, 150.0)

        # Geometric Brownian Motion parameters
        mu = 0.0005  # daily drift
        sigma = 0.018  # daily volatility

        returns = np.random.normal(mu, sigma, len(dates))
        price_paths = start_price * np.exp(np.cumsum(returns))

        records = []
        for i, dt in enumerate(dates):
            close_p = float(price_paths[i])
            daily_vol = random.uniform(0.008, 0.025)
            open_p = close_p * (1 + random.uniform(-daily_vol / 2, daily_vol / 2))
            high_p = max(open_p, close_p) * (1 + random.uniform(0.001, daily_vol))
            low_p = min(open_p, close_p) * (1 - random.uniform(0.001, daily_vol))
            volume = int(random.uniform(15_000_000, 85_000_000))

            records.append(
                {
                    "Symbol": symbol,
                    "Date": dt,
                    "Open": round(open_p, 2),
                    "High": round(high_p, 2),
                    "Low": round(low_p, 2),
                    "Close": round(close_p, 2),
                    "Volume": volume,
                }
            )

        return pd.DataFrame(records)

    def calculate_technical_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """Calculates SMA, EMA, RSI, MACD, Bollinger Bands, and Volatility."""
        df = df.sort_values("Date").copy()

        # Simple Moving Averages
        df["SMA_20"] = df["Close"].rolling(window=config.INDICATOR_PARAMS["SMA_SHORT"]).mean()
        df["SMA_50"] = df["Close"].rolling(window=config.INDICATOR_PARAMS["SMA_MEDIUM"]).mean()
        df["SMA_200"] = df["Close"].rolling(window=config.INDICATOR_PARAMS["SMA_LONG"]).mean()

        # Exponential Moving Averages
        df["EMA_12"] = df["Close"].ewm(span=config.INDICATOR_PARAMS["EMA_FAST"], adjust=False).mean()
        df["EMA_26"] = df["Close"].ewm(span=config.INDICATOR_PARAMS["EMA_SLOW"], adjust=False).mean()

        # MACD Line & Signal Line
        df["MACD"] = df["EMA_12"] - df["EMA_26"]
        df["MACD_Signal"] = df["MACD"].ewm(span=config.INDICATOR_PARAMS["MACD_SIGNAL"], adjust=False).mean()
        df["MACD_Hist"] = df["MACD"] - df["MACD_Signal"]

        # Relative Strength Index (RSI - 14)
        delta = df["Close"].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=config.INDICATOR_PARAMS["RSI_PERIOD"]).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=config.INDICATOR_PARAMS["RSI_PERIOD"]).mean()
        rs = gain / (loss.replace(0, np.nan))
        df["RSI_14"] = 100 - (100 / (1 + rs))
        df["RSI_14"] = df["RSI_14"].fillna(50)

        # Bollinger Bands
        sma20 = df["SMA_20"]
        std20 = df["Close"].rolling(window=config.INDICATOR_PARAMS["BBANDS_PERIOD"]).std()
        df["BB_Upper"] = sma20 + (std20 * config.INDICATOR_PARAMS["BBANDS_STD"])
        df["BB_Lower"] = sma20 - (std20 * config.INDICATOR_PARAMS["BBANDS_STD"])

        # Daily Returns & 20-Day Annualized Volatility
        df["Daily_Return"] = df["Close"].pct_change()
        df["Volatility_20D"] = df["Daily_Return"].rolling(window=20).std() * np.sqrt(252) * 100

        # Fill NaNs nicely
        df = df.bfill().ffill().fillna(0)

        # Round numerical values
        float_cols = [
            "Open", "High", "Low", "Close", "SMA_20", "SMA_50", "SMA_200",
            "EMA_12", "EMA_26", "MACD", "MACD_Signal", "MACD_Hist",
            "RSI_14", "BB_Upper", "BB_Lower", "Daily_Return", "Volatility_20D"
        ]
        for col in float_cols:
            if col in df.columns:
                df[col] = df[col].round(4)

        return df

    def run_pipeline(self):
        """Executes full ETL Pipeline across all default tickers and persists to DuckDB."""
        print("Starting Stock Market Data Pipeline...")
        all_raw = []
        all_processed = []
        summary_records = []

        for item in self.tickers:
            symbol = item["symbol"]
            name = item["name"]
            sector = item["sector"]

            raw_df = self.fetch_stock_data(symbol)
            processed_df = self.calculate_technical_indicators(raw_df)

            all_raw.append(raw_df)
            all_processed.append(processed_df)

            # Latest metrics for summary
            latest = processed_df.iloc[-1]
            prev = processed_df.iloc[-2] if len(processed_df) > 1 else latest

            change = latest["Close"] - prev["Close"]
            change_pct = (change / prev["Close"]) * 100 if prev["Close"] != 0 else 0

            rsi_signal = "Neutral"
            if latest["RSI_14"] >= 70:
                rsi_signal = "Overbought"
            elif latest["RSI_14"] <= 30:
                rsi_signal = "Oversold"

            macd_signal = "Bullish" if latest["MACD"] > latest["MACD_Signal"] else "Bearish"

            summary_records.append(
                {
                    "Symbol": symbol,
                    "Name": name,
                    "Sector": sector,
                    "Latest_Price": round(latest["Close"], 2),
                    "Change": round(change, 2),
                    "Change_Pct": round(change_pct, 2),
                    "High_52W": round(processed_df["High"].max(), 2),
                    "Low_52W": round(processed_df["Low"].min(), 2),
                    "Volume": int(latest["Volume"]),
                    "RSI": round(latest["RSI_14"], 2),
                    "RSI_Signal": rsi_signal,
                    "MACD_Signal": macd_signal,
                    "Volatility_Pct": round(latest["Volatility_20D"], 2),
                    "Last_Updated": latest["Date"],
                }
            )

        full_raw_df = pd.concat(all_raw, ignore_index=True)
        full_processed_df = pd.concat(all_processed, ignore_index=True)
        summary_df = pd.DataFrame(summary_records)

        # Load into DuckDB or SQLite database
        conn = self.get_db_connection()
        if conn:
            conn.execute("CREATE TABLE IF NOT EXISTS stocks_raw AS SELECT * FROM full_raw_df WHERE 1=0")
            conn.execute("DELETE FROM stocks_raw")
            conn.execute("INSERT INTO stocks_raw SELECT * FROM full_raw_df")

            conn.execute("CREATE TABLE IF NOT EXISTS stocks_processed AS SELECT * FROM full_processed_df WHERE 1=0")
            conn.execute("DELETE FROM stocks_processed")
            conn.execute("INSERT INTO stocks_processed SELECT * FROM full_processed_df")

            conn.execute("CREATE TABLE IF NOT EXISTS stock_summary AS SELECT * FROM summary_df WHERE 1=0")
            conn.execute("DELETE FROM stock_summary")
            conn.execute("INSERT INTO stock_summary SELECT * FROM summary_df")

            conn.close()
            print(f"Data Pipeline finished successfully. DuckDB database updated at {self.db_path}")
        else:
            # Native SQLite Fallback
            import sqlite3
            sqlite_path = os.path.join(config.BASE_DIR, "stock_market.sqlite")
            sq_conn = sqlite3.connect(sqlite_path)
            full_raw_df.to_sql("stocks_raw", sq_conn, if_exists="replace", index=False)
            full_processed_df.to_sql("stocks_processed", sq_conn, if_exists="replace", index=False)
            summary_df.to_sql("stock_summary", sq_conn, if_exists="replace", index=False)
            sq_conn.close()
            print(f"Data Pipeline finished successfully. SQLite database updated at {sqlite_path}")

            # CSV Backups
            full_processed_df.to_csv(os.path.join(config.BASE_DIR, "stocks_processed.csv"), index=False)
            summary_df.to_csv(os.path.join(config.BASE_DIR, "stock_summary.csv"), index=False)

        return full_processed_df, summary_df


if __name__ == "__main__":
    pipeline = StockDataPipeline()
    pipeline.run_pipeline()
