"""实时估值（腾讯财经）+ 历史行情（BaoStock）+ 本地技术分析。"""

from datetime import datetime, timedelta

from . import config  # noqa: F401  先清代理环境变量，再 import baostock

import pandas as pd
import requests as _req
import baostock as bs
import baostock.common.context as _bs_context

from .indicators import (
    calc_rsi, rsi_signal,
    calc_macd, macd_signal,
    calc_kdj, kdj_signal,
    trend_position,
)
from .progress import emit
from .risk_metrics import annualized_volatility, max_drawdown_pct, risk_signal
from .valuation_history import fetch_valuation_percentile

# 250 交易日 ≈ 一年，日历天数要留足周末/节假日的余量，否则拿不到 250 根实际K线。
HISTORY_WINDOW_DAYS = 400

# BaoStock 用自己的 socket 协议，不走 HTTP/代理，登录一次全局复用。
# 但长期常驻进程（如 Railway 部署）中，这条 TCP 连接可能被对端或网络中间设备
# 空闲断开，baostock 本身不会自动重连，因此这里查询失败时重新登录一次再试。
# 另外 baostock 底层 socket 默认没有超时（无论是 connect 还是 recv），
# 一旦云端网络把这条连接静默丢包（连接看似建立、但对方永不回包，常见于
# 跨境访问被中间设备拦截的场景），recv() 会永久阻塞，导致整个请求卡死。
# 因此这里显式给 socket 设置超时，把"无限期卡住"转成"超时后走重试/降级"。
_BS_SOCKET_TIMEOUT_SEC = 15


def _bs_set_socket_timeout() -> None:
    sock_obj = getattr(_bs_context, "default_socket", None)
    if sock_obj is not None:
        sock_obj.settimeout(_BS_SOCKET_TIMEOUT_SEC)


_bs_login = bs.login()
_bs_set_socket_timeout()


def _bs_query_history_with_retry(*args, **kwargs):
    """带重连和超时保护的 BaoStock 历史行情查询：底层 socket 断开或长时间无响应时重新登录后重试一次。"""
    rs = bs.query_history_k_data_plus(*args, **kwargs)
    if rs is None or getattr(rs, "error_code", "0") != "0":
        bs.logout()
        bs.login()
        _bs_set_socket_timeout()
        rs = bs.query_history_k_data_plus(*args, **kwargs)
    return rs


def fetch_valuation(ticker: str) -> dict:
    """腾讯财经实时行情：PE/PB/市值。抠自 simonlin1212/a-stock-data。"""
    try:
        prefix = "sh" if ticker.startswith(("6", "9")) else ("bj" if ticker.startswith("8") else "sz")
        r = _req.get(
            f"https://qt.gtimg.cn/q={prefix}{ticker}",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=8,
            proxies={},
        )
        vals = r.content.decode("gbk").split('"')[1].split("~")
        return {
            "price":    float(vals[3])  if vals[3]  else 0,
            "pe_ttm":   float(vals[39]) if vals[39] else 0,
            "pb":       float(vals[46]) if vals[46] else 0,
            "mcap_yi":  float(vals[44]) if vals[44] else 0,
        }
    except Exception as e:
        emit(f"⚠️ {ticker} 估值拉取失败（{type(e).__name__}），跳过", ticker=ticker)
        return {"price": 0, "pe_ttm": 0, "pb": 0, "mcap_yi": 0}


