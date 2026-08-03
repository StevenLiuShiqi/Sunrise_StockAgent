"""持仓组合层面分析：行业集中度、单票集中度、粗略相关性提示。

输入是 agent_a_node() 里已经拿到的全部个股分析结果（含 qty/cost，只在本地使用），
纯函数，不发起任何网络请求。
"""

from collections import defaultdict

POSITION_CONCENTRATION_THRESHOLD = 0.30  # 单票占比超过此阈值视为重仓
INDUSTRY_CONCENTRATION_THRESHOLD = 0.40  # 行业占比超过此阈值视为集中


def _position_value(h: dict) -> float:
    qty   = h.get("qty", 0) or 0
    price = h.get("price") or h.get("close") or h.get("cost", 0) or 0
    return qty * price


def analyze_portfolio(full_result: list[dict]) -> dict:
    """计算行业集中度、单票集中度，并给出可读的风险提示文案。"""
    positions = [
        {
            "ticker":        h["ticker"],
            "name":          h["name"],
            "value":         _position_value(h),
            "industry_tags": h.get("industry_tags") or [],
        }
        for h in full_result
    ]
    total_value = sum(p["value"] for p in positions)

    if total_value <= 0:
        return {
            "total_value":            0,
            "industry_concentration": [],
            "position_concentration": [],
            "warnings":               [],
        }

    for p in positions:
        p["weight_pct"] = round(p["value"] / total_value * 100, 1)

    # 按行业标签聚合仓位权重；一支股票可能同时属于多个行业，权重会重复计入各自行业，
    # 这是简化处理，不代表行业占比总和等于 100%。
    industry_value:   dict[str, float]      = defaultdict(float)
    industry_holders: dict[str, list[str]]  = defaultdict(list)
    for p in positions:
        for tag in p["industry_tags"]:
            industry_value[tag] += p["value"]
            industry_holders[tag].append(p["name"])

    industry_concentration = sorted(
        (
            {
                "industry":   tag,
                "weight_pct": round(value / total_value * 100, 1),
                "names":      industry_holders[tag],
            }
            for tag, value in industry_value.items()
        ),
        key=lambda x: x["weight_pct"],
        reverse=True,
    )

    position_concentration = sorted(
        ({"ticker": p["ticker"], "name": p["name"], "weight_pct": p["weight_pct"]} for p in positions),
        key=lambda x: x["weight_pct"],
        reverse=True,
    )

    warnings = []
    for ind in industry_concentration:
        if ind["weight_pct"] >= INDUSTRY_CONCENTRATION_THRESHOLD * 100 and len(ind["names"]) > 1:
            warnings.append(
                f"「{ind['industry']}」行业集中度 {ind['weight_pct']}%"
                f"（{'、'.join(ind['names'])}），建议关注集中度风险"
            )
    for pos in position_concentration:
        if pos["weight_pct"] >= POSITION_CONCENTRATION_THRESHOLD * 100:
            warnings.append(f"{pos['name']}（{pos['ticker']}）单票占比 {pos['weight_pct']}%，为重仓股")

    return {
        "total_value":            round(total_value, 0),
        "industry_concentration": industry_concentration,
        "position_concentration": position_concentration,
        "warnings":               warnings,
    }
