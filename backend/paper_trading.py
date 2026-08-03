"""模拟交易：市价单成交，直接读写 holdings.json。

不对接任何真实交易系统，成交价直接用 fetch_valuation() 的实时快照价格，
假设"立即全部成交"，不模拟排队/部分成交/限价单——这次只做最简单的市价单模拟。

持仓存储跟 AI 分析共用同一份 holdings.json（不是单独一份 paper_positions.json），
这样交易终端下单之后，下一次 AI 分析看到的就是交易完的最新持仓——这是"真正的
模拟持仓"的核心：买卖要真的改变分析所依据的数据，不是自说自话的两套账。
委托历史仍然单独记在 paper_orders.json 里，这是纯粹的流水记录，跟持仓快照是
两个不同的概念，不需要合并。
"""

import json
from datetime import datetime
from pathlib import Path

from .holdings import load_holdings, save_holdings
from .market_data import fetch_valuation

_ROOT = Path(__file__).parent.parent
ORDERS_FILE = _ROOT / "paper_orders.json"

_DEFAULT_RISK_LEVEL  = "medium"
_DEFAULT_HOLD_PERIOD = "mid"


def _load_orders() -> list:
    try:
        return json.loads(ORDERS_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except json.JSONDecodeError as e:
        raise RuntimeError(f"{ORDERS_FILE} 格式错误：{e}")


def _save_orders(orders: list) -> None:
    ORDERS_FILE.write_text(
        json.dumps(orders, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def place_paper_order(ticker: str, side: str, qty: int) -> dict:
    """市价单模拟成交：拿当前快照价直接记一笔成交，直接改写 holdings.json。
    加仓按加权平均成本计算，保留原有的 industry_tags/risk_level/hold_period/notes；
    全部卖出会把这支股票从持仓列表里整条删掉。
    返回 {"order": {...}, "holding": {...} | None}（holding 为 None 表示已清仓）。
    """
    if side not in ("buy", "sell"):
        raise ValueError(f"side 必须是 buy 或 sell，收到：{side}")
    if qty <= 0:
        raise ValueError(f"qty 必须大于 0，收到：{qty}")

    price = fetch_valuation(ticker)["price"]
    if not price:
        raise RuntimeError(f"拿不到 {ticker} 的实时价格，无法模拟成交")

    holdings, risk_profile = load_holdings()
    idx = next((i for i, h in enumerate(holdings) if h["ticker"] == ticker), None)
    existing = holdings[idx] if idx is not None else None

    if side == "buy":
        if existing:
            new_qty  = existing["qty"] + qty
            new_cost = (existing["qty"] * existing["cost"] + qty * price) / new_qty
            updated  = {**existing, "qty": new_qty, "cost": round(new_cost, 4)}
            holdings = [*holdings[:idx], updated, *holdings[idx + 1:]]
        else:
            # 终端选股面板目前只会给已有持仓的 ticker 下单，这个分支是给以后
            # 万一支持"买入全新股票"留的：名字/行业标签/风险偏好没处拿，先用
            # 合理默认值兜底，不阻塞交易本身。
            updated = {
                "ticker": ticker, "name": ticker, "qty": qty, "cost": round(price, 4),
                "industry_tags": [], "risk_level": _DEFAULT_RISK_LEVEL,
                "hold_period": _DEFAULT_HOLD_PERIOD, "notes": "模拟买入新增持仓",
            }
            holdings = [*holdings, updated]
    else:  # sell
        held = existing["qty"] if existing else 0
        name = existing["name"] if existing else ticker
        if qty > held:
            raise ValueError(f"{name}（{ticker}）持仓只有 {held} 股，不能卖出 {qty} 股")
        remaining = held - qty
        if remaining == 0:
            updated  = None
            holdings = [*holdings[:idx], *holdings[idx + 1:]]
        else:
            updated  = {**existing, "qty": remaining}
            holdings = [*holdings[:idx], updated, *holdings[idx + 1:]]

    save_holdings(holdings, risk_profile)

    order = {
        "ticker":    ticker,
        "name":      updated["name"] if updated else (existing["name"] if existing else ticker),
        "side":      side,
        "qty":       qty,
        "price":     price,
        "timestamp": datetime.now().isoformat(timespec="seconds"),
    }
    orders = _load_orders()
    orders.append(order)
    _save_orders(orders)

    return {"order": order, "holding": updated}


def get_paper_positions() -> list[dict]:
    """当前所有持仓：ticker/name/qty/avg_cost/当前价/浮动盈亏（已在这里算好）。
    直接读 holdings.json——交易终端的持仓表格和 AI 分析用的是同一份数据。
    """
    holdings, _ = load_holdings()
    result = []
    for h in holdings:
        cost    = h.get("cost", 0)
        price   = fetch_valuation(h["ticker"])["price"] or cost
        pnl_pct = (price - cost) / cost * 100 if cost else 0
        pnl_amt = (price - cost) * h.get("qty", 0)
        result.append({
            "ticker":     h["ticker"],
            "name":       h["name"],
            "qty":        h.get("qty", 0),
            "avg_cost":   cost,
            "price":      round(price, 2),
            "pnl_pct":    round(pnl_pct, 2),
            "pnl_amount": round(pnl_amt, 2),
        })
    return result


def get_paper_orders() -> list[dict]:
    """历史成交记录，按时间倒序。"""
    return sorted(_load_orders(), key=lambda o: o["timestamp"], reverse=True)
