import requests
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from io import BytesIO
import sys
from telegram import Bot
import asyncio
from telegram import Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, ContextTypes,
    JobQueue
)
import datetime
import os

# Якщо хочемо tvdatafeed
try:
    from tvDatafeed import TvDatafeed, Interval
    tvdata_installed = True
except ImportError:
    TvDatafeed = None
    Interval = None
    tvdata_installed = False

if not tvdata_installed:
    print("❌ tvDatafeed не встановлено. Для встановлення: pip install tvdatafeed")

# ========== Налаштування ==========
TELEGRAM_TOKEN = "7548050336:AAEZe89_zJ66rFK-tN-l3ZbBPRY3u2hFcs0"

BINANCE_INTERVAL = "1h"         # 1m,3m,5m,15m,30m,1h,2h,4h,6h,8h,12h,1d
CHECK_INTERVAL_SEC = 900
THRESHOLD_FLAT = 0.01

BINANCE_API_KEY = "fo8MS8lNSI7YPkD2fcncjgyjHVoWMncXcS0xXY0fjKo7fmaFvnrtaXxmpKsGx3oQ"
BINANCE_API_SECRET = "gDVNllBbJ7xxFyw2HajJeJ8uTMOKnVkkW0zSzANC380Mzkojnyr5WE3FE0aATKeV"

TV_USERNAME = "uthhtu"
TV_PASSWORD = "Berezynskyi2004"

# Приклад мапи для TradingView (символ -> (symbol, exchange))
# Наприклад, якщо "APTUSDT" немає на Binance, але є "APTUSDT" на HUOBI в TV:
tv_symbol_map = {
    # "APTUSDT": ("APTUSDT","HUOBI"),
    # "SOMECOINUSDT": ("SOMECOINUSDT","GATEIO"),
    # ...
}

# ===============================
# 1. Отримання списку Binance Futures
# ===============================
def fetch_binance_symbols_futures() -> list:
    """
    Використовуємо Binance /fapi/v1/exchangeInfo, повертає список символів
    USDT-маржинальних ф’ючерсів у статусі TRADING.
    """
    url = "https://fapi.binance.com/fapi/v1/exchangeInfo"
    try:
        resp = requests.get(url)
        resp.raise_for_status()
        data = resp.json()
        symbols = []
        for s in data["symbols"]:
            if s["quoteAsset"] == "USDT" and s["status"] == "TRADING":
                symbols.append(s["symbol"])
        return symbols
    except Exception as e:
        print(f"Помилка отримання списку Binance Futures: {e}")
        return []

# ===============================
# 2. Отримання історії з Binance
# ===============================
def fetch_binance_futures_data(symbol: str, interval: str = "1h", limit: int = 200):
    """
    GET /fapi/v1/klines
    """
    url = (
        "https://fapi.binance.com/fapi/v1/klines"
        f"?symbol={symbol}&interval={interval}&limit={limit}"
    )
    headers = {
        "X-MBX-APIKEY": BINANCE_API_KEY
    }
    r = requests.get(url, headers=headers)
    try:
        r = requests.get(url)
        r.raise_for_status()
        data = r.json()
        if not data:
            return None

        df = pd.DataFrame(data, columns=[
            "openTime","open","high","low","close","volume",
            "closeTime","quoteAssetVolume","trades","takerBase","takerQuote","ignore"
        ])
        df["openTime"] = pd.to_datetime(df["openTime"], unit="ms")
        numeric_cols = ["open","high","low","close","volume"]
        for col in numeric_cols:
            df[col] = df[col].astype(float)
        df = df.sort_values("openTime")
        df.reset_index(drop=True, inplace=True)
        df.rename(columns={"openTime":"time"}, inplace=True)
        return df[["time","open","high","low","close","volume"]]
    except Exception as e:
        print(f"❌ fetch_binance_futures_data({symbol}) помилка: {e}")
        return None

