import requests
import pandas as pd
import matplotlib.pyplot as plt
from io import BytesIO
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
import datetime
import os

TELEGRAM_TOKEN = "7548050336:AAFQ4WUU9ife75DxLDf9QoaLHM3kVq6EaGg"  # Замініть на ваш токен

def fetch_bybit_data(symbol: str, interval: str = "60"):
    try:
        url = f"https://api.bybit.com/v5/market/kline?category=spot&symbol={symbol}&interval={interval}&limit=200"
        response = requests.get(url)
        response.raise_for_status()
        data = response.json()

        if "result" in data and "list" in data["result"]:
            df = pd.DataFrame(
                data["result"]["list"],
                columns=["open_time", "open", "high", "low", "close", "volume", "turnover"]
            )
            df['time'] = pd.to_datetime(df['open_time'], unit='ms')
            numeric_cols = ["open", "high", "low", "close", "volume", "turnover"]
            for col in numeric_cols:
                df[col] = df[col].astype(float)

            df = df.sort_values(by='time')
            df.reset_index(drop=True, inplace=True)
            return df[["time", "open", "high", "low", "close", "volume"]]
        else:
            print(f"❌ Дані відсутні для символу {symbol} на таймфреймі {interval}")
            return None
    except Exception as e:
        print(f"❌ Помилка: {e}")
        return None

def calculate_macd(data, short_window=12, long_window=26, signal_window=9):
    short_ema = data['close'].ewm(span=short_window, adjust=False).mean()
    long_ema = data['close'].ewm(span=long_window, adjust=False).mean()
    macd = short_ema - long_ema
    signal = macd.ewm(span=signal_window, adjust=False).mean()
    return macd, signal

def calculate_bollinger_bands(data, window=20, num_std_dev=2):
    rolling = data['close'].rolling(window=window)
    middle_band = rolling.mean()
    std = rolling.std()
    upper_band = middle_band + num_std_dev * std
    lower_band = middle_band - num_std_dev * std
    return middle_band, upper_band, lower_band

def calculate_atr(data, period=14):
    df = data.copy()
    df['prev_close'] = df['close'].shift(1)
    df['tr'] = df[['high', 'low', 'prev_close']].apply(
        lambda x: max(
            x['high'] - x['low'],
            abs(x['high'] - x['prev_close']),
            abs(x['low'] - x['prev_close'])
        ),
        axis=1
    )
    atr = df['tr'].rolling(period).mean()
    return atr

