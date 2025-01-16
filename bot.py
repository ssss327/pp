import os
import requests
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from io import BytesIO
import sys
import datetime
import time
import asyncio
import json
import logging
import numpy as np

from telegram import Bot, Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, ContextTypes,
    JobQueue
)

# Налаштування логування
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

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
CHECK_INTERVAL_SEC = 3600        # 1 година
THRESHOLD_FLAT = 0.01

BINANCE_API_KEY = os.getenv("fo8MS8lNSI7YPkD2fcncjgyjHVoWMncXcS0xXY0fjKo7fmaFvnrtaXxmpKsGx3oQ")
BINANCE_API_SECRET = os.getenv("gDVNllBbJ7xxFyw2HajJeJ8uTMOKnVkkW0zSzANC380Mzkojnyr5WE3FE0aATKeV")

TV_USERNAME = os.getenv("uthhtu")
TV_PASSWORD = os.getenv("Berezynskyi2004")

ACCOUNT_BALANCE = float(os.getenv("ACCOUNT_BALANCE", "10000"))  # Ваш баланс у доларах
RISK_PER_TRADE = float(os.getenv("RISK_PER_TRADE", "0.01"))   # Ризикувати 1% від балансу

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
        logger.error(f"Помилка отримання списку Binance Futures: {e}")
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
        logger.error(f"❌ fetch_binance_futures_data({symbol}) помилка: {e}")
        return None

# ===============================
# 3. TradingView (tvDatafeed)
# ===============================
def init_tvDatafeed():
    """
    Ініціалізує tvDatafeed з логіном/паролем.
    """
    if TvDatafeed is None or Interval is None:
        logger.warning("tvDatafeed не імпортовано, встановіть бібліотеку.")
        return None
    try:
        tv = TvDatafeed(
            username=TV_USERNAME,
            password=TV_PASSWORD
        )
        return tv
    except Exception as e:
        logger.error(f"Помилка ініціалізації tvDatafeed: {e}")
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
            logger.warning(f"❌ Дані для {symbol} з TradingView не знайдені.")
            return None
        data.reset_index(inplace=True)
        data.rename(columns={"datetime": "time"}, inplace=True)
        df = data[["time", "open", "high", "low", "close", "volume"]].copy()
        df = df.sort_values("time")
        df.reset_index(drop=True, inplace=True)
        return df
    except Exception as e:
        logger.error(f"❌ fetch_data_from_tv({symbol}, {exchange}) помилка: {e}")
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
# 5. Індикатори (MACD, RSI, Bollinger, ATR, Stochastic, SAR) + патерни
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
    base_name = symbol.replace("USDT", "").replace("BUSD","").replace("USDC","").lower()
    url = f"https://api.coingecko.com/api/v3/search?query={base_name}"
    try:
        resp = requests.get(url)
        resp.raise_for_status()
        data = resp.json()
        coins = data.get("coins", [])
        if not coins:
            return None
        first = coins[0]
        return first["id"]  # наприклад, "apecoin"
    except:
        return None

def analyze_fundamental(symbol: str):
    coin_id = find_coingecko_id(symbol)
    if coin_id is None:
        return (None, f"Фундаментал: не вдалося знайти {symbol} на CoinGecko (пошук).")

    url = (
        f"https://api.coingecko.com/api/v3/coins/{coin_id}"
        "?localization=false&tickers=false&market_data=true&community_data=true&developer_data=true&sparkline=false"
    )
    try:
        resp = requests.get(url)
        resp.raise_for_status()
        data = resp.json()

        market_cap = data["market_data"]["market_cap"].get("usd", 0)
        dev_score = data.get("developer_score", 0)
        comm_score = data.get("community_score", 0)
        up_votes = data.get("sentiment_votes_up_percentage", 0)

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
# 8. Структурний аналіз (Wyckoff + Smart Money Concept + Institutional Candle Model)
# ===============================
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

    if (latest_close - latest_min)/latest_min < 0.005 and vol_current > 2 * vol_avg:
        return ("buy", "Wyckoff: Spring (Accumulation)")
    if (latest_max - latest_close)/latest_close < 0.005 and vol_current > 2 * vol_avg:
        return ("sell", "Wyckoff: Upthrust (Distribution)")

    return (None, "Wyckoff: немає Spring чи UT.")

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
        final_signal = None
    else:
        buys = signals.count("buy")
        sells = signals.count("sell")
        if buys > sells:
            final_signal = "buy"
        elif sells > buys:
            final_signal = "sell"
        else:
            final_signal = None

    explanation = (
        f"=== Wyckoff ===\n{wy_expl}\n\n"
        f"=== SMC ===\n{smc_expl}\n\n"
        f"=== ICM ===\n{icm_expl}"
    )
    return (final_signal, explanation)