# ===============================
# 3. TradingView (tvDatafeed)
# ===============================
def init_tvDatafeed():
    """
    Ініціалізує tvDatafeed з логіном/паролем.
    """
    if TvDatafeed is None or Interval is None:
        print("tvDatafeed не імпортовано, встановіть бібліотеку.")
        return None
    try:
        tv = TvDatafeed(
            username=TV_USERNAME,
            password=TV_PASSWORD
        )
        return tv
    except Exception as e:
        print(f"Помилка ініціалізації tvDatafeed: {e}")
        return None

def fetch_data_from_tv(tv: TvDatafeed, symbol: str, exchange: str, interval=None, bars=200):
    """
    Завантажуємо історію свічок з TradingView (tvDatafeed).
    symbol: наприклад, "APTUSDT"
    exchange: наприклад, "HUOBI", "BINANCE", ...
    interval: Interval.in_1_hour, ...
    """

    def fetch_data_from_tv(tv: TvDatafeed, symbol: str, exchange: str, interval=None, bars=200):
        if interval is None:
            from tvDatafeed import Interval
            interval = Interval.in_1_hour

        try:
            data = tv.get_hist(
                symbol=symbol,
                exchange=exchange,
                interval=interval,
                n_bars=bars
            )
            if data is None or data.empty:
                print(f"❌ Дані для {symbol} з TradingView не знайдені.")
                return None
            data.reset_index(inplace=True)
            data.rename(columns={"datetime": "time"}, inplace=True)
            df = data[["time", "open", "high", "low", "close", "volume"]].copy()
            df = df.sort_values("time")
            df.reset_index(drop=True, inplace=True)
            return df
        except Exception as e:
            print(f"❌ fetch_data_from_tv({symbol}, {exchange}) помилка: {e}")
            return None

# ===============================
# 4. BTC.D з TradingView
# ===============================
def fetch_btc_dominance_tv(limit=200, interval=None):
    if interval is None:
        from tvDatafeed import Interval
        interval = Interval.in_1_hour
    """
    CRYPTOCAP:BTC.D
    """
    tv = init_tvDatafeed()
    if tv is None:
        return None
    return fetch_data_from_tv(tv, "BTC.D", "CRYPTOCAP", interval=interval, bars=limit)

# ===============================
# 5. Інші ваші функції (індикатори, патерни, signal, chart)
# ===============================
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

# ===============================
# 6. Логіка BTC + BTC.D
# ===============================
# =======================
#  ЛОГІКА BTC та BTC.D
# =======================
def get_trend(data, threshold=0.01):
    """
    Повертає 'rising', 'falling' або 'flat' (стабільно),
    порівнюючи останню ціну з ціною N свічок тому.
    threshold=0.01 означає 1% різницю – менше => flat.
    """
    if data is None or len(data) < 2:
        return "flat"
    last_close = data['close'].iloc[-1]
    prev_close = data['close'].iloc[-5]  # порівнюємо з ціною 5 свічок тому
    diff = (last_close - prev_close) / prev_close
    if diff > threshold:
        return "rising"
    elif diff < -threshold:
        return "falling"
    else:
        return "flat"

def alt_signal_adjustment(btcd_trend, btc_trend):
    """
    На основі вашої таблиці повертає, що відбувається з ALTS:
    'drop', 'drop_strong', 'stable', 'rise_strong', 'rise', ...
    Приклад:
      BTC.D = rising, BTC = rising => ALTS = drop
      BTC.D = rising, BTC = falling => ALTS = drop_strong
      ...
    """
    if btcd_trend == "rising" and btc_trend == "rising":
        return "drop"
    if btcd_trend == "rising" and btc_trend == "falling":
        return "drop_strong"
    if btcd_trend == "rising" and btc_trend == "flat":
        return "stable"

    if btcd_trend == "falling" and btc_trend == "rising":
        return "rise_strong"
    if btcd_trend == "falling" and btc_trend == "falling":
        return "stable"
    if btcd_trend == "falling" and btc_trend == "flat":
        return "rise"

    # Якщо немає чіткої умови
    return "stable"