def calculate_rsi(data, period=14):
    delta = data['close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(period).mean()
    rs = gain / loss
    rsi = 100 - (100 / (1 + rs))
    return rsi

def identify_candle_patterns(data):
    patterns = []
    lookback = 5
    for i in range(max(1, len(data)-lookback), len(data)):
        op = data['open'].iloc[i]
        cl = data['close'].iloc[i]
        hi = data['high'].iloc[i]
        lo = data['low'].iloc[i]

        body = abs(cl - op)
        upper_wick = hi - max(op, cl)
        lower_wick = min(op, cl) - lo

        # Doji
        if body < 0.001 * data['close'].iloc[i]:
            patterns.append(("doji", data['time'].iloc[i]))

        # Hammer
        if lower_wick > body * 2 and cl > op:
            patterns.append(("hammer", data['time'].iloc[i]))

        # Bullish engulfing
        if i > 0:
            prev_op = data['open'].iloc[i-1]
            prev_cl = data['close'].iloc[i-1]
            if cl > prev_op and op < prev_cl and (cl - op) > abs(prev_cl - prev_op):
                patterns.append(("bullish_engulfing", data['time'].iloc[i]))
    return patterns

def generate_signal(data):
    macd, macd_signal = calculate_macd(data)
    middle_band, upper_band, lower_band = calculate_bollinger_bands(data)
    rsi = calculate_rsi(data)
    atr = calculate_atr(data)

    # Для прикладу Stoх і SAR не використовуємо в логіці, але можна додати при бажанні.
    # Тут лише placeholders, якщо потрібно відобразити на графіку
    k, d = calculate_stochastic(data= data) # Якщо хочете stochastic
    sar = calculate_parabolic_sar(data= data) # Якщо хочете SAR

    latest_close = data['close'].iloc[-1]
    latest_macd = macd.iloc[-1]
    latest_macd_signal = macd_signal.iloc[-1]
    latest_rsi = rsi.iloc[-1]
    latest_upper_band = upper_band.iloc[-1]
    latest_lower_band = lower_band.iloc[-1]
    latest_atr = atr.iloc[-1]

    buy_signal = (latest_macd > latest_macd_signal) and (latest_rsi < 50) and (latest_close <= latest_lower_band * 1.02)
    sell_signal = (latest_macd < latest_macd_signal) and (latest_rsi > 50) and (latest_close >= latest_upper_band * 0.98)

    patterns = identify_candle_patterns(data)
    pattern_names = [p[0] for p in patterns]

    signal_type = None
    explanation = ""

    if buy_signal:
        signal_type = "buy"
        explanation = (
            f"Сигнал на купівлю:\n"
            f"MACD({latest_macd:.2f}) > Signal({latest_macd_signal:.2f})\n"
            f"RSI({latest_rsi:.2f}) < 50\n"
            f"Ціна({latest_close:.2f}) близько нижньої Bollinger({latest_lower_band:.2f})"
        )
        if 'hammer' in pattern_names or 'bullish_engulfing' in pattern_names:
            explanation += "\n(Патерн свічки підсилює сигнал: Hammer або Bullish Engulfing)"
    elif sell_signal:
        signal_type = "sell"
        explanation = (
            f"Сигнал на продаж:\n"
            f"MACD({latest_macd:.2f}) < Signal({latest_macd_signal:.2f})\n"
            f"RSI({latest_rsi:.2f}) > 50\n"
            f"Ціна({latest_close:.2f}) близько верхньої Bollinger({latest_upper_band:.2f})"
        )
        if 'doji' in pattern_names:
            explanation += "\n(Doji може вказувати на невизначеність)"
    else:
        # Якщо сигналу немає, але є патерни
        if 'hammer' in pattern_names and latest_close <= latest_lower_band * 1.02:
            explanation = "Нема чіткого сигналу, але hammer на нижній Bollinger."
        elif 'bullish_engulfing' in pattern_names and latest_close <= latest_lower_band * 1.02:
            explanation = "Нема чіткого сигналу, але bullish engulfing на нижній Bollinger."
        elif 'doji' in pattern_names:
            explanation = "Нема чіткого сигналу, але є doji (невизначеність)."

    if signal_type == "buy":
        entry = latest_close
        tp = round(entry + latest_atr * 2, 2)
        sl = round(entry - latest_atr * 1.5, 2)
    elif signal_type == "sell":
        entry = latest_close
        tp = round(entry - latest_atr * 2, 2)
        sl = round(entry + latest_atr * 1.5, 2)
    else:
        entry, tp, sl = None, None, None

    return (signal_type, explanation, entry, tp, sl,
            macd, macd_signal, rsi, middle_band, upper_band, lower_band, atr, k, d, sar)

def log_signal(symbol, interval, signal_type, entry, tp, sl, explanation):
    if not os.path.exists("signals_log.csv"):
        with open("signals_log.csv", "w") as f:
            f.write("timestamp,symbol,interval,signal_type,entry,tp,sl,explanation\n")
    with open("signals_log.csv", "a") as f:
        f.write(f"{datetime.datetime.utcnow()},{symbol},{interval},{signal_type},{entry},{tp},{sl},{explanation}\n")

# Stochastic та SAR якщо потрібно відобразити
def calculate_stochastic(data, period=14, d_period=3):
    low_min = data['low'].rolling(period).min()
    high_max = data['high'].rolling(period).max()
    k = 100 * (data['close'] - low_min) / (high_max - low_min)
    d = k.rolling(d_period).mean()
    return k, d

def calculate_parabolic_sar(data, af=0.02, af_max=0.2):
    # Спрощена версія SAR для демонстрації
    median_price = (data['high'] + data['low'])/2
    sar = median_price.ewm(alpha=0.1).mean()
    return sar

def generate_chart(data, macd, macd_signal, rsi, middle_band, upper_band, lower_band, atr, k, d, sar,
                   entry=None, tp=None, sl=None):
    if data is None or data.empty:
        return None

    plt.rcParams.update({'font.size': 10})
    plt.rcParams['axes.titlesize'] = 12
    fig, axs = plt.subplots(5, 1, figsize=(12, 12), sharex=True, gridspec_kw={'height_ratios': [3,1,1,1,1]})

    # Ціна + Bollinger + SAR
    axs[0].plot(data['time'], data['close'], label='Ціна', color='blue', linewidth=1.5)
    axs[0].plot(data['time'], middle_band, label='SMA 20', color='orange', linewidth=1)
    axs[0].plot(data['time'], upper_band, label='Upper Band', linestyle='dotted', color='green', linewidth=1)
    axs[0].plot(data['time'], lower_band, label='Lower Band', linestyle='dotted', color='red', linewidth=1)

    if entry is not None:
        axs[0].axhline(y=entry, color='yellow', linestyle='--', linewidth=1, label=f"Entry: {entry}")
    if tp is not None:
        axs[0].axhline(y=tp, color='green', linestyle='--', linewidth=1, label=f"TP: {tp}")
    if sl is not None:
        axs[0].axhline(y=sl, color='red', linestyle='--', linewidth=1, label=f"SL: {sl}")

    for i in range(len(data)):
        if sar.iloc[i] < data['close'].iloc[i]:
            axs[0].plot(data['time'].iloc[i], sar.iloc[i], marker='.', color='green')
        else:
            axs[0].plot(data['time'].iloc[i], sar.iloc[i], marker='.', color='red')

    axs[0].set_title("Ціна + Bollinger Bands + SAR")
    axs[0].legend()
    axs[0].grid(True)

    # MACD
    axs[1].plot(data['time'], macd, label="MACD", color='purple', linewidth=1.5)
    axs[1].plot(data['time'], macd_signal, label="Signal", color='magenta', linewidth=1)
    axs[1].axhline(y=0, color='gray', linestyle='--', linewidth=1)
    axs[1].set_title("MACD")
    axs[1].legend()
    axs[1].grid(True)

    # RSI
    axs[2].plot(data['time'], rsi, label="RSI", color='brown', linewidth=1.5)
    axs[2].axhline(y=70, color='red', linestyle='--', linewidth=1)
    axs[2].axhline(y=30, color='green', linestyle='--', linewidth=1)
    axs[2].set_title("RSI")
    axs[2].legend()
    axs[2].grid(True)

    # Stochastic
    axs[3].plot(data['time'], k, label="%K", color='blue', linewidth=1.5)
    axs[3].plot(data['time'], d, label="%D", color='orange', linewidth=1)
    axs[3].axhline(y=80, color='red', linestyle='--', linewidth=1)
    axs[3].axhline(y=20, color='green', linestyle='--', linewidth=1)
    axs[3].set_title("Stochastic")
    axs[3].legend()
    axs[3].grid(True)

    # ATR
    axs[4].plot(data['time'], atr, label="ATR", color='black', linewidth=1.5)
    axs[4].set_title("ATR")
    axs[4].legend()
    axs[4].grid(True)

    plt.tight_layout()

    buffer = BytesIO()
    plt.savefig(buffer, format="png")
    buffer.seek(0)
    plt.close(fig)
    return buffer

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "✅ Бот активовано!\n"
        "Команди:\n"
        "/start - Запуск бота\n"
        "/signal SYMBOL INTERVAL - Генерація сигналу з multi-timeframe та фільтрацією за обсягом"
    )

