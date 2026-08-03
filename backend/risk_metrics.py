"""风险指标：年化波动率、最大回撤，纯函数，不依赖网络或全局状态。"""

import math

TRADING_DAYS_PER_YEAR = 252

_RISK_LABEL = {"low": "低", "medium": "中", "high": "高"}


def annualized_volatility(daily_returns: "pd.Series") -> float:
    """日收益率序列 → 年化波动率（%）。"""
    daily_std = daily_returns.dropna().std()
    return round(float(daily_std * math.sqrt(TRADING_DAYS_PER_YEAR) * 100), 2)


def max_drawdown_pct(close: "pd.Series") -> float:
    """收盘价序列 → 区间内最大回撤（%，正数表示回撤幅度）。"""
    cummax   = close.cummax()
    drawdown = (close - cummax) / cummax
    return round(float(-drawdown.min() * 100), 2)


def _classify_risk(volatility: float, drawdown: float) -> str:
    if volatility >= 45 or drawdown >= 30:
        return "high"
    if volatility <= 20 and drawdown <= 12:
        return "low"
    return "medium"


def risk_signal(volatility: float, drawdown: float, declared_risk: str) -> str:
    """把算出来的波动率/回撤和用户自填的 risk_level 交叉对比，背离时给出提示。"""
    actual        = _classify_risk(volatility, drawdown)
    declared_text = _RISK_LABEL.get(declared_risk, declared_risk)
    actual_text   = _RISK_LABEL[actual]

    base = f"年化波动率{volatility}%，最大回撤{drawdown}%"
    if actual != declared_risk:
        return f"{base}，实际风险偏{actual_text}，与标注的「{declared_text}风险」存在背离"
    return f"{base}，与标注的「{declared_text}风险」基本一致"
