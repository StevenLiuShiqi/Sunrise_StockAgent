"""Trading Agent 辩论架构：看多/看空研究员。

只借鉴"辩论 + 风控综合"这个节点结构，不引入 TradingAgents 这个包本身（它的数据源是
美股/yfinance，跟这里的 A 股管道不兼容）。这两个节点只用 agent_a_result 的客观信号
（技术指标/估值分位）+ agent_b_result 的公开调研信息做推理，看不到 qty/cost/notes/
risk_signal——持仓成本只在风险管理节点（graph.py 的 risk_manager_node）里代入，
避免研究员的论点被用户已有仓位的浮盈浮亏锚定。
"""

import json

from .config import MODEL_PRO, llm
from .json_utils import strip_code_fence
from .progress import emit_event

_OBJECTIVE_FIELDS = (
    "ma_signal", "vol_signal", "rsi_signal", "macd_signal", "kdj_signal",
    "trend_signal", "pe_ttm", "pb", "pe_percentile", "pb_percentile", "turnover",
)

_STANCE_LABEL = {"bull": "看多（bullish）", "bear": "看空（bearish）"}
_VERB_LABEL   = {"bull": "看多", "bear": "看空"}


def _objective_signals(analysis: dict) -> dict:
    """只保留股票客观信号，不含 qty/cost/notes/risk_signal 这些持仓相关字段。"""
    return {k: analysis.get(k) for k in _OBJECTIVE_FIELDS if analysis.get(k) not in (None, "")}


def _format_signals(signals: dict) -> str:
    return "\n".join(f"{k}: {v}" for k, v in signals.items()) or "（无可用技术信号）"


def _format_cloud_info(b_item: dict) -> str:
    structured = b_item.get("structured", {})
    lines = []

    news = structured.get("news", [])
    if news:
        headlines = "；".join(
            f"{n.get('发布时间', '')[:10]} {n.get('新闻标题', '')}" for n in news[:3]
        )
        lines.append(f"近期新闻：{headlines}")

    hot = structured.get("hot_reason")
    if hot:
        lines.append(f"题材归因：{hot}")

    eps = structured.get("consensus_eps", [])
    if eps:
        lines.append("分析师一致预期EPS：" + "；".join(str(list(row.values())) for row in eps[:2]))

    for qa in b_item.get("qa", []):
        if qa.get("covered_by") == "structured":
            continue
        q       = qa.get("reason") or qa.get("query", "")
        summary = " ".join(qa.get("snippets", []))[:300] if qa.get("snippets") else "无相关信息"
        lines.append(f"搜索「{q}」：{summary}")

    return "\n".join(lines) if lines else "（无云端调研信息）"


def _parse_points(raw: str) -> list[str]:
    try:
        data = json.loads(strip_code_fence(raw))
    except json.JSONDecodeError:
        return []
    if isinstance(data, dict):
        points = data.get("points", [])
    elif isinstance(data, list):
        points = data
    else:
        return []
    return [str(p).strip() for p in points if str(p).strip()]


def _run_researcher(state: dict, side: str) -> dict:
    """side: 'bull' | 'bear'。返回 {ticker: {"name": str, "points": [str, ...]}}。"""
    a_map = {a["ticker"]: a for a in state.get("agent_a_result", [])}
    case: dict = {}
    verb = _VERB_LABEL[side]

    for b_item in state.get("agent_b_result", []):
        ticker = b_item["ticker"]
        name   = b_item["name"]
        a_item = a_map.get(ticker, {})

        signals_text = _format_signals(_objective_signals(a_item))
        cloud_text   = _format_cloud_info(b_item)

        prompt = f"""你是一名{_STANCE_LABEL[side]}研究员。以下是 {name}（{ticker}）的客观技术信号和公开调研信息，
不包含任何持仓成本——你的任务只是基于这些客观信息判断这支股票，不代入任何人的持仓盈亏。

技术/估值信号：
{signals_text}

公开调研信息：
{cloud_text}

请找出 3 条最有力的{verb}论据，每条论据必须点名依据上面哪个具体信号或信息，不要空泛表述。
输出 JSON：{{"points": ["论据1", "论据2", "论据3"]}}，只输出 JSON，不要其他文字。"""

        try:
            resp = llm.chat.completions.create(
                model=MODEL_PRO,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=2000,  # MODEL_PRO 是推理模型，实测单支股票约耗450~500 token，留够余量
                temperature=0.6,
            )
            points = _parse_points(resp.choices[0].message.content.strip())
            if not points:
                points = [f"{verb}论据生成失败或为空"]
        except Exception as e:
            points = [f"{verb}论据生成失败（{type(e).__name__}）"]

        case[ticker] = {"name": name, "points": points}
        emit_event({
            "type":   "debate_result",
            "side":   side,
            "ticker": ticker,
            "name":   name,
            "points": points,
        })

    return case


def bull_researcher_node(state: dict) -> dict:
    return {"bull_case": _run_researcher(state, "bull")}


def bear_researcher_node(state: dict) -> dict:
    return {"bear_case": _run_researcher(state, "bear")}
