"""K线数据 API：把 market_data.py 里已有的历史行情查询封装成前端图表组件
（Lightweight Charts）能直接消费的格式。不重新实现查询逻辑，复用
market_data.fetch_ohlcv_df()。
"""

from .market_data import HISTORY_WINDOW_DAYS, fetch_ohlcv_df


def _fmt_date(value) -> str:
    """akshare 返回的 date 列有时是 datetime.date 对象、有时是字符串（取决于
    请求是否分页），统一转成 Lightweight Charts 要的 'YYYY-MM-DD' 字符串。"""
    return value.strftime("%Y-%m-%d") if hasattr(value, "strftime") else str(value)


def fetch_kline_series(ticker: str, days: int = HISTORY_WINDOW_DAYS) -> list[dict]:
    """返回 [{"time": "2024-01-01", "open":.., "high":.., "low":.., "close":.., "volume":..}, ...]。

    只返回原始K线数据，不附带技术指标——MA 叠加线之类的由前端图表组件自己用
    收盘价序列算。失败时直接把异常抛给调用方（FastAPI 路由），不伪造数据。
    """
    df = fetch_ohlcv_df(ticker, days)
    return [
        {
            "time":   _fmt_date(row["date"]),
            "open":   round(float(row["开盘"]), 2),
            "high":   round(float(row["最高"]), 2),
            "low":    round(float(row["最低"]), 2),
            "close":  round(float(row["收盘"]), 2),
            "volume": round(float(row["成交量"]), 0),
        }
        for _, row in df.iterrows()
    ]
