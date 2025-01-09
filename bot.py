import requests
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from io import BytesIO
import sys
import datetime
import os
import time
import asyncio
import json

from telegram import Bot, Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, ContextTypes,
    JobQueue
)

# Якщо хочемо tvdatafeed
try:
    from tvDatafeed import TvDatafeed, Interval
    tvdata_installed = True
except ImportError:
    TvDatafeed = None
    Interval = None
    tvdata_installed = False

# WebSocket для реального кластерного аналізу
try:
    import websockets
    websockets_installed = True
except ImportError:
    websockets_installed = False

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
tv_symbol_map = {
    # "APTUSDT": ("APTUSDT","HUOBI"),
    # ...
}

# Для фундаментального аналізу (CoinGecko) — “BTCUSDT” -> “bitcoin”, ...
coingecko_map = {
    "BTCUSDT": "bitcoin",
    "ETHUSDT": "ethereum",
    "BNBUSDT": "binancecoin",
    "SOLUSDT": "solana",
    "ADAUSDT": "cardano",
    "MATICUSDT": "matic-network",
    # Додайте свої пари
}

# ===============================
# Глобальна змінна для WebSocket кластерного аналізу
# ===============================
global_aggtrades = {}
# Структура: {
#    "BTCUSDT": [ {price, qty, isBuyerMaker, timestamp}, ... ],
#    ...
# }

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
# 2. Отримання історії з Binance (Klines)
# ===============================
def fetch_binance_futures_data(symbol: str, interval: str = "1h", limit: int = 200):
    """
    GET /fapi/v1/klines (Binance Futures)
    """
    url = (
        "https://fapi.binance.com/fapi/v1/klines"
        f"?symbol={symbol}&interval={interval}&limit={limit}"
    )
    headers = {
        "X-MBX-APIKEY": BINANCE_API_KEY
    }
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
    """
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
    if interval is None and tvdata_installed:
        interval = Interval.in_1_hour
    else:
        interval = None  # Якщо немає tvdatafeed

    if not tvdata_installed:
        return None
    tv = init_tvDatafeed()
    if tv is None:
        return None
    return fetch_data_from_tv(tv, "BTC.D", "CRYPTOCAP", interval=interval, bars=limit)

# ===============================
# 5. Індикатори (MACD, RSI, Bollinger, ATR, Stoх, SAR) + патерни
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

def calculate_stochastic(data, period=14, d_period=3):
    low_min = data['low'].rolling(period).min()
    high_max = data['high'].rolling(period).max()
    k = 100 * (data['close'] - low_min) / (high_max - low_min)
    d = k.rolling(d_period).mean()
    return k, d

def calculate_parabolic_sar(data, af=0.02, af_max=0.2):
    # Спрощена (демо) версія SAR
    median_price = (data['high'] + data['low']) / 2
    sar = median_price.ewm(alpha=0.1).mean()
    return sar

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

# ===============================
# 6. Фундаментальний аналіз (CoinGecko)
# ===============================
def find_coingecko_id(symbol: str):
    """
    Пошук на CoinGecko:
    Наприклад, якщо symbol="APEUSDT", то шукаємо "APE" чи "apecoin" тощо.
    """
    # 1) Видаляємо "USDT" з кінця, аби отримати "APE"
    base_name = symbol.replace("USDT", "").lower()
    url = f"https://api.coingecko.com/api/v3/search?query={base_name}"
    try:
        resp = requests.get(url)
        resp.raise_for_status()
        data = resp.json()
        coins = data.get("coins", [])
        if not coins:
            return None
        # Беремо перший збіг
        # coins[i] виглядає як {"id": "apecoin", "symbol":"ape", "name":"ApeCoin", ...}
        first = coins[0]
        return first["id"]  # тобто "apecoin"
    except:
        return None

def analyze_fundamental(symbol: str):
    # 1) Пробуємо знайти coin_id через пошук
    coin_id = find_coingecko_id(symbol)
    if coin_id is None:
        return (None, f"Фундаментал: не вдалося знайти {symbol} на CoinGecko (пошук).")

    url = (
        f"https://api.coingecko.com/api/v3/coins/{coin_id}"
        "?localization=false&tickers=false&market_data=true&community_data=true&developer_data=true&sparkline=false"
    )
    # ... далі як завжди
    try:
        resp = requests.get(url)
        resp.raise_for_status()
        data = resp.json()

        market_cap = data["market_data"]["market_cap"].get("usd", 0)
        dev_score = data.get("developer_score", 0)
        comm_score = data.get("community_score", 0)
        up_votes = data.get("sentiment_votes_up_percentage", 0)
        # ... можна аналізувати більше полів

        explanation = f"[CoinGecko] MarketCap=${market_cap}, DevScore={dev_score}, CommScore={comm_score}, UpVotes={up_votes}%"

        if market_cap > 1e10 and dev_score > 50 and up_votes > 70:
            return ("buy", explanation + "\nПозитивні показники (велика капа, хороший dev, +sentiment).")
        elif market_cap < 3e8 or dev_score < 5:
            return ("sell", explanation + "\nДуже низька капа або активність розробників — ризик.")
        else:
            return (None, explanation + "\nНейтральний фундамент.")
    except Exception as e:
        return (None, f"Фундаментал: помилка CoinGecko => {e}")

# ===============================
# 7. Кластерний аналіз через WebSocket (CVD)
# ===============================
def compute_cvd(trades_list):
    cvd = 0
    buy_vol = 0
    sell_vol = 0
    for t in trades_list:
        qty = t["qty"]
        is_maker = t["isBuyerMaker"]
        if not is_maker:
            # агресивний покупець
            cvd += qty
            buy_vol += qty
        else:
            cvd -= qty
            sell_vol += qty
    return cvd, buy_vol, sell_vol

def analyze_cluster(symbol: str):
    """
    Використовуємо глобальний список трейдів global_aggtrades[symbol],
    рахуємо CVD за останні 15 хв.
    """
    trades = global_aggtrades.get(symbol, [])
    if len(trades) < 10:
        return (None, f"Кластер: Недостатньо trades у пам'яті для {symbol}.")

    now_ts = time.time() * 1000
    filtered = [t for t in trades if (now_ts - t["timestamp"]) < (15 * 60 * 1000)]
    if not filtered:
        return (None, f"Кластер: За останні 15 хв немає trades для {symbol}.")

    cvd, buy_vol, sell_vol = compute_cvd(filtered)
    explanation = f"CVD={cvd:.2f}, buyVol={buy_vol:.2f}, sellVol={sell_vol:.2f} (15min)"

    if cvd > 0 and buy_vol > sell_vol * 1.5:
        return ("buy", explanation + "\nЯвна перевага покупців.")
    elif cvd < 0 and sell_vol > buy_vol * 1.5:
        return ("sell", explanation + "\nЯвна перевага продавців.")
    else:
        return (None, explanation + "\nНемає однозначного перекосу.")

# ===============================
# 8. Структурний аналіз (Wyckoff + SM + ICM)
# ===============================

# -- 8.1 Wyckoff --
def analyze_wyckoff(data):
    if data is None or len(data) < 50:
        return (None, "Wyckoff: замало даних.")
    df = data.copy()
    window = 30
    df["rolling_min"] = df["close"].rolling(window=window).min()
    df["rolling_max"] = df["close"].rolling(window=window).max()

    latest_close = df["close"].iloc[-1]
    latest_min   = df["rolling_min"].iloc[-1]
    latest_max   = df["rolling_max"].iloc[-1]
    vol_current  = df["volume"].iloc[-1]
    vol_avg      = df["volume"].iloc[-window:].mean()

    # Приклад:
    if (latest_close - latest_min)/latest_min < 0.005 and vol_current > 2 * vol_avg:
        return ("buy", "Wyckoff: Spring (Accumulation)")
    if (latest_max - latest_close)/latest_close < 0.005 and vol_current > 2 * vol_avg:
        return ("sell", "Wyckoff: Upthrust (Distribution)")

    return (None, "Wyckoff: немає Spring чи UT.")

# -- 8.2 SM (Smart Money Concept) --
def analyze_smc(data):
    if data is None or len(data) < 30:
        return (None, "SMC: мало даних.")
    lookback = 10
    df = data.copy()
    df["swing_high"] = df["high"].rolling(window=lookback).max()
    df["swing_low"]  = df["low"].rolling(window=lookback).min()

    last_close = df["close"].iloc[-1]
    last_high  = df["swing_high"].iloc[-2]
    last_low   = df["swing_low"].iloc[-2]

    bos_up   = (last_close > last_high*1.001)
    bos_down = (last_close < last_low*0.999)

    if bos_up:
        return ("buy", f"SMC: BOS up (пробили swing high={last_high:.2f}).")
    elif bos_down:
        return ("sell", f"SMC: BOS down (пробили swing low={last_low:.2f}).")
    else:
        return (None, "SMC: без BOS.")

# -- 8.3 ICM (Institutional Candle Model) --
def analyze_icm(data):
    if data is None or len(data) < 5:
        return (None, "ICM: мало даних.")
    df = data.copy()
    c1 = df.iloc[-2]
    c2 = df.iloc[-1]

    inside_bar = (c2["high"] < c1["high"]) and (c2["low"] > c1["low"])
    df["candle_body"] = (df["close"] - df["open"]).abs()
    body_avg = df["candle_body"].iloc[-5:].mean()
    c2_body = abs(c2["close"] - c2["open"])
    big_candle = (c2_body > 1.5 * body_avg)

    if inside_bar and big_candle:
        if c2["close"] > c2["open"]:
            return ("buy", "ICM: Inside Bar + Big Bullish Candle.")
        else:
            return ("sell", "ICM: Inside Bar + Big Bearish Candle.")

    return (None, "ICM: не знайдено inside_bar + big_candle.")

def analyze_structural(data):
    wy_sig, wy_expl = analyze_wyckoff(data)
    smc_sig, smc_expl = analyze_smc(data)
    icm_sig, icm_expl = analyze_icm(data)

    signals = []
    if wy_sig is not None:
        signals.append(wy_sig)
    if smc_sig is not None:
        signals.append(smc_sig)
    if icm_sig is not None:
        signals.append(icm_sig)

    if "buy" in signals and "sell" in signals:
        final = None
    else:
        buys = signals.count("buy")
        sells = signals.count("sell")
        if buys > sells:
            final = "buy"
        elif sells > buys:
            final = "sell"
        else:
            final = None

    explanation = (
        f"=== Wyckoff ===\n{wy_expl}\n\n"
        f"=== SMC ===\n{smc_expl}\n\n"
        f"=== ICM ===\n{icm_expl}"
    )
    return (final, explanation)

# ===============================
# 9. Генерація сигналу (індикатори + фундаментал + кластер + структура)
# ===============================
def generate_indicator_signal(data):
    macd, macd_signal = calculate_macd(data)
    middle_band, upper_band, lower_band = calculate_bollinger_bands(data)
    rsi = calculate_rsi(data)
    atr = calculate_atr(data)
    k, d = calculate_stochastic(data)
    sar = calculate_parabolic_sar(data)
    patterns = identify_candle_patterns(data)

    latest_close = data['close'].iloc[-1]
    latest_macd = macd.iloc[-1]
    latest_macd_signal = macd_signal.iloc[-1]
    latest_rsi = rsi.iloc[-1]
    latest_upper_band = upper_band.iloc[-1]
    latest_lower_band = lower_band.iloc[-1]
    latest_atr = atr.iloc[-1]
    pattern_names = [p[0] for p in patterns]

    buy_signal = (
        (latest_macd > latest_macd_signal) and
        (latest_rsi < 50) and
        (latest_close <= latest_lower_band * 1.02)
    )
    sell_signal = (
        (latest_macd < latest_macd_signal) and
        (latest_rsi > 50) and
        (latest_close >= latest_upper_band * 0.98)
    )

    signal_type = None
    explanation = ""

    if buy_signal:
        signal_type = "buy"
        explanation = (
            f"Indicator-based: MACD bullish, RSI<50, close near lower Bollinger ({latest_lower_band:.2f})."
        )
        if 'hammer' in pattern_names or 'bullish_engulfing' in pattern_names:
            explanation += "\n+ Свічковий патерн (hammer/bullish_engulfing)."
    elif sell_signal:
        signal_type = "sell"
        explanation = (
            f"Indicator-based: MACD bearish, RSI>50, close near upper Bollinger ({latest_upper_band:.2f})."
        )
        if 'doji' in pattern_names:
            explanation += "\n+ Doji => невизначеність."
    else:
        if 'hammer' in pattern_names and latest_close <= latest_lower_band * 1.02:
            explanation = "No clear indicator signal, але hammer біля нижньої Bollinger."
        elif 'bullish_engulfing' in pattern_names and latest_close <= latest_lower_band * 1.02:
            explanation = "No clear indicator signal, але bullish engulfing біля нижньої Bollinger."
        elif 'doji' in pattern_names:
            explanation = "No clear indicator signal, але Doji."

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

    return (
        signal_type, explanation, entry, tp, sl,
        macd, macd_signal, rsi,
        middle_band, upper_band, lower_band, atr,
        k, d, sar
    )

def combine_signals(
    indicator_sig,
    fundamental_sig,
    cluster_sig,
    structural_sig
):
    sigs = []
    ind_type = indicator_sig[0]
    ind_expl = indicator_sig[1]

    f_type, f_expl = fundamental_sig
    c_type, c_expl = cluster_sig
    s_type, s_expl = structural_sig

    if ind_type is not None:
        sigs.append(ind_type)
    if f_type is not None:
        sigs.append(f_type)
    if c_type is not None:
        sigs.append(c_type)
    if s_type is not None:
        sigs.append(s_type)

    if "buy" in sigs and "sell" in sigs:
        final_signal = None
    else:
        buys = sigs.count("buy")
        sells = sigs.count("sell")
        if buys > sells:
            final_signal = "buy"
        elif sells > buys:
            final_signal = "sell"
        else:
            final_signal = None

    full_expl = (
        "===== Indicators =====\n" + ind_expl + "\n\n"
        "===== Fundamental =====\n" + f_expl + "\n\n"
        "===== Cluster =====\n" + c_expl + "\n\n"
        "===== Structural (Wyckoff + SM + ICM) =====\n" + s_expl
    )
    return final_signal, full_expl

def generate_signal(symbol, data):
    indicator_sig = generate_indicator_signal(data)
    fundamental_sig = analyze_fundamental(symbol)
    cluster_sig = analyze_cluster(symbol)
    structural_sig = analyze_structural(data)

    final_signal, explanation = combine_signals(
        indicator_sig,
        fundamental_sig,
        cluster_sig,
        structural_sig
    )
    return final_signal, explanation, indicator_sig

# ===============================
# 10. Побудова графіка
# ===============================
def generate_chart(
    data, macd, macd_signal, rsi,
    middle_band, upper_band, lower_band,
    atr, k, d, sar,
    entry=None, tp=None, sl=None
):
    if data is None or data.empty:
        return None

    plt.rcParams.update({'font.size': 10})
    plt.rcParams['axes.titlesize'] = 12
    fig, axs = plt.subplots(5, 1, figsize=(12, 12), sharex=True,
                            gridspec_kw={'height_ratios': [3,1,1,1,1]})

    # Ціна + Bollinger + SAR
    axs[0].plot(data['time'], data['close'], label='Ціна', color='blue', linewidth=1.5)
    axs[0].plot(data['time'], middle_band, label='SMA 20', color='orange', linewidth=1)
    axs[0].plot(data['time'], upper_band, label='Upper', linestyle='dotted', color='green', linewidth=1)
    axs[0].plot(data['time'], lower_band, label='Lower', linestyle='dotted', color='red', linewidth=1)

    if entry is not None:
        axs[0].axhline(y=entry, color='yellow', linestyle='--', linewidth=1, label=f"Entry={entry}")
    if tp is not None:
        axs[0].axhline(y=tp, color='green', linestyle='--', linewidth=1, label=f"TP={tp}")
    if sl is not None:
        axs[0].axhline(y=sl, color='red', linestyle='--', linewidth=1, label=f"SL={sl}")

    for i in range(len(data)):
        if sar.iloc[i] < data['close'].iloc[i]:
            axs[0].plot(data['time'].iloc[i], sar.iloc[i], marker='.', color='green')
        else:
            axs[0].plot(data['time'].iloc[i], sar.iloc[i], marker='.', color='red')

    axs[0].set_title("Price + Bollinger + SAR")
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
# Лог сигналу
# ===============================
def log_signal(symbol, interval, signal_type, entry, tp, sl, explanation):
    if not os.path.exists("signals_log.csv"):
        with open("signals_log.csv", "w") as f:
            f.write("timestamp,symbol,interval,signal_type,entry,tp,sl,explanation\n")
    with open("signals_log.csv", "a") as f:
        f.write(f"{datetime.datetime.utcnow()},{symbol},{interval},{signal_type},{entry},{tp},{sl},{explanation}\n")

# ===============================
# 11. Логіка BTC + BTC.D
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
# 12. Автоматична перевірка (JobQueue)
# ===============================
async def check_signals(context: ContextTypes.DEFAULT_TYPE):
    """
    Викликається JobQueue кожні N хв/год.
    """
    print("Функція check_signals запущена")
    chat_id = context.job.chat_id

    try:
        await context.bot.send_message(chat_id=chat_id, text="🔄 Перевіряємо сигнали (індикатори + фундаментал + кластер + структура)...")

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

            final_signal, final_explanation, ind_sig = generate_signal(symbol, df)
            adjusted_signal = adjust_final_signal(final_signal, alts_outlook)

            if adjusted_signal is not None:
                found_any_signal = True

                (
                    _s_type, _ex,
                    entry, tp, sl,
                    macd, macd_signal, rsi,
                    mb, ub, lb, atr,
                    k, d, sar
                ) = ind_sig

                caption = (
                    f"АвтоСигнал для {symbol}:\n"
                    f"Тип: {adjusted_signal.upper()}\n"
                    f"Entry: {entry}\n"
                    f"TP: {tp}\n"
                    f"SL: {sl}\n\n"
                    f"{final_explanation}\n"
                    f"BTC={btc_trend}, BTC.D={btcd_trend} => ALTS={alts_outlook}"
                )
                chart = generate_chart(df, macd, macd_signal, rsi, mb, ub, lb, atr, k, d, sar, entry, tp, sl)
                await context.bot.send_photo(chat_id=chat_id, photo=chart, caption=caption)
                log_signal(symbol, BINANCE_INTERVAL, adjusted_signal, entry, tp, sl, final_explanation)

        if not found_any_signal:
            await context.bot.send_message(chat_id=chat_id, text="Немає сигналів на даний момент.")
    except Exception as e:
        print(f"❌ Помилка у check_signals: {e}")
        await context.bot.send_message(chat_id=chat_id, text=f"❌ Помилка у перевірці сигналів: {e}")

# ===============================
# 13. /START і /SIGNAL
# ===============================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Запускає перевірку сигналів кожні X хв/год.
    """
    await update.message.reply_text(
        "✅ Бот активовано!\n"
        "Сигнали будуть надсилатися кожну годину."
    )

    job_queue = context.application.job_queue
    if job_queue is None:
        print("❌ JobQueue не ініціалізовано. Перевірте залежності.")
        return

    current_jobs = job_queue.get_jobs_by_name("check_signals")
    for job in current_jobs:
        job.schedule_removal()

    job_queue.run_repeating(
        callback=check_signals,
        interval=3600,  # 1 година
        first=10,
        name="check_signals",
        chat_id=update.effective_chat.id
    )
    print("✅ JobQueue успішно запущено. Сигнали будуть надсилатися кожну годину.")

