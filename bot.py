import os
import json
import logging
from datetime import datetime, time
import pytz
import requests
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

BOT_TOKEN = os.environ["BOT_TOKEN"]
FINNHUB_KEY = os.environ.get("FINNHUB_KEY", "")
SUBSCRIBERS_FILE = "subscribers.json"
SEND_HOUR_NY = 9
NY_TZ = pytz.timezone("America/New_York")

# Фолбэк цена золота (если металл-API не ответит)
FALLBACK_GOLD = 4372.0
GLD_TO_GOLD = 10.90

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
    """Цена золота через metals.live, с фолбэком."""
    try:
        r = requests.get(
            "https://api.metals.live/v1/spot/gold",
            timeout=10,
            headers={"User-Agent": "Mozilla/5.0"}
        )
        r.raise_for_status()
        data = r.json()
        if isinstance(data, list) and data:
            price = data[0].get("gold")
            if price:
                return float(price), "metals.live"
    except Exception as e:
        logging.warning(f"metals.live failed: {e}")
    return FALLBACK_GOLD, "fallback"


def fetch_option_chain():
    """Получает опционную цепочку GLD через Finnhub."""
    url = "https://finnhub.io/api/v1/stock/option-chain"
    params = {"symbol": "GLD", "token": FINNHUB_KEY}
    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    data = r.json()

    if "data" not in data or not data["data"]:
        raise Exception(f"Finnhub: no data. Response: {str(data)[:200]}")

    # Берём ближайшую экспирацию
    expirations = sorted([d["expirationDate"] for d in data["data"]])
    nearest = expirations[0]

    # Находим нужный блок
    block = next(d for d in data["data"] if d["expirationDate"] == nearest)
    calls = block["options"]["CALL"]
    puts = block["options"]["PUT"]

    # Сводим OI по страйкам
    chain = {}  # strike: [call_oi, put_oi]
    for c in calls:
        k = float(c["strike"])
        oi = int(c.get("openInterest") or 0)
        chain.setdefault(k, [0, 0])[0] = oi
    for p in puts:
        k = float(p["strike"])
        oi = int(p.get("openInterest") or 0)
        chain.setdefault(k, [0, 0])[1] = oi

    return chain, nearest


def get_gold_levels():
    spot, source = get_gold_price()
    logging.info(f"Gold source={source} spot={spot}")

    chain, expiry = fetch_option_chain()

    # Отбрасываем страйки с нулевым OI
    chain = {k: v for k, v in chain.items() if v[0] > 0 or v[1] > 0}
    if not chain:
        raise Exception("All strikes have zero OI")

    strikes = sorted(chain.keys())
    ratio = GLD_TO_GOLD

    # Call Wall / Put Wall
    call_wall_k = max(strikes, key=lambda k: chain[k][0])
    put_wall_k = max(strikes, key=lambda k: chain[k][1])

    # Top-3
    top_calls = sorted(strikes, key=lambda k: -chain[k][0])[:3]
    top_puts = sorted(strikes, key=lambda k: -chain[k][1])[:3]

    # Max Pain
    pains = []
    for K in strikes:
        cl = sum(max(0, (K - s)) * chain[s][0] for s in strikes)
        pl = sum(max(0, (s - K)) * chain[s][1] for s in strikes)
        pains.append(cl + pl)
    max_pain_k = strikes[pains.index(min(pains))]

    # P/C ratio
    total_c = sum(v[0] for v in chain.values())
    total_p = sum(v[1] for v in chain.values())
    pc_ratio = round(total_p / total_c, 2) if total_c else 0

    return {
        "spot": round(spot, 2),
        "source": source,
        "expiry": expiry,
        "call_wall": round(call_wall_k * ratio),
        "put_wall": round(put_wall_k * ratio),
        "max_pain": round(max_pain_k * ratio),
        "pc_ratio": pc_ratio,
        "top_calls": [(round(k * ratio), chain[k][0]) for k in top_calls],
        "top_puts": [(round(k * ratio), chain[k][1]) for k in top_puts],
    }


def format_message(lv):
    def bias(pcr):
        if pcr > 1.2:
            return "🔴 bearish"
        if pcr < 0.7:
            return "🟢 bullish"
        return "⚪ neutral"

    lines = [
        f"🥇 *GOLD / XAUUSD — {datetime.now():%d.%m.%Y}*",
        "",
        f"💰 Spot: *${lv['spot']}*",
        f"📅 Expiry: {lv['expiry']}",
        "",
        f"🟢 Call Wall: *${lv['call_wall']}*",
        f"⚪ Max Pain: *${lv['max_pain']}*",
        f"🔴 Put Wall: *${lv['put_wall']}*",
        "",
        f"📊 P/C ratio: *{lv['pc_ratio']}* — {bias(lv['pc_ratio'])}",
        "",
        "*Топ-3 сопротивления (Call OI):*",
    ]
    for s, oi in lv["top_calls"]:
        lines.append(f"  • ${s}  (OI {oi:,})")
    lines.append("")
    lines.append("*Топ-3 поддержки (Put OI):*")
    for s, oi in lv["top_puts"]:
        lines.append(f"  • ${s}  (OI {oi:,})")
    lines.append("")
    lines.append(f"_Источник цены: {lv['source']}_")
    lines.append(f"_Updated: {datetime.now():%H:%M}_")
    return "\n".join(lines)


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🥇 *Gold Levels Bot*\n\n"
        "/levels — уровни на сегодня\n"
        "/subscribe — ежедневно в 16:00 МСК\n"
        "/unsubscribe — отписаться",
        parse_mode="Markdown"
    )


async def cmd_levels(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        lv = get_gold_levels()
        await update.message.reply_text(format_message(lv), parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")


async def cmd_subscribe(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    subs = load_subs()
    subs.add(update.effective_chat.id)
    save_subs(subs)
    await update.message.reply_text("✅ Подписан! Уровни ежедневно в 16:00 МСК.")


async def cmd_unsubscribe(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    subs = load_subs()
    subs.discard(update.effective_chat.id)
    save_subs(subs)
    await update.message.reply_text("❌ Отписан.")


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