def adjust_final_signal(alt_signal, alt_trend_from_table):
    """
    Якщо alt_signal = buy, але з таблиці = drop_strong, то краще відхилити.
    Якщо alt_signal = buy, а таблиця каже rise_strong, це підтверджує buy.
    І так далі.
    Приклад логіки:
    """
    if alt_signal == "buy":
        if alt_trend_from_table in ["drop", "drop_strong"]:
            return None  # ігноруємо buy
        # якщо "rise", "rise_strong", "stable" => лишаємо buy
    elif alt_signal == "sell":
        if alt_trend_from_table in ["rise", "rise_strong"]:
            return None  # ігноруємо sell
        # якщо "drop", "drop_strong", "stable" => лишаємо sell
    return alt_signal

# ===============================
# 7. Автоматична перевірка
# ===============================
async def check_signals(context: ContextTypes.DEFAULT_TYPE):
    """
    Функція перевірки сигналів.
    Викликається JobQueue кожні 4 години.
    """
    print("Функція check_signals запущена")
    chat_id = context.job.chat_id

    try:
        # Логіка перевірки сигналів
        await context.bot.send_message(chat_id=chat_id, text="🔄 Перевіряємо сигнали...")
        print("✅ Сигнали перевіряються.")

        # Основна логіка
        all_symbols = fetch_binance_symbols_futures()
        if not all_symbols:
            await context.bot.send_message(chat_id=chat_id, text="❌ Не вдалося отримати список пар з Binance.")
            return

        data_btc = fetch_binance_futures_data("BTCUSDT", interval=BINANCE_INTERVAL)
        btc_trend = get_trend(data_btc)
        data_btcd = fetch_btc_dominance_tv()
        btcd_trend = get_trend(data_btcd)
        alts_outlook = alt_signal_adjustment(btcd_trend, btc_trend)

        found_any_signal = False
        for symbol in all_symbols:
            if symbol == "BTCUSDT":
                continue
            df = fetch_binance_futures_data(symbol, interval=BINANCE_INTERVAL)
            if df is None or df.empty:
                continue
            (sig_type, explanation, entry, tp, sl,
             macd, macd_signal, rsi,
             mb, ub, lb, atr,
             k, d, sar) = generate_signal(df)
            final_signal = adjust_final_signal(sig_type, alts_outlook)
            if final_signal is not None:
                found_any_signal = True
                caption = (
                    f"АвтоСигнал для {symbol}:\n"
                    f"Тип: {final_signal.upper()}\n"
                    f"Entry: {entry}\n"
                    f"TP: {tp}\n"
                    f"SL: {sl}\n\n"
                    f"{explanation}\n"
                    f"BTC={btc_trend}, BTC.D={btcd_trend} => ALTS={alts_outlook}"
                )
                chart = generate_chart(df, macd, macd_signal, rsi, mb, ub, lb, atr, k, d, sar, entry, tp, sl)
                await context.bot.send_photo(chat_id=chat_id, photo=chart, caption=caption)
        if not found_any_signal:
            await context.bot.send_message(chat_id=chat_id, text="Немає сигналів на даний момент.")
    except Exception as e:
        print(f"❌ Помилка у check_signals: {e}")
        await context.bot.send_message(chat_id=chat_id, text=f"❌ Помилка у перевірці сигналів: {e}")

