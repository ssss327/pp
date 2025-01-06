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

# Додаємо імпорт TensorFlow, якщо ви хочете завантажувати tf-модель
import tensorflow as tf

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
TELEGRAM_TOKEN = "7548050336:AAEZe89_zJ66rFK-tN-l3ZbBPRY3u2Hfcs0"

BINANCE_INTERVAL = "1h"
CHECK_INTERVAL_SEC = 900
THRESHOLD_FLAT = 0.01

BINANCE_API_KEY = "fo8MS8lNSI7YPkD2fcncjgyjHVoWMncXcS0xXY0fjKo7fmaFvnrtaXxmpKsGx3oQ"
BINANCE_API_SECRET = "gDVNllBbJ7xxFyw2HajJeJ8uTMOKnVkkW0zSzANC380Mzkojnyr5WE3FE0aATKeV"

TV_USERNAME = "uthhtu"
TV_PASSWORD = "Berezynskyi2004"

# Приклад мапи для TradingView (символ -> (symbol, exchange))
tv_symbol_map = {
    # "APTUSDT": ("APTUSDT","HUOBI"),
    # "SOMECOINUSDT": ("SOMECOINUSDT","GATEIO"),
}

# ===============================
# 1. Отримання списку Binance Futures
# ===============================
def fetch_binance_symbols_futures() -> list:
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
    url = f"https://fapi.binance.com/fapi/v1/klines?symbol={symbol}&interval={interval}&limit={limit}"
    headers = {"X-MBX-APIKEY": BINANCE_API_KEY}
    try:
        r = requests.get(url, headers=headers)
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
    if TvDatafeed is None or Interval is None:
        print("tvDatafeed не імпортовано, встановіть бібліотеку.")
        return None
    try:
        tv = TvDatafeed(username=TV_USERNAME, password=TV_PASSWORD)
        return tv
    except Exception as e:
        print(f"Помилка ініціалізації tvDatafeed: {e}")
        return None

def fetch_data_from_tv(tv: TvDatafeed, symbol: str, exchange: str, interval=None, bars=200):
    if interval is None:
        from tvDatafeed import Interval
        interval = Interval.in_1_hour
    try:
        data = tv.get_hist(symbol=symbol, exchange=exchange, interval=interval, n_bars=bars)
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
    tv = init_tvDatafeed()
    if tv is None:
        return None
    return fetch_data_from_tv(tv, "BTC.D", "CRYPTOCAP", interval=interval, bars=limit)

# ===============================
# ІНДИКАТОРИ, ПАТЕРНИ, AI
# ===============================
def calculate_vwap(data):
    df = data.copy()
    df['cum_pv'] = (df['close'] * df['volume']).cumsum()
    df['cum_v'] = df['volume'].cumsum()
    df['vwap'] = df['cum_pv'] / df['cum_v']
    return df['vwap']

def calculate_obv(data):
    obv = [0]
    for i in range(1, len(data)):
        if data['close'].iloc[i] > data['close'].iloc[i-1]:
            obv.append(obv[-1] + data['volume'].iloc[i])
        elif data['close'].iloc[i] < data['close'].iloc[i-1]:
            obv.append(obv[-1] - data['volume'].iloc[i])
        else:
            obv.append(obv[-1])
    return pd.Series(obv, index=data.index)

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

        if body < 0.001 * data['close'].iloc[i]:
            patterns.append(("doji", data['time'].iloc[i]))
        if lower_wick > body * 2 and cl > op:
            patterns.append(("hammer", data['time'].iloc[i]))
        if i > 0:
            prev_op = data['open'].iloc[i-1]
            prev_cl = data['close'].iloc[i-1]
            if cl > prev_op and op < prev_cl and (cl - op) > abs(prev_cl - prev_op):
                patterns.append(("bullish_engulfing", data['time'].iloc[i]))
    return patterns

# Припустимо, ви хочете TF-модель (замість joblib).
# Якщо хочете scikit-learn, замініть цей блок на joblib.load("my_model.pkl").
# Перед main() додамо: import tensorflow as tf
# і тут завантаження моделі:

# У фіналі при запуску (див. if __name__...)
# model = tf.keras.models.load_model("mymodel.h5")

