import os
import json
import logging
from datetime import datetime, time
import pytz
import requests
import yfinance as yf
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

BOT_TOKEN = os.environ["BOT_TOKEN"]
ALPHA_KEY = os.environ.get("ALPHA_KEY", "")
SUBSCRIBERS_FILE = "subscribers.json"
SEND_HOUR_NY = 9
NY_TZ = pytz.timezone("America/New_York")

GLD_TO_GOLD = 10.5
FALLBACK_GOLD_PRICE = 2650.0

logging.basicConfig(level=logging.INFO)


def load_subs():
    if os.path.exists(SUBSCRIBERS_FILE):
        with open(SUBSCRIBERS_FILE) as f:
            return set(json.load(f))
    return set()


def save_subs(subs):
    with open(SUBSCRIBERS_FILE, "w") as f:
        json.dump(list(subs), f)


def _alpha_quote(symbol):
    url = "https://www.alphavantage.co/query"
    params = {"function": "GLOBAL_QUOTE", "symbol": symbol, "apikey": ALPHA_KEY}
    r = requests.get(url, params=params, timeout=15)
    try:
        data = r.json()
    except Exception:
        raise Exception(f"Alpha not JSON: status={r.status_code} body={r.text[:80]}")
    price_str = data.get("Global Quote", {}).get("05. price")
    if not price_str:
        raise Exception(f"Alpha no price: {str(data)[:120]}")
    return float(price_str)


def _yahoo_price(symbol):
    t = yf.Ticker(symbol)
    hist = t.history(period="5d")
    if hist.empty:
        raise Exception(f"yfinance no data for {symbol}")
    return float(hist["Close"].iloc[-1])


def get_gold_price():
    errors = []
    try:
        return _alpha_quote("XAU") * 1.0, "Alpha:XAU"
    except Exception as e:
        errors.append(f"Alpha XAU: {e}")
    try:
        return _alpha_quote("GLD") * GLD_TO_GOLD, "Alpha:GLD"
    except Exception as e:
        errors.append(f"Alpha GLD: {e}")
    try:
        return _yahoo_price("GC=F"), "Yahoo:GC"
    except Exception as e:
        errors.append(f"Yahoo GC: {e}")
    try:
        return _yahoo_price("GLD") * GLD_TO_GOLD, "Yahoo:GLD"
    except Exception as e:
        errors.append(f"Yahoo GLD: {e}")
    logging.warning(f"All failed: {errors}")
    return FALLBACK_GOLD_PRICE, "fallback"


def get_gld_price():
    try:
        return _alpha_quote("GLD")
    except Exception:
        pass
    try:
        return _yahoo_price("GLD")
    except Exception:
        pass
    spot, _ = get_gold_price()
    return spot / GLD_TO_GOLD


def get_gold_levels():
    spot, source = get_gold_price()
    logging.info(f"Gold source={source} price={spot}")

    gld_price = get_gld_price()

    gld = yf.Ticker("GLD")
    expiry = gld.options[0]
    chain = gld.option_chain(expiry)

    calls = chain.calls.dropna(subset=["openInterest"])
    puts = chain.puts.dropna(subset=["openInterest"])

    if calls.empty or puts.empty:
        raise Exception("Empty options chain")

    ratio = spot / gld_price if gld_price else GLD_TO_GOLD

    cw = calls.loc[calls.openInterest.idxmax()]
    pw = puts.loc[puts.openInterest.idxmax()]

    strikes = sorted(set(calls.strike) & set(puts.strike))
    pains = []
    for K in strikes:
        cl = ((K - calls.strike).clip(lower=0) * calls.openInterest).sum()
        pl = ((puts.strike - K).clip(lower=0) * puts.openInterest).sum()
        pains.append(cl + pl)
    max_pain = strikes[pains.index(min(pains))]

    pc_ratio = puts.openInterest.sum() / calls.openInterest.sum()

    top_calls = calls.nlargest(3, "openInterest")[["strike", "openInterest"]]
    top_puts = puts.nlargest(3, "openInterest")[["strike", "openInterest"]]

    return {
        "spot": round(spot, 2),
        "source": source,
        "gld": round(gld_price, 2),
        "expiry": expiry,
        "call_wall": round(cw.strike * ratio),
        "put_wall": round(pw.strike * ratio),
        "max_pain": round(max_pain * ratio),
        "pc_ratio": round(pc_ratio, 2),
        "top_calls": [(round(r.strike * ratio), int(r.openInterest))
                      for _, r in top_calls.iterrows()],
        "top_puts": [(round(r.strike * ratio), int(r.openInterest))
                     for _, r in top_puts.iterrows()],
    }


def format_message(lv):
    def bias(pcr):
        if pcr > 1.2:
            return "🔴 bearish"
        if pcr < 0.7:
            return "🟢 bullish"
        return "⚪ neutral"

    lines = [
        f"🥇 *GOLD — levels for {datetime.now():%d.%m.%Y}*",
        "",
        f"💰 Spot: *${lv['spot']}*  _({lv['source']})_",
        f"📊 GLD: ${lv['gld']}",
        f"📅 Expiry: {lv['expiry']}",
        "",
        f"🟢 Call Wall: *${lv['call_wall']}*",
        f"🔴 Put Wall:     *${lv['put_wall']}*",
        f"⚖️  Max Pain: *${lv['max_pain']}*",
        f"📊 P/C ratio: *{lv['pc_ratio']}* — {bias(lv['pc_ratio'])}",
        "",
        "*Top-3 resistance (Call OI):*",
    ]
    for s, oi in lv["top_calls"]:
        lines.append(f"  • ${s}  (OI {oi:,})")
    lines.append("")
    lines.append("*Top-3 support (Put OI):*")
    for s, oi in lv["top_puts"]:
        lines.append(f"  • ${s}  (OI {oi:,})")
    lines.append("")
    lines.append(f"_Updated: {datetime.now():%H:%M}_")
    return "\n".join(lines)


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Hello! I show gold options levels.\n\n"
        "/levels — today levels\n"
        "/subscribe — daily at 16:00 MSK\n"
        "/unsubscribe — stop"
    )


async def cmd_levels(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("Calculating...")
    try:
        lv = get_gold_levels()
        await msg.edit_text(format_message(lv), parse_mode="Markdown")
    except Exception as e:
        await msg.edit_text(f"Error: {e}")


async def cmd_subscribe(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    subs = load_subs()
    subs.add(update.effective_chat.id)
    save_subs(subs)
    await update.message.reply_text("Subscribed!")


async def cmd_unsubscribe(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    subs = load_subs()
    subs.discard(update.effective_chat.id)
    save_subs(subs)
    await update.message.reply_text("Unsubscribed.")


async def daily_broadcast(ctx: ContextTypes.DEFAULT_TYPE):
    try:
        lv = get_gold_levels()
        text = format_message(lv)
    except Exception as e:
        text = f"Error: {e}"
    for chat_id in load_subs():
        try:
            await ctx.bot.send_message(chat_id, text, parse_mode="Markdown")
        except Exception as e:
            logging.warning(f"Failed {chat_id}: {e}")


def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("levels", cmd_levels))
    app.add_handler(CommandHandler("subscribe", cmd_subscribe))
    app.add_handler(CommandHandler("unsubscribe", cmd_unsubscribe))
    app.job_queue.run_daily(
        daily_broadcast,
        time=time(hour=SEND_HOUR_NY, minute=0, tzinfo=NY_TZ),
        name="daily_gold_levels",
    )
    print("Bot started")
    app.run_polling()


if __name__ == "__main__":
    main()