# ===============================
# 9. Генерація сигналу (індикатори + фундаментал + кластер + структура)
# ===============================
def calculate_moving_average(data, window=50):
    return data['close'].rolling(window=window).mean()

def get_trend_long_term(data, window=200, threshold=0.02):
    if len(data) < window:
        return "flat"
    ma = calculate_moving_average(data, window)
    latest_close = data['close'].iloc[-1]
    latest_ma = ma.iloc[-1]
    prev_ma = ma.iloc[-window]
    diff = (latest_ma - prev_ma) / prev_ma
    if diff > threshold:
        return "uptrend"
    elif diff < -threshold:
        return "downtrend"
    else:
        return "flat"

def calculate_position_size(entry_price, sl_price):
    risk_amount = ACCOUNT_BALANCE * RISK_PER_TRADE
    risk_per_unit = abs(entry_price - sl_price)
    if risk_per_unit == 0:
        return 0
    position_size = risk_amount / risk_per_unit
    return position_size

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

    # Довгостроковий тренд
    long_term_trend = get_trend_long_term(data, window=200, threshold=0.02)

    # Фільтрація за трендом
    if long_term_trend == "uptrend":
        buy_condition = True
        sell_condition = False
    elif long_term_trend == "downtrend":
        buy_condition = False
        sell_condition = True
    else:
        buy_condition = sell_condition = True  # В боковому тренді дозволяємо обидва

    # --- ОРИГІНАЛЬНІ УМОВИ (ЗАКОМЕНТОВАНІ) ---
    #
    # trend_condition_buy = (latest_macd > latest_macd_signal and latest_macd > 0)
    # rsi_condition_buy = (latest_rsi < 30)
    # bollinger_condition_buy = (latest_close < lower_band.iloc[-1])
    #
    # trend_condition_sell = (latest_macd < latest_macd_signal and latest_macd < 0)
    # rsi_condition_sell = (latest_rsi > 70)
    # bollinger_condition_sell = (latest_close > upper_band.iloc[-1])
    #
    # volume_condition = (data['volume'].iloc[-1] > data['volume'].rolling(window=20).mean().iloc[-1] * 1.5)

    # --- ОНОВЛЕНІ УМОВИ ---
    # MACD: враховуємо різницю (macd_diff)
    macd_diff = latest_macd - latest_macd_signal

    # RSI: робимо екстремальнішою (25/75)
    rsi_condition_buy = (latest_rsi < 25)
    rsi_condition_sell = (latest_rsi > 75)

    # MACD «жорсткіший»: вимагаємо, щоб різниця була > 0.2 (або < -0.2 для sell)
    trend_condition_buy = (macd_diff > 0.2 and latest_macd > 0)
    trend_condition_sell = (macd_diff < -0.2 and latest_macd < 0)

    # Bollinger: відступ 1% від нижньої/верхньої межі
    bollinger_condition_buy = (latest_close < latest_lower_band * 0.99)
    bollinger_condition_sell = (latest_close > latest_upper_band * 1.01)

    # Volume: замість 1.5x робимо 2x
    volume_condition = (
        data['volume'].iloc[-1] > data['volume'].rolling(window=20).mean().iloc[-1] * 2
    )

    # Підсумкові buy/sell сигнали
    buy_signal = (trend_condition_buy
                  and rsi_condition_buy
                  and bollinger_condition_buy
                  and volume_condition
                  and buy_condition)

    sell_signal = (trend_condition_sell
                   and rsi_condition_sell
                   and bollinger_condition_sell
                   and volume_condition
                   and sell_condition)

    signal_type = None
    explanation = ""

    if buy_signal:
        signal_type = "buy"
        explanation = (
            f"Indicator-based (посилений): MACD різниця > 0.2, RSI < 25, " 
            f"ціна нижче нижньої Bollinger*0.99, обсяг > 2x середнього."
        )
        if 'hammer' in pattern_names or 'bullish_engulfing' in pattern_names:
            explanation += "\n+ Свічковий патерн (hammer/bullish_engulfing)."
    elif sell_signal:
        signal_type = "sell"
        explanation = (
            f"Indicator-based (посилений): MACD різниця < -0.2, RSI > 75, "
            f"ціна вище верхньої Bollinger*1.01, обсяг > 2x середнього."
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
        tp = round(entry + latest_atr * 2, 2)  # TP на 2 ATR вище
        sl = round(entry - latest_atr * 1.5, 2)  # SL на 1.5 ATR нижче
        position_size = calculate_position_size(entry, sl)
    elif signal_type == "sell":
        entry = latest_close
        tp = round(entry - latest_atr * 2, 2)  # TP на 2 ATR нижче
        sl = round(entry + latest_atr * 1.5, 2)  # SL на 1.5 ATR вище
        position_size = calculate_position_size(entry, sl)
    else:
        entry, tp, sl, position_size = None, None, None, None

    return (
        signal_type, explanation, entry, tp, sl, position_size,
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
    fig, axs = plt.subplots(5, 1, figsize=(12, 18), sharex=True,
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

    axs[0].set_title("Price + Bollinger Bands + SAR")
    axs[0].legend(loc='upper left')
    axs[0].grid(True)

    # MACD
    axs[1].plot(data['time'], macd, label="MACD", color='purple', linewidth=1.5)
    axs[1].plot(data['time'], macd_signal, label="Signal", color='magenta', linewidth=1)
    axs[1].axhline(y=0, color='gray', linestyle='--', linewidth=1)
    axs[1].set_title("MACD")
    axs[1].legend(loc='upper left')
    axs[1].grid(True)

    # RSI
    axs[2].plot(data['time'], rsi, label="RSI", color='brown', linewidth=1.5)
    axs[2].axhline(y=70, color='red', linestyle='--', linewidth=1)
    axs[2].axhline(y=30, color='green', linestyle='--', linewidth=1)
    axs[2].set_title("RSI")
    axs[2].legend(loc='upper left')
    axs[2].grid(True)

    # Stochastic
    axs[3].plot(data['time'], k, label="%K", color='blue', linewidth=1.5)
    axs[3].plot(data['time'], d, label="%D", color='orange', linewidth=1)
    axs[3].axhline(y=80, color='red', linestyle='--', linewidth=1)
    axs[3].axhline(y=20, color='green', linestyle='--', linewidth=1)
    axs[3].set_title("Stochastic")
    axs[3].legend(loc='upper left')
    axs[3].grid(True)

    # ATR
    axs[4].plot(data['time'], atr, label="ATR", color='black', linewidth=1.5)
    axs[4].set_title("ATR")
    axs[4].legend(loc='upper left')
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
def log_signal(symbol, interval, signal_type, entry, tp, sl, explanation, position_size):
    if not os.path.exists("signals_log.csv"):
        with open("signals_log.csv", "w") as f:
            f.write("timestamp,symbol,interval,signal_type,entry,tp,sl,position_size,explanation\n")
    with open("signals_log.csv", "a") as f:
        f.write(f"{datetime.datetime.utcnow()},{symbol},{interval},{signal_type},{entry},{tp},{sl},{position_size},{explanation}\n")

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

def adjust_final_signal(alt_signal, alts_outlook):
    if alt_signal == "buy":
        if alts_outlook in ["drop", "drop_strong"]:
            return None
    elif alt_signal == "sell":
        if alts_outlook in ["rise", "rise_strong"]:
            return None
    return alt_signal

# ===============================
# 12. Автоматична перевірка (JobQueue)
# ===============================
async def check_signals(context: ContextTypes.DEFAULT_TYPE):
    """
    Викликається JobQueue кожні N хв/год.
    """
    logger.info("Функція check_signals запущена")
    chat_id = context.job.chat_id

    try:
        await context.bot.send_message(chat_id=chat_id, text="🔄 Перевіряємо сигнали (індикатори + фундаментал + кластер + структура)...")

        all_symbols = fetch_binance_symbols_futures()
        if not all_symbols:
            await context.bot.send_message(chat_id=chat_id, text="❌ Не вдалося отримати список пар з Binance.")
            return

        data_btc = fetch_binance_futures_data("BTCUSDT", interval=BINANCE_INTERVAL)
        btc_trend = get_trend_long_term(data_btc)
        if btc_trend is None:
            btc_trend = "flat"

        data_btcd = fetch_btc_dominance_tv()
        btcd_trend = get_trend_long_term(data_btcd) if data_btcd is not None else "flat"

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
                (
                    _s_type, _ex,
                    entry, tp, sl, position_size,
                    macd, macd_signal, rsi,
                    mb, ub, lb, atr,
                    k, d, sar
                ) = ind_sig

                logger.debug(f"Сигнал для {symbol}: Type={adjusted_signal}, Entry={entry}, TP={tp}, SL={sl}, Position Size={position_size}")

                if entry is None or tp is None or sl is None or position_size is None:
                    logger.warning(f"Сигнал для {symbol} має невірні значення: entry={entry}, tp={tp}, sl={sl}, position_size={position_size}")
                    continue

                caption = (
                    f"АвтоСигнал для {symbol}:\n"
                    f"Тип: {adjusted_signal.upper()}\n"
                    f"Entry: {entry if entry is not None else 'N/A'}\n"
                    f"TP: {tp if tp is not None else 'N/A'}\n"
                    f"SL: {sl if sl is not None else 'N/A'}\n"
                    f"Position Size: {position_size:.4f}\n\n" if position_size is not None else "Position Size: N/A\n\n"
                    f"{final_explanation}\n"
                    f"BTC={btc_trend}, BTC.D={btcd_trend} => ALTS={alts_outlook}"
                )

                chart = generate_chart(df, macd, macd_signal, rsi, mb, ub, lb, atr, k, d, sar, entry, tp, sl)
                if chart:
                    await context.bot.send_photo(chat_id=chat_id, photo=chart, caption=caption)
                log_signal(symbol, BINANCE_INTERVAL, adjusted_signal, entry, tp, sl, final_explanation, position_size)

                found_any_signal = True

        if not found_any_signal:
            await context.bot.send_message(chat_id=chat_id, text="Немає сигналів на даний момент.")
    except Exception as e:
        logger.error(f"❌ Помилка у check_signals: {e}")
        await context.bot.send_message(chat_id=chat_id, text=f"❌ Помилка у перевірці сигналів: {e}")

# ===============================
# 13. Команди /START, /SIGNAL, /REPORT
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
        logger.error("❌ JobQueue не ініціалізовано. Перевірте залежності.")
        return

    current_jobs = job_queue.get_jobs_by_name("check_signals")
    for job in current_jobs:
        job.schedule_removal()

    chat_id = update.effective_chat.id
    job_queue.run_repeating(
        callback=check_signals,
        interval=CHECK_INTERVAL_SEC,  # 1 година
        first=10,
        name="check_signals",
        chat_id=chat_id
    )
    logger.info("✅ JobQueue успішно запущено. Сигнали будуть надсилатися кожну годину.")

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
                entry, tp, sl, position_size,
                macd, macd_signal, rsi,
                mb, ub, lb, atr,
                k, d, sar
            ) = ind_sig

            chart = generate_chart(df, macd, macd_signal, rsi, mb, ub, lb, atr, k, d, sar, entry, tp, sl)
            caption = (
                f"Сигнал для {symbol}:\n"
                f"Тип: {adjusted_signal.upper()}\n"
                f"Entry: {entry if entry is not None else 'N/A'}\n"
                f"TP: {tp if tp is not None else 'N/A'}\n"
                f"SL: {sl if sl is not None else 'N/A'}\n"
                f"Position Size: {position_size:.4f}\n\n" if position_size is not None else "Position Size: N/A\n\n"
                f"{final_explanation}\n"
                f"BTC={btc_tr}, BTC.D={btcd_tr} => ALTS={alts_outlook}"
            )
            if chart:
                await update.message.reply_photo(photo=chart, caption=caption)
            log_signal(symbol, BINANCE_INTERVAL, adjusted_signal, entry, tp, sl, final_explanation, position_size)
        else:
            await update.message.reply_text("Немає чіткого сигналу.\n\n" + final_explanation)
    except Exception as e:
        logger.error(f"❌ Помилка: {e}")
        await update.message.reply_text(f"❌ Помилка: {e}")

async def report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Команда: /report
    Генерує звіт по угодах.
    """
    report = generate_report()
    await update.message.reply_text(report, parse_mode='Markdown')

def generate_report():
    if not os.path.exists("signals_log.csv"):
        return "Немає логів для звіту."

    df = pd.read_csv("signals_log.csv", parse_dates=["timestamp"])
    total_trades = len(df)
    successful_trades = len(df[(df['signal_type'].isin(['buy', 'sell'])) &
                               ((df['tp'] > df['entry']) | (df['sl'] < df['entry']))])
    failed_trades = len(df[df['sl'] < df['entry']])

    profit = 0
    for _, row in df.iterrows():
        if row['signal_type'] == 'buy':
            if row['tp'] and row['sl']:
                profit += (row['tp'] - row['entry']) * row['position_size']
                profit -= (row['entry'] - row['sl']) * row['position_size']
        elif row['signal_type'] == 'sell':
            if row['tp'] and row['sl']:
                profit += (row['entry'] - row['tp']) * row['position_size']
                profit -= (row['sl'] - row['entry']) * row['position_size']

    report = (
        f"📊 **Звіт по Угодах** 📊\n"
        f"Загальна кількість угод: {total_trades}\n"
        f"Успішні угоди (TP > Entry або SL < Entry): {successful_trades}\n"
        f"Невдалі угоди (SL < Entry): {failed_trades}\n"
        f"Загальний прибуток: ${profit:.2f}\n"
    )
    return report

# ===============================
# 14. WebSocket для кластерного аналізу (CVD)
# ===============================
async def binance_combined_ws(symbols):
    import websockets
    stream_names = [f"{sym.lower()}@aggTrade" for sym in symbols]
    streams = "/".join(stream_names)
    url = f"wss://fstream.binance.com/stream?streams={streams}"

    try:
        async with websockets.connect(url) as ws:
            logger.info(f"🔗 Combined WebSocket підключено для: {', '.join(symbols)}")
            async for msg in ws:
                data = json.loads(msg)
                stream = data['stream']
                payload = data['data']
                symbol = payload['s']
                trade = {
                    "price": float(payload["p"]),
                    "qty": float(payload["q"]),
                    "isBuyerMaker": payload["m"],
                    "timestamp": payload["T"]
                }
                if symbol not in global_aggtrades:
                    global_aggtrades[symbol] = []
                global_aggtrades[symbol].append(trade)

                # Очищаємо трейди старіші за 1 годину
                cutoff = time.time()*1000 - (60*60*1000)
                global_aggtrades[symbol] = [
                    t for t in global_aggtrades[symbol]
                    if t["timestamp"] >= cutoff
                ]
    except Exception as e:
        logger.error(f"❌ Помилка у Combined WebSocket: {e}")
        await asyncio.sleep(5)
        await binance_combined_ws(symbols)

# ===============================
# 15. Головна функція
# ===============================
def init_db():
    logger.info("Database initialized.")
    pass

def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("signal", signal))
    app.add_handler(CommandHandler("report", report))

    if len(sys.argv) > 1 and sys.argv[1] == "check_signals":
        logger.info("🔄 Запускаємо check_signals через Scheduler")
        asyncio.run(run_check_signals())
    else:
        logger.info("✅ Бот запущено! Використовуйте /start")

        if websockets_installed:
            loop = asyncio.get_event_loop()
            symbols_to_track = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "MATICUSDT"]
            loop.create_task(binance_combined_ws(symbols_to_track))

        app.run_polling()


async def run_check_signals():
    try:
        chat_id = "542817935"  # ваш chat_id
        bot = Bot(token=TELEGRAM_TOKEN)
        await bot.send_message(chat_id=chat_id, text="Запуск перевірки сигналів...")
        logger.info("✅ Повідомлення успішно надіслано.")

        # Тепер викликаємо check_signals
        class FakeContext:
            def __init__(self, bot, chat_id):
                self.bot = bot
                self.job = self
                self.chat_id = chat_id

        # Створюємо об'єкт, який імітує context
        fake_context = FakeContext(bot, chat_id)
        await check_signals(fake_context)

    except Exception as e:
        logger.error(f"❌ Помилка у run_check_signals: {e}")

if __name__ == "__main__":
    main()