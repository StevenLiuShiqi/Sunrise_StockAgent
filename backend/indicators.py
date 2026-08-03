"""技术指标计算：RSI / MACD / KDJ，纯函数，不依赖网络或全局状态。"""


def calc_rsi(close: "pd.Series", period: int = 14) -> float:
    delta    = close.diff()
    avg_gain = delta.clip(lower=0).rolling(period).mean().iloc[-1]
    avg_loss = (-delta.clip(upper=0)).rolling(period).mean().iloc[-1]
    if avg_loss == 0:
        return 100.0
    return float(round(100 - 100 / (1 + avg_gain / avg_loss), 1))


def rsi_signal(rsi: float) -> str:
    if rsi >= 80:   return f"RSI={rsi}，强超买"
    if rsi >= 70:   return f"RSI={rsi}，超买"
    if rsi <= 20:   return f"RSI={rsi}，强超卖"
    if rsi <= 30:   return f"RSI={rsi}，超卖"
    if rsi >= 50:   return f"RSI={rsi}，偏强"
    return               f"RSI={rsi}，偏弱"


def calc_macd(close: "pd.Series", fast=12, slow=26, signal=9) -> tuple[float, float, float]:
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    dif      = ema_fast - ema_slow
    dea      = dif.ewm(span=signal, adjust=False).mean()
    hist     = (dif - dea) * 2
    return round(float(dif.iloc[-1]), 4), round(float(dea.iloc[-1]), 4), round(float(hist.iloc[-1]), 4)


def macd_signal(dif: float, dea: float, hist: float, prev_hist: float) -> str:
    cross = "DIF上穿DEA金叉" if dif > dea else "DIF下穿DEA死叉"
    trend = "柱状图扩大" if abs(hist) > abs(prev_hist) else "柱状图收缩"
    zone  = "零轴上方" if dif > 0 else "零轴下方"
    return f"{cross}，{trend}，{zone}"


def calc_kdj(df: "pd.DataFrame", period: int = 9) -> tuple[float, float, float]:
    low_n  = df["最低"].rolling(period).min()
    high_n = df["最高"].rolling(period).max()
    rsv    = (df["收盘"] - low_n) / (high_n - low_n + 1e-8) * 100
    k      = rsv.ewm(com=2, adjust=False).mean()   # alpha=1/3
    d      = k.ewm(com=2, adjust=False).mean()
    j      = 3 * k - 2 * d
    return round(float(k.iloc[-1]), 1), round(float(d.iloc[-1]), 1), round(float(j.iloc[-1]), 1)


def kdj_signal(k: float, d: float, j: float, prev_k: float, prev_d: float) -> str:
    cross = ""
    if prev_k <= prev_d and k > d:
        cross = "K/D金叉，"
    elif prev_k >= prev_d and k < d:
        cross = "K/D死叉，"
    overbought = "J超买（>90）" if j > 90 else ("J超卖（<10）" if j < 10 else "")
    return f"{cross}K={k} D={d} J={j}" + (f"，{overbought}" if overbought else "")


def trend_position(close: "pd.Series", period_short: int = 60, period_long: int = 120) -> str:
    """现价相对 MA60/MA120 的位置 + MA60/MA120 多空排列，用于判断中期趋势。"""
    if len(close) < period_long + 1:
        return "历史数据不足，无法判断中期趋势"

    ma_short = close.rolling(period_short).mean()
    ma_long  = close.rolling(period_long).mean()

    last_price = float(close.iloc[-1])
    last_short, last_long = float(ma_short.iloc[-1]), float(ma_long.iloc[-1])
    prev_short, prev_long = float(ma_short.iloc[-2]), float(ma_long.iloc[-2])

    price_pos = f"MA{period_short}上方" if last_price > last_short else f"MA{period_short}下方"

    if prev_short <= prev_long and last_short > last_long:
        arrangement = f"MA{period_short}上穿MA{period_long}，中期转多头排列"
    elif prev_short >= prev_long and last_short < last_long:
        arrangement = f"MA{period_short}下穿MA{period_long}，中期转空头排列"
    elif last_short > last_long:
        arrangement = f"MA{period_short}/MA{period_long}维持多头排列"
    else:
        arrangement = f"MA{period_short}/MA{period_long}维持空头排列"

    return f"现价位于{price_pos}，{arrangement}"
