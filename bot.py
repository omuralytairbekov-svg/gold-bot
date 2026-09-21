import os
import json
import logging
from datetime import datetime, time
import pytz
import requests
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

BOT_TOKEN = os.environ["BOT_TOKEN"]
SUBSCRIBERS_FILE = "subscribers.json"
SEND_HOUR_NY = 9
NY_TZ = pytz.timezone("America/New_York")

# ═══════════════════════════════════════════════════════
#  НАСТРОЙКИ — ОБНОВЛЯЙ РАЗ В ДЕНЬ (или раз в неделю)
# ═══════════════════════════════════════════════════════

# Фолбэк цена золота (если metals.live не ответит)
FALLBACK_GOLD = 4372.0

# Коэффициент GLD → золото (обнови если сильно меняется)
GLD_TO_GOLD = 10.90

# Экспирация (для отображения)
EXPIRY = "2026-09-21 (0DTE)"

# ═══════════════════════════════════════════════════════
#  ОПЦИОННАЯ ЦЕПОЧКА GLD — ОБНОВЛЯЙ 1-2 РАЗА В ДЕНЬ
#  Формат: strike: (Call OI, Put OI)
#  Источник: barchart.com/stocks/quotes/GLD/options
# ═══════════════════════════════════════════════════════

CHAIN = {
    # Страйк: (Call OI, Put OI) — данные 21.09.2026, 0DTE
    390: (42,   96),
    391: (43,   88),
    392: (40,   88),
    393: (40,   95),
    394: (297,  231),
    395: (75,   161),
    396: (66,   717),    # ← Put Wall (максимум Put OI)
    397: (119,  257),
    398: (85,   167),
    399: (105,  132),
    400: (70,   485),
    401: (396,  61),
    402: (165,  99),
    403: (73,   39),
    404: (100,  48),
    405: (90,   47),
    406: (310,  8),      # ← сопротивление
    407: (128,  11),
    408: (72,   50),
    409: (90,   10),
    410: (82,   16),
    411: (718,  12),     # ← Call Wall 2
    412: (76,   2),
    413: (96,   2),
    414: (184,  4),
    415: (96,   1),
    416: (865,  3),      # ← Call Wall (максимум Call OI)
    417: (67,   0),
    418: (43,   0),
}

logging.basicConfig(level=logging.INFO)


# ═══════════════════════════════════════════════════════
#  АВТОЦЕНА ЗОЛОТА — metals.live (без блокировок)
# ═══════════════════════════════════════════════════════

def get_gold_price():
    """Цена золота через metals.live (надёжный, без API-ключа)."""
    try:
        r = requests.get(
            "https://api.metals.live/v1/spot/gold",
            timeout=10,
            headers={"User-Agent": "Mozilla/5.0"}
        )
        r.raise_for_status()
        data = r.json()
        # формат: [{"gold": 4372.5}]
        if isinstance(data, list) and data:
            price = data[0].get("gold")
            if price:
                return float(price), "metals.live"
    except Exception as e:
        logging.warning(f"metals.live failed: {e}")

    # Фолбэк — Фикс
    return FALLBACK_GOLD, "fallback"


# ═══════════════════════════════════════════════════════
#  РАСЧЁТ УРОВНЕЙ
# ═══════════════════════════════════════════════════════

def get_gold_levels():
    spot, source = get_gold_price()
    logging.info(f"Gold source={source}, spot={spot}")

    strikes = sorted(CHAIN.keys())

    # Коэффициент GLD→золото (динамический)
    # GLD ≈ spot / 10.9, значит соотношение = 10.9
    ratio = GLD_TO_GOLD

    # Call Wall — страйк с макс Call OI
    call_wall_k = max(strikes, key=lambda k: CHAIN[k][0])
    call_wall = round(call_wall_k * ratio)

    # Put Wall — страйк с макс Put OI
    put_wall_k = max(strikes, key=lambda k: CHAIN[k][1])
    put_wall = round(put_wall_k * ratio)

    # Топ-2 уровней (резистенс/поддержка возле цены)
    def second_call_wall():
        # Второй по величине Call OI
        sorted_calls = sorted(strikes, key=lambda k: -CHAIN[k][0])
        return round(sorted_calls[1] * ratio)

    def second_put_wall():
        sorted_puts = sorted(strikes, key=lambda k: -CHAIN[k][1])
        return round(sorted_puts[1] * ratio)

    # Max Pain
    pains = []
    for K in strikes:
        cl = sum(max(0, (K - s)) * CHAIN[s][0] for s in strikes)
        pl = sum(max(0, (s - K)) * CHAIN[s][1] for s in strikes)
        pains.append(cl + pl)
    max_pain_k = strikes[pains.index(min(pains))]
    max_pain = round(max_pain_k * ratio)

    # P/C ratio
    total_c = sum(v[0] for v in CHAIN.values())
    total_p = sum(v[1] for v in CHAIN.values())
    pc_ratio = round(total_p / total_c, 2) if total_c else 0

    # Top-3
    top_calls = sorted(strikes, key=lambda k: -CHAIN[k][0])[:3]
    top_puts = sorted(strikes, key=lambda k: -CHAIN[k][1])[:3]

    return {
        "spot": round(spot, 2),
        "source": source,
        "expiry": EXPIRY,
        "call_wall": call_wall,
        "resistance": second_call_wall(),
        "put_wall": put_wall,
        "support": second_put_wall(),
        "max_pain": max_pain,
        "pc_ratio": pc_ratio,
        "top_calls": [(round(k * ratio), CHAIN[k][0]) for k in top_calls],
        "top_puts": [(round(k * ratio), CHAIN[k][1]) for k in top_puts],
    }


# ═══════════════════════════════════════════════════════
#  ФОРМАТИРОВАНИЕ СООБЩЕНИЯ
# ═══════════════════════════════════════════════════════

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
        f"🟢 Resistance: *${lv['resistance']}*",
        f"⚪ Max Pain: *${lv['max_pain']}*",
        f"🔴 Put Wall: *${lv['put_wall']}*",
        f"🔴 Support: *${lv['support']}*",
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


# ═══════════════════════════════════════════════════════
#  КОМАНДЫ БОТА
# ═══════════════════════════════════════════════════════

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🥇 *Gold Levels Bot*\n\n"
        "Показываю опционные уровни по золоту (XAUUSD).\n\n"
        "/levels — уровни на сегодня\n"
        "/subscribe — ежедневная рассылка в 16:00 МСК\n"
        "/unsubscribe — отписаться",
        parse_mode="Markdown"
    )


async def cmd_levels(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        lv = get_gold_levels()
        await update.message.reply_text(
            format_message(lv),
            parse_mode="Markdown"
        )
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")


async def cmd_subscribe(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    subs = load_subs()
    subs.add(update.effective_chat.id)
    save_subs(subs)
    await update.message.reply_text("✅ Подписан! Уровни будут приходить ежедневно в 16:00 МСК.")


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


# ═══════════════════════════════════════════════════════
#  ХРАНЕНИЕ ПОДПИСЧИКОВ
# ═══════════════════════════════════════════════════════

def load_subs():
    if os.path.exists(SUBSCRIBERS_FILE):
        with open(SUBSCRIBERS_FILE) as f:
            return set(json.load(f))
    return set()


def save_subs(subs):
    with open(SUBSCRIBERS_FILE, "w") as f:
        json.dump(list(subs), f)


# ═══════════════════════════════════════════════════════
#  ЗАПУСК
# ═══════════════════════════════════════════════════════

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