async def signal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Вручну: /signal SYMBOL
    """
    try:
        args = context.args
        if len(args) < 1:
            await update.message.reply_text("Використання: /signal SYMBOL (наприклад: /signal ETHUSDT)")
            return
        symbol = args[0].upper()

        tv = init_tvDatafeed()
        data_btc = fetch_binance_futures_data("BTCUSDT", BINANCE_INTERVAL)
        btc_tr = get_trend(data_btc)

        data_btcd = fetch_btc_dominance_tv(200, Interval.in_1_hour) if tv and tvdata_installed else None
        btcd_tr = get_trend(data_btcd)
        alts_outlook = alt_signal_adjustment(btcd_tr, btc_tr)

        df = fetch_binance_futures_data(symbol, BINANCE_INTERVAL)
        if (df is None or df.empty) and tv:
            if symbol in tv_symbol_map:
                tv_sym, tv_exch = tv_symbol_map[symbol]
                df = fetch_data_from_tv(tv, tv_sym, tv_exch, interval=Interval.in_1_hour)
            else:
                await update.message.reply_text(f"❌ Немає даних для {symbol} на BinanceFutures і без мапи tv.")
                return

        if df is None or df.empty:
            await update.message.reply_text(f"❌ Даних немає для {symbol}.")
            return

        final_signal, final_explanation, ind_sig = generate_signal(symbol, df)
        adjusted_signal = adjust_final_signal(final_signal, alts_outlook)

        if adjusted_signal is not None:
            (
                _s_type, _expl,
                entry, tp, sl,
                macd, macd_signal, rsi,
                mb, ub, lb, atr,
                k, d, sar
            ) = ind_sig

            chart = generate_chart(df, macd, macd_signal, rsi, mb, ub, lb, atr, k, d, sar, entry, tp, sl)
            caption = (
                f"Сигнал для {symbol}:\n"
                f"Тип: {adjusted_signal.upper()}\n"
                f"Entry: {entry}\n"
                f"TP: {tp}\n"
                f"SL: {sl}\n\n"
                f"{final_explanation}\n"
                f"BTC={btc_tr}, BTC.D={btcd_tr} => ALTS={alts_outlook}"
            )
            await update.message.reply_photo(photo=chart, caption=caption)
            log_signal(symbol, BINANCE_INTERVAL, adjusted_signal, entry, tp, sl, final_explanation)
        else:
            await update.message.reply_text("Немає чіткого сигналу.\n\n" + final_explanation)
    except Exception as e:
        await update.message.reply_text(f"❌ Помилка: {e}")

# ===============================
# 14. WebSocket для кластерного аналізу (CVD)
# ===============================
async def binance_aggtrade_ws(symbol="btcusdt"):
    """
    Вебсокет для fstream.binance.com/ws/btcusdt@aggTrade,
    Зберігаємо результати у global_aggtrades[symbol].
    """
    import websockets
    url = f"wss://fstream.binance.com/ws/{symbol.lower()}@aggTrade"
    async with websockets.connect(url) as ws:
        print(f"🔗 WebSocket підключено: {symbol}")
        async for msg in ws:
            data = json.loads(msg)
            price = float(data["p"])
            qty = float(data["q"])
            is_maker = data["m"]
            ts = data["T"]
            trade = {
                "price": price,
                "qty": qty,
                "isBuyerMaker": is_maker,
                "timestamp": ts
            }
            if symbol.upper() not in global_aggtrades:
                global_aggtrades[symbol.upper()] = []
            global_aggtrades[symbol.upper()].append(trade)

            # Видаляємо надто старі (понад 1 годину)
            cutoff = time.time()*1000 - (60*60*1000)
            global_aggtrades[symbol.upper()] = [
                t for t in global_aggtrades[symbol.upper()]
                if t["timestamp"] >= cutoff
            ]

# ===============================
# 15. Головна функція
# ===============================
def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("signal", signal))

    if len(sys.argv) > 1 and sys.argv[1] == "check_signals":
        print("🔄 Запускаємо check_signals через Scheduler")
        asyncio.run(run_check_signals())
    else:
        print("✅ Бот запущено! Використовуйте /start")

        # Якщо хочемо запустити кластерний потік (для BTC, ETH тощо):
        if websockets_installed:
            loop = asyncio.get_event_loop()
            # Запускаємо для BTCUSDT:
            loop.create_task(binance_aggtrade_ws("btcusdt"))
            # Можна додати й інші символи, якщо потрібно
            # loop.create_task(binance_aggtrade_ws("ethusdt"))

        app.run_polling()

async def run_check_signals():
    """
    Якщо викликається напряму з Heroku Scheduler чи іншого планувальника.
    """
    try:
        chat_id = "542817935"
        bot = Bot(token=TELEGRAM_TOKEN)
        await bot.send_message(chat_id=chat_id, text="Запуск перевірки сигналів...")
        print("✅ Повідомлення успішно надіслано.")
        # Тут можна викликати check_signals() "у ручному режимі", але
        # для цього треба контекст job. Можна написати mock.
    except Exception as e:
        print(f"❌ Помилка у run_check_signals: {e}")

if __name__ == "__main__":
    main()