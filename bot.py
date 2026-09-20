import os
import json
import logging
from datetime import datetime, time
import pytz
import yfinance as yf
import pandas as pd
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, ContextTypes
)

# ==== НАСТРОЙКИ ====
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

def get_gold_levels():
    gold = yf.Ticker("GC=F")
    spot = gold.fast_info["last_price"]

    gld = yf.Ticker("GLD")
    expiry = gld.options[0]
    chain = gld.option_chain(expiry)

    calls = chain.calls.dropna(subset=["openInterest"])
    puts = chain.puts.dropna(subset=["openInterest"])

    ratio = spot / gld.fast_info["last_price"]

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
            return "🔴 медвежий"
        if pcr < 0.7:
            return "🟢 бычий"
        return "⚪ нейтральный"

    lines = [
        f"🥇 *GOLD — уровни на {datetime.now():%d.%m.%Y}*",
        f"",
        f"💰 Spot: *${lv['spot']}*",
        f"📅 Экспирация: {lv['expiry']}",
        f"",
        f"🟢 Call Wall (сопротивление): *${lv['call_wall']}*",
        f"🔴 Put Wall (поддержка):      *${lv['put_wall']}*",
        f"⚖️  Max Pain: *${lv['max_pain']}*",
        f"📊 P/C ratio: *{lv['pc_ratio']}* — {bias(lv['pc_ratio'])}",
        f"",
        f"*Топ-3 сопротивления (Call OI):*",
    ]
    for s, oi in lv["top_calls"]:
        lines.append(f"  • ${s}  (OI {oi:,})")
    lines.append(f"\n*Топ-3 поддержки (Put OI):*")
    for s, oi in lv["top_puts"]:
        lines.append(f"  • ${s}  (OI {oi:,})")
    lines.append(f"\n_Обновлено: {datetime.now():%H:%M} МСК_")
    return "\n".join(lines)

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Привет! Я показываю опционные уровни по золоту.\n\n"
        "/levels — уровни на сегодня\n"
        "/subscribe — получать каждый день в 16:00 МСК\n"
        "/unsubscribe — отписаться"
    )

async def cmd_levels(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("⏳ Считаю уровни...")
    try:
        lv = get_gold_levels()
        await msg.edit_text(format_message(lv), parse_mode="Markdown")
    except Exception as e:
        await msg.edit_text(f"❌ Ошибка: {e}")

async def cmd_subscribe(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    subs = load_subs()
    subs.add(update.effective_chat.id)
    save_subs(subs)
    await update.message.reply_text("✅ Подписался! Уровни будут приходить каждый день в 16:00 МСК.")

async def cmd_unsubscribe(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    subs = load_subs()
    subs.discard(update.effective_chat.id)
    save_subs(subs)
    await update.message.reply_text("❌ Отписался.")

async def daily_broadcast(ctx: ContextTypes.DEFAULT_TYPE):
    try:
        lv = get_gold_levels()
        text = format_message(lv)
    except Exception as e:
        text = f"❌ Ошибка получения данных: {e}"

    for chat_id in load_subs():
        try:
            await ctx.bot.send_message(chat_id, text, parse_mode="Markdown")
        except Exception as e:
            logging.warning(f"Не отправилось {chat_id}: {e}")

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

    print("✅ Бот запущен")
    app.run_polling()

if __name__ == "__main__":
    main()