def ai_strategy(data):
    # Приклад, якщо ви використовуєте ту саму логіку features:
    latest_close = data['close'].iloc[-1]
    rsi_series = calculate_rsi(data)
    rsi_value = rsi_series.iloc[-1]
    macd_series, macd_signal = calculate_macd(data)
    macd_value = macd_series.iloc[-1]

    X = [latest_close, rsi_value, macd_value]
    # Припустімо, модель видає 1=buy, -1=sell, 0=none
    y_pred = model.predict([X])  # Якщо це TF-модель, можливо, .predict() видає масив ймовірностей
    # => тут потрібна ваша логіка інтерпретації

    # Спрощено, якщо ви отримали y_pred[0] = 1 => buy, -1 => sell...
    # (Потрібно адаптувати під вихід вашої реальної моделі)
    val = y_pred[0]
    if val == 1:
        return "buy"
    elif val == -1:
        return "sell"
    else:
        return None

def calculate_stochastic(data, period=14, d_period=3):
    low_min = data['low'].rolling(period).min()
    high_max = data['high'].rolling(period).max()
    k = 100 * (data['close'] - low_min) / (high_max - low_min)
    d = k.rolling(d_period).mean()
    return k, d

def calculate_parabolic_sar(data, af=0.02, af_max=0.2):
    median_price = (data['high'] + data['low'])/2
    sar = median_price.ewm(alpha=0.1).mean()
    return sar

def generate_signal(data_1h, data_4h=None):
    avg_volume = data_1h['volume'].rolling(20).mean().iloc[-1]
    current_vol = data_1h['volume'].iloc[-1]
    if current_vol < avg_volume * 0.5:
        return (None, "Занадто низький обсяг", None, None, None,
                None, None, None, None, None, None, None, None, None, None)

    macd, macd_signal = calculate_macd(data_1h)
    middle_band, upper_band, lower_band = calculate_bollinger_bands(data_1h)
    rsi = calculate_rsi(data_1h)
    atr = calculate_atr(data_1h)
    k, d = calculate_stochastic(data_1h)
    sar = calculate_parabolic_sar(data_1h)

    latest_close = data_1h['close'].iloc[-1]
    latest_macd = macd.iloc[-1]
    latest_macd_signal = macd_signal.iloc[-1]
    latest_rsi = rsi.iloc[-1]
    latest_upper_band = upper_band.iloc[-1]
    latest_lower_band = lower_band.iloc[-1]
    latest_atr = atr.iloc[-1]

    buy_signal = (
        (latest_macd > latest_macd_signal)
        and (latest_rsi < 50)
        and (latest_close <= latest_lower_band * 1.02)
    )
    sell_signal = (
        (latest_macd < latest_macd_signal)
        and (latest_rsi > 50)
        and (latest_close >= latest_upper_band * 0.98)
    )

    signal_type = None
    explanation = ""

    if buy_signal:
        signal_type = "buy"
        explanation = "Сигнал на купівлю"
    elif sell_signal:
        signal_type = "sell"
        explanation = "Сигнал на продаж"
    else:
        return (None, "Немає базового сигналу", None, None, None,
                None, None, None, None, None, None, None, None, None, None)

    if data_4h is not None and len(data_4h) > 10:
        big_tf_macd, big_tf_signal = calculate_macd(data_4h)
        if signal_type == "buy" and (big_tf_macd.iloc[-1] < big_tf_signal.iloc[-1]):
            return (None, "Старший ТФ не підтверджує buy", None, None, None,
                    None, None, None, None, None, None, None, None, None, None)
        if signal_type == "sell" and (big_tf_macd.iloc[-1] > big_tf_signal.iloc[-1]):
            return (None, "Старший ТФ не підтверджує sell", None, None, None,
                    None, None, None, None, None, None, None, None, None, None)

    if signal_type == "buy":
        entry = latest_close
        tp = round(entry + latest_atr * 2, 2)
        sl = round(entry - latest_atr * 1.5, 2)
    else:
        entry = latest_close
        tp = round(entry - latest_atr * 2, 2)
        sl = round(entry + latest_atr * 1.5, 2)

    return (signal_type, explanation, entry, tp, sl,
            macd, macd_signal, rsi,
            middle_band, upper_band, lower_band, atr,
            k, d, sar)