# ===============================
# /START і /SIGNAL
# ===============================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Обробник команди /start.
    Запускає перевірку сигналів кожні 4 години.
    """
    await update.message.reply_text(
        "✅ Бот активовано!\n"
        "Сигнали будуть надсилатися кожну годину."
    )

    job_queue = context.application.job_queue
    if job_queue is None:
        print("❌ JobQueue не ініціалізовано. Перевірте залежності.")
        return

    # Видалення існуючих завдань для запобігання дублюванню
    current_jobs = job_queue.get_jobs_by_name("check_signals")
    for job in current_jobs:
        job.schedule_removal()

    # Додаємо завдання до JobQueue
    job_queue.run_repeating(
        callback=check_signals,   # Функція, яка викликається
        interval=3600,          # Інтервал 1 година
        first=10,                # Перше виконання через 10 секунд
        name="check_signals",    # Унікальне ім'я завдання
        chat_id=update.effective_chat.id  # ID чату
    )
    print("✅ JobQueue успішно запущено. Сигнали надсилатимуться кожну годину.")

async def signal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Вручну: /signal SYMBOL
    1) Спершу Binance
    2) Якщо ні - TradingView
    """
    try:
        args = context.args
        if len(args) < 1:
            await update.message.reply_text("Використання: /signal SYMBOL (наприклад: /signal ETHUSDT)")
            return
        symbol = args[0]

        # Ініціалізуємо tvDatafeed
        tv = init_tvDatafeed()

        # BTC => Binance
        data_btc = fetch_binance_futures_data("BTCUSDT", BINANCE_INTERVAL)
        btc_tr = get_trend(data_btc)
        # BTC.D => tv
        data_btcd = fetch_btc_dominance_tv(200, Interval.in_1_hour) if tv else None
        btcd_tr = get_trend(data_btcd)
        alts_outlook = alt_signal_adjustment(btcd_tr, btc_tr)

        # Пробуємо Binance
        df = fetch_binance_futures_data(symbol, BINANCE_INTERVAL)
        if df is None or df.empty:
            # Спробуємо tv_symbol_map
            if tv and (symbol in tv_symbol_map):
                tv_sym, tv_exch = tv_symbol_map[symbol]
                df = fetch_data_from_tv(tv, tv_sym, tv_exch, interval=Interval.in_1_hour)
            else:
                await update.message.reply_text(f"❌ Не вдалося знайти {symbol} на BinanceFutures і не маємо мапи tv.")
                return

        if df is None or df.empty:
            await update.message.reply_text(f"❌ Даних немає для {symbol}.")
            return

        (sig_type, explanation, entry, tp, sl,
         macd, macd_signal, rsi,
         mb, ub, lb, atr,
         k, d, sar) = generate_signal(df)

        final_signal = adjust_final_signal(sig_type, alts_outlook)
        if final_signal is not None:
            chart = generate_chart(df, macd, macd_signal, rsi, mb, ub, lb, atr, k, d, sar, entry, tp, sl)
            caption = (
                f"Сигнал для {symbol}:\n"
                f"Тип: {final_signal.upper()}\n"
                f"Entry: {entry}\n"
                f"TP: {tp}\n"
                f"SL: {sl}\n\n"
                f"{explanation}\n"
                f"BTC={btc_tr}, BTC.D={btcd_tr} => ALTS={alts_outlook}"
            )
            await update.message.reply_photo(photo=chart, caption=caption)
        else:
            await update.message.reply_text("Нема чіткого сигналу.")
    except Exception as e:
        await update.message.reply_text(f"❌ Помилка: {e}")

# Головна функція
def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))

    # Перевіряємо аргументи командного рядка
    if len(sys.argv) > 1 and sys.argv[1] == "check_signals":
        # Запуск тільки функції перевірки сигналів
        print("🔄 Запускаємо check_signals через Heroku Scheduler")
        asyncio.run(run_check_signals())
    else:
        # Стандартний запуск бота
        print("✅ Бот запущено! Використовуйте /start")
        app.run_polling()

async def run_check_signals():
    """
    Функція для запуску перевірки сигналів.
    """
    try:
        chat_id = 542817935 # Замінити на свій Chat ID
        bot = Bot(token=TELEGRAM_TOKEN)

        # Основна логіка перевірки сигналів
        await bot.send_message(chat_id=chat_id, text="Запуск перевірки сигналів...")
        # Тут можна додати основну логіку, наприклад:
        # await bot.send_message(chat_id=chat_id, text="Сигнали перевірено. Поки що без змін.")

        print("✅ Повідомлення успішно надіслано.")
    except Exception as e:
        print(f"❌ Помилка у run_check_signals: {e}")

if __name__ == "__main__":
        main()