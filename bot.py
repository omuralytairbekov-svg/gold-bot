import os
import json
import logging
from datetime import datetime, time
import pytz
import requests
import pandas as pd
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

BOT_TOKEN = os.environ["BOT_TOKEN"]
SUBSCRIBERS_FILE = "subscribers.json"
SEND_HOUR_NY = 9
NY_TZ = pytz.timezone("America/New_York")

logging.basicConfig(level=logging.INFO)

def load_subs():
    if os.path.exists(SUBSCRIBERS_FILE):
        with open(SUBSCRIBERS_FILE) as f:
            return set(json.load(f))
    return set()

def save_subs(subs):
    with open(SUBSCRIBERS_FILE, "w") as f:
        json.dump(list(subs), f)

def get_gold_price():
    """Цена золота через Stooq (не блокирует серверы)."""
    try:
        # Stooq: XAUUSD — золото спот
        url = "https://stooq.com/q/l/?s=xauusd&f=sd2t2ohlcv&h&e=csv"
        r = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        df = pd.read_csv(pd.compat.StringIO(r.text)) if False else pd.read_csv(
            __import__("io").StringIO(r.text)
        )
        return float(df["Close"].iloc[0])
    except Exception as e:
        logging.error(f"Stooq error: {e}")
        # Фолбэк — Yahoo
        try:
            import yfinance as yf
            hist = yf.Ticker("GC=F").history(period="5d")
            return float(hist["Close"].iloc[-1])
        except Exception as e2:
            logging.error(f"Yahoo fallback error: {e2}")
            raise

def get_gld_price():
    """Цена GLD через Stooq."""
    try:
        url = "https://stooq.com/q/l/?s=gld.us&f=sd2t2ohlcv&h&e=csv"
        r = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        import io
        df = pd.read_csv(io.StringIO(r.text))
        return float(df["Close"].iloc[0])
    except Exception as e:
        logging.error(f"GLD Stooq error: {e}")
        return None

def get_gold_levels():
    # Цена золота
    spot = get_gold_price()

    # Пытаемся получить опционы через yfinance (GLD)
    import yfinance as yf
    gld = yf.Ticker("GLD")
    gld_price = get_gld_price() or gld.info.get("regularMarketPrice")

    if not gld_price:
        raise Exception("Cannot fetch GLD price")

    # Экспирации
    try:
        expiry = gld.options[0]
    except Exception as e:
        raise Exception(f"No GLD options: {e}")

    chain = gld.option_chain(expiry)
    calls = chain.calls.dropna(subset=["openInterest"])
    puts = chain.puts.dropna(subset=["openInterest"])

    if calls.empty or puts.empty:
        raise Exception("Empty options chain")

    ratio = spot / gld_price

    cw = calls.loc[calls.openInterest.idxmax()]
    pw = puts.loc[puts.openInterest.idxmax()]

    strikes = sorted(set(calls.strike) & set(puts.strike))
    if not strikes:
        raise Exception
