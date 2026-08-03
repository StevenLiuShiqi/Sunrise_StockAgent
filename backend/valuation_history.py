"""历史估值分位：拉取近5年 PE(TTM)/PB 序列，计算当前值在历史分布中的分位数。

接口用 ak.stock_zh_valuation_baidu 核实过（百度股市通，返回 date/value 两列，
覆盖近5年日频估值），不是文档里未经验证的 stock_a_indicator_lg。
"""

from . import config  # noqa: F401  先清代理环境变量，再 import akshare

import akshare as ak

from .progress import emit

_PERIOD = "近五年"


def _percentile(series: "pd.Series", value: float) -> float | None:
    """value 在历史序列里的分位数（0~100）。"""
    valid = series.dropna()
    if value is None or valid.empty:
        return None
    rank = (valid < value).sum()
    return round(float(rank) / len(valid) * 100, 1)


def fetch_valuation_percentile(ticker: str, name: str, pe_ttm: float, pb: float) -> dict:
    """返回 {"pe_percentile":.., "pb_percentile":..}，单路失败不影响另一路。"""
    result: dict = {"pe_percentile": None, "pb_percentile": None}

    try:
        pe_hist = ak.stock_zh_valuation_baidu(symbol=ticker, indicator="市盈率(TTM)", period=_PERIOD)
        result["pe_percentile"] = _percentile(pe_hist["value"], pe_ttm)
    except Exception as e:
        emit(f"⚠️ {name} 历史PE分位拉取失败（{type(e).__name__}）")

    try:
        pb_hist = ak.stock_zh_valuation_baidu(symbol=ticker, indicator="市净率", period=_PERIOD)
        result["pb_percentile"] = _percentile(pb_hist["value"], pb)
    except Exception as e:
        emit(f"⚠️ {name} 历史PB分位拉取失败（{type(e).__name__}）")

    return result