async def signal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        args = context.args
        if len(args) < 2:
            await update.message.reply_text("Використання: /signal SYMBOL INTERVAL (наприклад: /signal BTCUSDT 60)")
            return
        symbol = args[0]
        interval = args[1]

        await update.message.reply_text(f"🔄 Генерую сигнал для {symbol} на {interval}...")

        data_main = fetch_bybit_data(symbol, interval=interval)
        if data_main is None or data_main.empty:
            await update.message.reply_text(f"❌ Не вдалося отримати дані для {symbol} на {interval}.")
            return

        data_lower = fetch_bybit_data(symbol, interval="15")
        data_higher = fetch_bybit_data(symbol, interval="240")

        main_signal = generate_signal(data_main)
        lower_signal = generate_signal(data_lower) if (data_lower is not None and not data_lower.empty) else (None,)*15
        higher_signal = generate_signal(data_higher) if (data_higher is not None and not data_higher.empty) else (None,)*15

        (main_type, main_expl, main_entry, main_tp, main_sl,
         main_macd, main_macd_signal, main_rsi, main_mb, main_ub, main_lb, main_atr, main_k, main_d, main_sar) = main_signal

        (lower_type, *_) = lower_signal
        (higher_type, *_) = higher_signal

        final_type = main_type
        if main_type == "buy":
            if lower_type == "sell" or higher_type == "sell":
                final_type = None  # Відхиляємо сигнал
        elif main_type == "sell":
            if lower_type == "buy" or higher_type == "buy":
                final_type = None

        avg_volume = data_main['volume'].rolling(window=20).mean().iloc[-1] if len(data_main) > 20 else data_main['volume'].mean()
        current_volume = data_main['volume'].iloc[-1]
        volume_factor = current_volume / avg_volume if avg_volume else 1

        if final_type is not None and volume_factor < 0.5:
            final_type = None
            main_expl += "\nОбсяг занадто низький - сигнал відхилено."

        if final_type is not None and volume_factor > 2:
            main_expl += "\nОбсяг дуже високий - сигнал підсилено обсягом."

        chart = generate_chart(data_main, main_macd, main_macd_signal, main_rsi, main_mb, main_ub, main_lb, main_atr, main_k, main_d, main_sar, main_entry, main_tp, main_sl)

        if final_type is not None:
            caption = (
                f"📊 Сигнал для {symbol} [{interval}]:\n"
                f"Тип сигналу: {final_type.upper()}\n"
                f"Entry: {main_entry}\n"
                f"TP: {main_tp}\n"
                f"SL: {main_sl}\n\n"
                f"{main_expl}\n"
                f"- Multi-timeframe фільтр\n"
                f"- Фільтрація за обсягом"
            )
            log_signal(symbol, interval, final_type, main_entry, main_tp, main_sl, main_expl)
        else:
            caption = (
                f"Для {symbol} [{interval}] немає чіткого сигналу.\n"
                f"{main_expl}\n"
                f"- Multi-timeframe фільтр\n"
                f"- Фільтрація за обсягом"
            )

        await update.message.reply_photo(photo=chart, caption=caption)

    except Exception as e:
        await update.message.reply_text(f"❌ Помилка: {e}")

def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("signal", signal))
    print("✅ Бот запущено! Використовуйте /start")
    app.run_polling()

if __name__ == "__main__":
    main()