# ===============================
#  ЛОГІКА BTC & BTC.D
# ===============================
def get_trend(data, threshold=0.01):
    if data is None or len(data) < 6:
        return "flat"
    last_close = data['close'].iloc[-1]
    prev_close = data['close'].iloc[-5]
    diff = (last_close - prev_close) / prev_close
    if diff > threshold:
        return "rising"
    elif diff < -threshold:
        return "falling"
    else:
        return "flat"

def alt_signal_adjustment(btcd_trend, btc_trend):
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
    return "stable"

def adjust_final_signal(alt_signal, alt_trend_from_table):
    if alt_signal == "buy":
        if alt_trend_from_table in ["drop", "drop_strong"]:
            return None
    elif alt_signal == "sell":
        if alt_trend_from_table in ["rise", "rise_strong"]:
            return None
    return alt_signal

# ===============================
# 7. Автоматична перевірка
# ===============================
async def check_signals(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.chat_id
    try:
        await context.bot.send_message(chat_id=chat_id, text="🔄 Перевіряємо сигнали...")

        all_symbols = fetch_binance_symbols_futures()
        if not all_symbols:
            await context.bot.send_message(chat_id=chat_id, text="❌ Не отримано жодної пари з Binance.")
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

            df_1h = fetch_binance_futures_data(symbol, interval=BINANCE_INTERVAL)
            if df_1h is None or df_1h.empty:
                continue

            # (Опційно) Якщо хочемо 4h:
            # df_4h = fetch_binance_futures_data(symbol, interval="4h")
            # (sig_type, explanation, entry, tp, sl, macd, macd_signal, rsi,
            #  mb, ub, lb, atr, k, d, sar) = generate_signal(df_1h, df_4h)
            (sig_type, explanation, entry, tp, sl,
             macd, macd_signal, rsi,
             mb, ub, lb, atr,
             k, d, sar) = generate_signal(df_1h, None)

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
                chart = generate_chart(df_1h, macd, macd_signal, rsi,
                                       mb, ub, lb, atr, k, d, sar,
                                       entry, tp, sl)
                await context.bot.send_photo(chat_id=chat_id, photo=chart, caption=caption)

        if not found_any_signal:
            await context.bot.send_message(chat_id=chat_id, text="Немає сигналів на даний момент.")

    except Exception as e:
        await context.bot.send_message(chat_id=chat_id, text=f"❌ Помилка у check_signals: {e}")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "✅ Бот активовано!\n"
        "Сигнали будуть надсилатися кожні 15 хв. Можна вручну викликати /signal SYMBOL"
    )

    job_queue = context.application.job_queue
    if job_queue is None:
        print("❌ JobQueue не ініціалізовано.")
        return

    current_jobs = job_queue.get_jobs_by_name("check_signals")
    for job in current_jobs:
        job.schedule_removal()

    job_queue.run_repeating(
        callback=check_signals,
        interval=900,         # 15 хвилин
        first=10,
        name="check_signals",
        chat_id=update.effective_chat.id
    )
    print("✅ JobQueue успішно запущено. Сигнали перевірятимуться кожні 15 хвилин.")

async def signal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        args = context.args
        if len(args) < 1:
            await update.message.reply_text("Використання: /signal SYMBOL")
            return
        symbol = args[0]

        df = fetch_binance_futures_data(symbol, BINANCE_INTERVAL)
        if df is None or df.empty:
            await update.message.reply_text(f"❌ Даних немає для {symbol}.")
            return

        data_btc = fetch_binance_futures_data("BTCUSDT", BINANCE_INTERVAL)
        btc_tr = get_trend(data_btc)
        data_btcd = fetch_btc_dominance_tv()
        btcd_tr = get_trend(data_btcd)
        alts_outlook = alt_signal_adjustment(btcd_tr, btc_tr)

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
            await update.message.reply_text("Немає чіткого сигналу.")
    except Exception as e:
        await update.message.reply_text(f"❌ Помилка: {e}")

def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("signal", signal))

    print("✅ Бот запущено! Використовуйте /start або /signal SYMBOL")
    app.run_polling()

async def run_check_signals():
    try:
        chat_id = "542817935"  # Замінити на свій Chat ID
        bot = Bot(token=TELEGRAM_TOKEN)
        await bot.send_message(chat_id=chat_id, text="Запуск перевірки сигналів...")
        await check_signals()
        print("✅ Повідомлення успішно надіслано.")
    except Exception as e:
        print(f"❌ Помилка у run_check_signals: {e}")

if __name__ == "__main__":
    main()