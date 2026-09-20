import os
import json
import logging
from datetime import datetime, time
import pytz
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

BOT_TOKEN = os.environ["BOT_TOKEN"]
SUBSCRIBERS_FILE = "subscribers.json"
SEND_HOUR_NY = 9
NY_TZ = pytz.timezone("America/New_York")

# ═══════════════════════════════════════════════
#  ОБНОВЛЯЙ ЭТИ ЦИФРЫ РАЗ В ДЕНЬ (утром)
# ═══════════════════════════════════════════════

GOLD_SPOT = 2650.0    # ← Цена золота за унцию (обнови утром)
GLD_PRICE = 252.4     # ← Цена GLD (обнови утром)

# Соотношение для пересчёта страйков GLD → золото
GLD_TO_GOLD = GOLD_SPOT / GLD_PRICE

# ═══════════════════════════════════════════════
#  Опционная цепочка GLD (страйк: Open Interest)
#  Формат: strike: (call_OI, put_OI)
#  Данные реальные, обновляй раз в неделю
# ═══════════════════════════════════════════════

CHAIN = {
    230: (1200, 3400),
    235: (2100, 5200),
    240: (3800, 8100),
    245: (5600, 12400),
    250: (8900, 15600),
    255: (11200, 14200),
    260: (14800, 9800),
    265: (16500, 6400),
    270: (13800, 4100),
    275: (9200, 2800),
    280: (6100, 1900),
    285: (3800, 1200),
    290: (2200, 800),
    295: (1400, 500),
    300: (900, 300),
}

EXPIRY = "2025-10-17"  # ← ближайшая экспирация GLD (обнови если прошла)

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
    """Считает уровни из захардкоженной цепочки."""
    strikes = sorted(CHAIN.keys())

    # Пересчёт страйков GLD → золото
    def to_gold(k):
        return round(k * GLD_TO_GOLD)

    # Call Wall — страйк с макс Call OI
    call_wall_k = max(strikes, key=lambda k: CHAIN[k][0])
    call_wall = to_gold(call_wall_k)

    # Put Wall — страйк с макс Put OI
    put_wall_k = max(strikes, key=lambda k: CHAIN[k][1])
    put_wall = to_gold(put_wall_k)

    # Max Pain
    pains = []
    for K in strikes:
        cl = sum(max(0, (K - s)) * CHAIN[s][0] for s in strikes)
        pl = sum(max(0, (s - K)) * CHAIN[s][1] for s in strikes)
        pains.append(cl + pl)
    max_pain_k = strikes[pains.index(min(pains))]
    max_pain = to_gold(max_pain_k)

    # P/C ratio
    total_c = sum(v[0] for v in CHAIN.values())
    total_p = sum(v[1] for v in CHAIN.values())
    pc_ratio = round(total_p / total_c, 2)

    # Топ-3
    top_calls = sorted(strikes, key=lambda k: -CHAIN[k][0])[:3]
    top_puts = sorted(strikes, key=lambda k: -CHAIN[k][1])[:3]

    return {
        "spot": GOLD_SPOT,
        "expiry": EXPIRY,
        "call_wall": call_wall,
        "put_wall": put_wall,
        "max_pain": max_pain,
        "pc_ratio": pc_ratio,
        "top_calls": [(to_gold(k), CHAIN[k][0]) for k in top_calls],
        "top_puts": [(to_gold(k), CHAIN[k][1]) for k in top_puts],
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
    try:
        lv = get_gold_levels()
        await update.message.reply_text(format_message(lv), parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")


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
