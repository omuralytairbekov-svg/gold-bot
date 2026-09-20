import os
import json
import io
import logging
from datetime import datetime, time
import pytz
import requests
import pandas as pd
import yfinance as yf
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

BOT_TOKEN = os.environ["BOT_TOKEN"]
SUBSCRIBERS_FILE = "subscribers.json"
SEND_HOUR_NY = 9
NY_TZ = pytz.timezone("America/New_York")

# Коэффициент пересчёта GLD → золото (доллары за унцию)
GLD_TO_GOLD = 10.5

logging.basicConfig(level=logging.INFO)


def load_subs():
    if os.path.exists(SUBSCRIBERS_FILE):
        with open(SUBSCRIBERS_FILE) as f:
            return set(json.load(f))
    return set()


def save_subs(subs):
    with open(SUBSCRIBERS_FILE, "w") as f:
        json.dump(list(subs), f)


def get_gold_levels():
    # 1. GLD — ETF на золото, через него получаем и цену, и опционы
    gld = yf.Ticker("GLD")

    # 2. Пытаемся взять свежую цену GLD через yfinance
    gld_price = None
    try:
        hist = gld.history(period="5d")
        if not hist.empty:
            gld_price = float(hist["Close"].iloc[-1])
    except Exception as e:
        logging.warning(f"yfinance history failed: {e}")

    # 3. Если yfinance не отдал — берём через Stooq
    if gld_price is None:
        url = "https://stooq.com/q/l/?s=gld.us&f=sd2t2ohlcv&h&e=csv"
        r = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        df = pd.read_csv(io.StringIO(r.text))
        gld_price = float(df["Close"].iloc[0])

    # 4. Цена золота за унцию (приближённо)
    spot = gld_price * GLD_TO_GOLD

    # 5. Опционная цепочка GLD
    expiry = gld.options[0]
    chain = gld.option_chain(expiry)

    calls = chain.calls.dropna(subset=["openInterest"])
    puts = chain.puts.dropna(subset=["openInterest"])

    if calls.empty or puts.empty:
        raise Exception("Empty options chain")

    ratio = GLD_TO_GOLD

    # 6. Call Wall и Put Wall
    cw = calls.loc[calls.openInterest.idxmax()]
    pw = puts.loc[puts.openInterest.idxmax()]

    # 7. Max Pain
    strikes = sorted(set(calls.strike) & set(puts.strike))
    pains = []
    for K in strikes:
        cl = ((K - calls.strike).clip(lower=0) * calls.openInterest).sum()
        pl = ((puts.strike - K).clip(lower=0) * puts.openInterest).sum()
        pains.append(cl + pl)
    max_pain = strikes[pains.index(min(pains))]

    # 8. P/C ratio
    pc_ratio = puts.openInterest.sum() / calls.openInterest.sum()

    # 9. Топ-3 страйка
    top_calls = calls.nlargest(3, "openInterest")[["strike", "openInterest"]]
    top_puts = puts.nlargest(3, "openInterest")[["strike", "openInterest"]]

    return {
        "spot": round(spot, 2),
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
        f"💰 Spot: *${lv['spot']}*",
        f"📅 Expiry: {lv['expiry']}",
        "",
        f"🟢 Call Wall (resistance): *${lv['call_wall']}*",
        f"🔴 Put Wall (support):     *${lv['put_wall']}*",
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
    await update.message.reply_text("Subscribed! Daily at 16:00 MSK.")


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