def analyze_stock(h: dict) -> dict:
    item   = dict(h)
    ticker = h["ticker"]
    name   = h["name"]

    emit(f"💹 拉取 {name}（{ticker}）实时价格…", ticker=ticker)
    valuation = fetch_valuation(ticker)
    item.update({
        "price":   valuation["price"],
        "mcap_yi": valuation["mcap_yi"],
        "pe_ttm":  valuation["pe_ttm"],   # 占位，BaoStock 成功后会覆盖
        "pb":      valuation["pb"],
    })
    if valuation["price"]:
        emit(f"  当前价={valuation['price']}  市值={valuation['mcap_yi']:.0f}亿", ticker=ticker)

    emit(f"📡 拉取 {name}（{ticker}）历史行情（BaoStock）…", ticker=ticker)

    try:
        prefix = "sh" if ticker.startswith(("6", "9")) else ("bj" if ticker.startswith("8") else "sz")
        end    = datetime.now().strftime("%Y-%m-%d")
        start  = (datetime.now() - timedelta(days=HISTORY_WINDOW_DAYS)).strftime("%Y-%m-%d")

        rs = _bs_query_history_with_retry(
            f"{prefix}.{ticker}",
            "date,close,high,low,volume,turn,peTTM,pbMRQ",
            start_date=start, end_date=end,
            frequency="d", adjustflag="2",
        )
        raw = rs.get_data()
        for col in ["close", "high", "low", "volume", "turn", "peTTM", "pbMRQ"]:
            raw[col] = pd.to_numeric(raw[col], errors="coerce")
        raw = raw.dropna(subset=["close"])

        # 重命名以复用现有计算逻辑
        df = raw.rename(columns={
            "close": "收盘", "high": "最高", "low": "最低",
            "volume": "成交量", "turn": "换手率",
        })

        # PE/PB 直接从 BaoStock 拿，覆盖腾讯财经的估算值
        last_pe = float(raw["peTTM"].iloc[-1]) if raw["peTTM"].iloc[-1] else 0
        last_pb = float(raw["pbMRQ"].iloc[-1]) if raw["pbMRQ"].iloc[-1] else 0
        item.update({"pe_ttm": round(last_pe, 2), "pb": round(last_pb, 2)})
        emit(f"  PE(TTM)={last_pe:.1f}x  PB={last_pb:.2f}x  市值={valuation['mcap_yi']:.0f}亿", ticker=ticker)

        emit(f"📐 {name} 拉取历史估值分位…", ticker=ticker)
        percentile = fetch_valuation_percentile(ticker, name, last_pe, last_pb)
        item.update(percentile)
        if percentile["pe_percentile"] is not None:
            pb_pct_text = f"，PB分位{percentile['pb_percentile']}%" if percentile["pb_percentile"] is not None else ""
            emit(f"  PE历史分位 {percentile['pe_percentile']}%{pb_pct_text}", ticker=ticker)

        df["MA5"]  = df["收盘"].rolling(5).mean()
        df["MA20"] = df["收盘"].rolling(20).mean()
        prev, last = df.iloc[-2], df.iloc[-1]

        if prev["MA5"] < prev["MA20"] and last["MA5"] >= last["MA20"]:
            ma_signal = "MA5/MA20 金叉"
        elif prev["MA5"] > prev["MA20"] and last["MA5"] <= last["MA20"]:
            ma_signal = "MA5/MA20 死叉"
        else:
            ma_signal = "MA5/MA20 无交叉"

        avg_vol  = float(df["成交量"].iloc[-6:-1].mean())
        last_vol = float(last["成交量"])
        if last_vol > avg_vol * 1.3:
            vol_signal = "放量"
        elif last_vol < avg_vol * 0.7:
            vol_signal = "缩量"
        else:
            vol_signal = "量能平稳"

        deviation       = (last["收盘"] - last["MA20"]) / last["MA20"] * 100
        price_change_5d = (last["收盘"] - df.iloc[-6]["收盘"]) / df.iloc[-6]["收盘"] * 100

        # ── RSI ──
        rsi        = calc_rsi(df["收盘"])
        rsi_sig    = rsi_signal(rsi)

        # ── MACD ──
        dif, dea, hist     = calc_macd(df["收盘"])
        prev_dif, prev_dea, prev_hist = calc_macd(df["收盘"].iloc[:-1])
        macd_sig   = macd_signal(dif, dea, hist, prev_hist)

        # ── KDJ ──
        k, d, j             = calc_kdj(df)
        prev_k, prev_d, _   = calc_kdj(df.iloc[:-1])
        kdj_sig    = kdj_signal(k, d, j, prev_k, prev_d)

        # ── 换手率 ──
        turnover     = round(float(last["换手率"]), 2)
        turnover_avg = round(float(df["换手率"].iloc[-6:-1].mean()), 2)

        # ── 波动率 / 最大回撤 ──
        daily_returns = df["收盘"].pct_change()
        volatility    = annualized_volatility(daily_returns)
        drawdown      = max_drawdown_pct(df["收盘"])
        risk_sig      = risk_signal(volatility, drawdown, h.get("risk_level", "medium"))

        # ── 中期趋势（MA60/MA120）──
        trend_sig = trend_position(df["收盘"])

        item.update({
            "ma_signal":       ma_signal,
            "vol_signal":      vol_signal,
            "ma20_deviation":  round(float(deviation), 2),
            "price_change_5d": round(float(price_change_5d), 2),
            "close":           round(float(last["收盘"]), 2),
            "rsi":             rsi,
            "rsi_signal":      rsi_sig,
            "macd_signal":     macd_sig,
            "kdj_k": k, "kdj_d": d, "kdj_j": j,
            "kdj_signal":      kdj_sig,
            "turnover":        turnover,
            "turnover_avg5":   turnover_avg,
            "volatility":      volatility,
            "max_drawdown":    drawdown,
            "risk_signal":     risk_sig,
            "trend_signal":    trend_sig,
        })

        emit(f"📈 {name}：{ma_signal}，{vol_signal}，近5日 {item['price_change_5d']:+.1f}%", ticker=ticker)
        emit(f"  {rsi_sig}  |  MACD {macd_sig}  |  KDJ {kdj_sig}", ticker=ticker)
        emit(f"  换手率 {turnover}%（5日均 {turnover_avg}%）", ticker=ticker)
        emit(f"  {trend_sig}", ticker=ticker)
        emit(f"  {risk_sig}", ticker=ticker)

    except Exception as e:
        emit(f"⚠️  {name} 行情获取失败（{type(e).__name__}），使用模拟数据", ticker=ticker)
        fallback_price = valuation["price"] or round(h.get("cost", 10) * 1.05, 2)
        item.update({
            "ma_signal":       "MA5/MA20 金叉（模拟）",
            "vol_signal":      "放量（模拟）",
            "ma20_deviation":  2.5,
            "price_change_5d": 3.2,
            "close":           fallback_price,
            "rsi":             0,
            "rsi_signal":      "RSI 不可用（模拟数据）",
            "macd_signal":     "MACD 不可用（模拟数据）",
            "kdj_k": 0, "kdj_d": 0, "kdj_j": 0,
            "kdj_signal":      "KDJ 不可用（模拟数据）",
            "turnover":        0,
            "turnover_avg5":   0,
            "pe_percentile":   None,
            "pb_percentile":   None,
            "volatility":      0,
            "max_drawdown":    0,
            "risk_signal":     "风险指标不可用（模拟数据）",
            "trend_signal":    "中期趋势不可用（模拟数据）",
        })

    return item
