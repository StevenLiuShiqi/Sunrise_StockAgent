"""LangGraph 流程编排：本地分析 → 用户确认 → 云端调研 → 本地融合。"""

from typing import Literal, TypedDict

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from .cloud_research import akshare_research
from .config import DOMAIN_MAP, DOMAINS_OPINION, MODEL_PRO, llm, tavily
from .debate import bear_researcher_node, bull_researcher_node
from .holdings import load_holdings
from .llm_questions import filter_snippets, llm_generate_questions
from .market_data import analyze_stock, fetch_valuation
from .portfolio_analysis import analyze_portfolio
from .progress import emit, emit_event


class State(TypedDict, total=False):
    selected_tickers: list[str]   # 用户在选股面板勾选的股票代码；空列表表示全部
    deep_mode: bool               # 是否跑看多/看空辩论架构，关闭时走单次 fusion 轻量流程
    agent_a_result: list[dict]
    portfolio_summary: dict
    research_questions: list[dict]
    user_decision: Literal["approved", "rejected"]
    agent_b_result: list[dict]
    bull_case: dict   # {ticker: {"name": str, "points": [str, ...]}}，仅深度模式
    bear_case: dict   # 同上
    fusion_result: dict


# ================= Agent A =================

def agent_a_node(state: State) -> dict:
    # 每次都从磁盘重新读，不用进程启动时缓存的旧值——交易终端的买卖会直接改
    # holdings.json，要让每次分析都看到最新的持仓状态。
    all_holdings, risk_profile = load_holdings()

    selected = state.get("selected_tickers") or []
    holdings = [h for h in all_holdings if not selected or h["ticker"] in selected]
    emit(f"📋 分析 {len(holdings)} 支股票（持仓共 {len(all_holdings)} 支）")

    full_result        = []
    research_questions = []

    for h in holdings:
        analysis = analyze_stock(h)
        full_result.append(analysis)

        findings = {
            "ma_signal":       analysis["ma_signal"],
            "vol_signal":      analysis["vol_signal"],
            "ma20_deviation":  analysis["ma20_deviation"],
            "price_change_5d": analysis["price_change_5d"],
            "rsi_signal":      analysis.get("rsi_signal", ""),
            "macd_signal":     analysis.get("macd_signal", ""),
            "kdj_signal":      analysis.get("kdj_signal", ""),
            "turnover":        analysis.get("turnover", 0),
            "turnover_avg5":   analysis.get("turnover_avg5", 0),
            "pe_ttm":          analysis.get("pe_ttm", 0),
            "pb":              analysis.get("pb", 0),
            "mcap_yi":         analysis.get("mcap_yi", 0),
            "pe_percentile":   analysis.get("pe_percentile"),
            "pb_percentile":   analysis.get("pb_percentile"),
            "trend_signal":    analysis.get("trend_signal", ""),
            "volatility":      analysis.get("volatility", 0),
            "max_drawdown":    analysis.get("max_drawdown", 0),
            "risk_signal":     analysis.get("risk_signal", ""),
        }

        questions = llm_generate_questions(
            analysis["ticker"],
            analysis["name"],
            findings,
            risk_level=analysis.get("risk_level", "medium"),
            hold_period=analysis.get("hold_period", "mid"),
            notes=analysis.get("notes", ""),
            risk_profile=risk_profile,
        )

        research_questions.append({
            "ticker":        analysis["ticker"],
            "name":          analysis["name"],
            "qty":           analysis.get("qty", 0),
            "cost":          analysis.get("cost", 0),
            "price":         analysis.get("price") or analysis.get("close") or 0,
            "industry_tags": analysis["industry_tags"],
            "risk_level":    analysis.get("risk_level", "medium"),
            "hold_period":   analysis.get("hold_period", "mid"),
            "notes":         analysis.get("notes", ""),
            "findings":      findings,
            "questions":     questions,
        })

    # 组合集中度必须按【全部持仓】算，不能只按本次选中的股票算——否则选1支就会
    # 被误判成"仓位100%"。没被选中深度分析的持仓，只拉一次实时价格用于算市值权重，
    # 不做技术指标/风险指标这些重分析（那些只有用户选中要看的股票才需要）。
    analyzed_tickers = {h["ticker"] for h in holdings}
    other_holdings   = [h for h in all_holdings if h["ticker"] not in analyzed_tickers]

    portfolio_positions = list(full_result)
    if other_holdings:
        emit(f"📊 拉取其余 {len(other_holdings)} 支持仓的实时价格，用于计算组合仓位权重…")
        for h in other_holdings:
            valuation = fetch_valuation(h["ticker"])
            portfolio_positions.append({**h, "price": valuation["price"]})

    portfolio_summary = analyze_portfolio(portfolio_positions)
    if portfolio_summary["warnings"]:
        emit("📊 组合概览：" + "；".join(portfolio_summary["warnings"]))
    else:
        emit("📊 组合概览：暂无明显集中度风险")

    emit("🎯 本地分析完成，等待你确认")

    return {
        "agent_a_result":     full_result,
        "portfolio_summary":  portfolio_summary,
        "research_questions": research_questions,
    }


# ================= 确认节点 =================

def human_confirm_node(state: State) -> dict:
    # 确认清单 = 即将发往云端的内容，因此不含任何持仓/成本，避免误会。
    # 持仓与成本只在最后的本地 fusion 分析中使用（数据源为本地 agent_a_result）。
    decision = interrupt({
        "research_questions": state["research_questions"],
        "portfolio_summary":  state.get("portfolio_summary"),
    })
    return {"user_decision": decision["action"]}


# ================= 路由 =================

def route_after_confirm(state: State) -> str:
    return "agent_b" if state["user_decision"] == "approved" else END


# ================= Agent B：结构化主力 + Tavily 补充 =================

def agent_b_node(state: State) -> dict:
    emit_event({"type": "cloud_progress", "text": f"☁️ 开始云端调研，共 {len(state['research_questions'])} 支股票"})
    results = []

    for item in state["research_questions"]:
        ticker = item["ticker"]
        name   = item["name"]

        # ── 第一层：结构化数据（主力信源）──
        emit_event({"type": "cloud_progress", "text": f"🗂 {name} 开始拉取结构化数据…", "ticker": ticker})
        structured = akshare_research(ticker, name)

        # 把结构化结果作为可展开卡片推给前端
        emit_event({
            "type":         "structured_result",
            "name":         name,
            "news":         structured.get("news", []),
            "hot_reason":   structured.get("hot_reason"),
            "consensus_eps": structured.get("consensus_eps", []),
        })

        # ── 第二层：Tavily 补充（只搜 opinion 类问题，factual/news 已由结构化覆盖）──
        qa_list: list[dict] = []
        tavily_questions = [q for q in item["questions"] if q.get("type") == "opinion"]
        other_questions  = [q for q in item["questions"] if q.get("type") != "opinion"]

        # opinion 问题走 Tavily
        for q_obj in tavily_questions:
            q_query  = q_obj["query"]     # 实际发给 Tavily 的检索词
            q_reason = q_obj.get("reason", "")  # 只用于展示和过滤时的上下文，绝不进入搜索请求
            q_type   = q_obj.get("type", "opinion")
            domains  = DOMAIN_MAP.get(q_type, DOMAINS_OPINION)

            raw_snippets: list[str] = []
            try:
                resp = tavily.search(
                    query=f"{name} {q_query}",
                    max_results=6,
                    search_depth="advanced",
                    include_domains=domains,
                    days=30,
                )
                raw_snippets = [
                    r["content"] for r in resp.get("results", [])
                    if r.get("content") and len(r["content"].strip()) > 40
                ]
            except Exception as e:
                emit_event({"type": "cloud_progress", "text": f"⚠️ 搜索失败（{type(e).__name__}）", "ticker": ticker})

            # 兜底：白名单没搜到就放开域名限制
            if not raw_snippets:
                try:
                    resp = tavily.search(
                        query=f"{name} {q_query}",
                        max_results=5,
                        search_depth="advanced",
                        days=60,
                    )
                    raw_snippets = [
                        r["content"] for r in resp.get("results", [])
                        if r.get("content") and len(r["content"].strip()) > 40
                    ]
                except Exception:
                    pass

            # 过滤片段用 query + reason 做上下文帮助判断相关性（这是 LLM 内部推理，不是搜索请求）
            filter_context = f"{q_query}（{q_reason}）" if q_reason else q_query
            filtered = filter_snippets(filter_context, raw_snippets) if raw_snippets else []

            emit_event({
                "type":      "search_result",
                "name":      name,
                "query":     q_query,
                "reason":    q_reason,
                "q_type":    q_type,
                "snippets":  [s[:400] for s in filtered],
                "raw_count": len(raw_snippets),
            })
            qa_list.append({"query": q_query, "reason": q_reason, "snippets": filtered})

        # factual / news 问题记录一下（结构化已覆盖，不再单独搜索）
        for q_obj in other_questions:
            qa_list.append({
                "query":       q_obj["query"],
                "reason":      q_obj.get("reason", ""),
                "snippets":    [],
                "covered_by":  "structured",
            })

        results.append({
            "ticker":     ticker,
            "name":       name,
            "structured": structured,
            "qa":         qa_list,
        })

    emit_event({"type": "cloud_progress", "text": "✅ 云端调研完成"})
    return {"agent_b_result": results}


# ================= 风险管理层：综合看多/看空论据 + 持仓成本（原 fusion_node）=================
# 这一步在【本地】进行：云端只把公开新闻/调研结果传回本地，
# 持仓量、成本这些个人数据从不上传云端，只在本地由 AI 结合分析。
# 当前临时调用 DeepSeek API，后续将替换为完全本地运行的模型。
# 深度模式下 state 里会带 bull_case/bear_case（来自 debate.py 的两个研究员节点），
# 风险管理层要综合两方论据；轻量模式下没有这两个字段，退回原来的单次综合。

def _build_stock_sections(state: State) -> list[str]:
    """构建每支股票的信息块：持仓成本/浮盈亏 + 本地技术信号 + 云端数据。"""
    sections = []
    b_map = {item["ticker"]: item for item in state["agent_b_result"]}

    for h in state["agent_a_result"]:
        ticker   = h["ticker"]
        name     = h["name"]
        findings = (
            f"  均线：{h.get('ma_signal', '-')}\n"
            f"  量能：{h.get('vol_signal', '-')}\n"
            f"  偏离MA20：{h.get('ma20_deviation', '-')}%\n"
            f"  近5日涨跌：{h.get('price_change_5d', '-')}%"
        )

        # 持仓信息：持仓量、成本、当前价、浮盈亏
        qty     = h.get("qty", 0)
        cost    = h.get("cost", 0)
        cur     = h.get("price") or h.get("close") or 0
        position_text = f"  持仓：{qty} 股，成本价 {cost}"
        if cur and cost:
            pnl_pct = (cur - cost) / cost * 100
            pnl_amt = (cur - cost) * qty
            status  = "浮盈" if pnl_pct >= 0 else "浮亏"
            position_text += (
                f"，现价 {round(cur, 2)}，"
                f"{status} {abs(round(pnl_pct, 2))}%（约 {round(pnl_amt, 0):.0f} 元）"
            )

        structured_text = ""
        tavily_text     = ""

        if ticker in b_map:
            s = b_map[ticker].get("structured", {})

            # 个股新闻（最新 3 条标题）
            news_items = s.get("news", [])
            if news_items:
                headlines = "; ".join(
                    f"{n.get('发布时间', '')[:10]} {n.get('新闻标题', '')}"
                    for n in news_items[:3]
                )
                structured_text += f"  近期新闻：{headlines}\n"

            # 题材归因
            hot = s.get("hot_reason")
            if hot:
                structured_text += f"  题材归因：{hot}\n"

            # 分析师一致预期
            eps_list = s.get("consensus_eps", [])
            if eps_list:
                eps_summary = "；".join(
                    str(list(row.values())) for row in eps_list[:2]
                )
                structured_text += f"  分析师一致预期EPS：{eps_summary}\n"

            # Tavily 补充（opinion 类）：用 reason（人类可读的推理链）做展示，reason 缺失时兜底用 query
            for qa in b_map[ticker].get("qa", []):
                if qa.get("covered_by") == "structured":
                    continue
                q       = qa.get("reason") or qa["query"]
                summary = " ".join(qa["snippets"])[:300] if qa["snippets"] else "无相关信息"
                tavily_text += f"  问：{q}\n  网搜摘要：{summary}\n\n"

        cloud_section = ""
        if structured_text:
            cloud_section += f"结构化数据：\n{structured_text}\n"
        if tavily_text:
            cloud_section += f"网络搜索补充：\n{tavily_text}"

        sections.append(
            f"【{name}（{ticker}）】\n"
            f"我的持仓：\n{position_text}\n\n"
            f"本地技术信号：\n{findings}\n\n"
            f"云端数据：\n{cloud_section or '  无'}"
        )

    return sections


def _portfolio_text(state: State) -> str:
    """始终展示完整持仓构成（不只是触发阈值才有的警告）——否则单独分析1支股票时
    LLM 的 prompt 里完全看不到"这只是持仓里的一支，占比多少"这个信息，会把这支
    股票当成仓位全部来分析，即便 analyze_portfolio() 早就正确算出了真实占比。
    """
    summary   = state.get("portfolio_summary") or {}
    positions = summary.get("position_concentration") or []
    if not positions:
        return "- 暂无持仓组合数据"

    total_value = summary.get("total_value") or 0
    lines = [f"- 持仓组合共 {len(positions)} 支股票，总市值约 {total_value:,.0f} 元，各股占比："]
    lines += [f"  - {p['name']}（{p['ticker']}）：{p['weight_pct']}%" for p in positions]

    warnings = summary.get("warnings") or []
    if warnings:
        lines.append("- 集中度提示：")
        lines += [f"  - {w}" for w in warnings]
    else:
        lines.append("- 暂无明显集中度风险")

    return "\n".join(lines)


def _format_case(case: dict, label: str) -> str:
    if not case:
        return f"（无{label}论据）"
    blocks = []
    for ticker, info in case.items():
        points = "\n".join(f"  - {p}" for p in info.get("points", []))
        blocks.append(f"【{info.get('name', ticker)}（{ticker}）】\n{points}")
    return "\n\n".join(blocks)


def _build_light_prompt(portfolio_text: str, sections: list[str]) -> str:
    """轻量模式：单次 LLM 调用直接给建议（原有行为，不跑辩论架构）。"""
    return (
        "你是一位严谨的 A 股研究分析师。请根据以下持仓组合概览、每只股票的持仓情况、"
        "本地技术信号和云端调研信息，给出结合持仓成本的个性化投资建议。\n\n"
        f"持仓组合概览：\n{portfolio_text}\n\n"
        "要求：\n"
        "- 若组合概览中提示了集中度风险，请在综合建议中呼应一下，不要只谈单只股票\n"
        "- 每只股票给出：①一句话综合判断 ②建议操作（持有/减仓/加仓/清仓/观望）③一条主要风险提示\n"
        "- 结合当前浮盈/浮亏状态给出针对性建议（如浮亏是否补仓、浮盈是否止盈）\n"
        "- 建议要有依据，紧扣具体信号和持仓成本\n\n"
        + "\n\n---\n\n".join(sections)
    )


def _build_debate_prompt(portfolio_text: str, sections: list[str], bull_case: dict, bear_case: dict) -> str:
    """深度模式：综合看多/看空研究员的论据 + 持仓成本，输出三段式结构。"""
    return (
        "你是风险管理层，负责综合看多研究员和看空研究员的论据，结合用户的实际持仓成本和风险偏好，"
        "给出最终判断。看多/看空研究员看不到持仓成本，只看客观信号；只有你能看到持仓成本，"
        "这是有意的分层设计，避免论点被用户已有仓位的浮盈浮亏锚定。\n\n"
        f"持仓组合概览：\n{portfolio_text}\n\n"
        f"看多研究员论据：\n{_format_case(bull_case, '看多')}\n\n"
        f"看空研究员论据：\n{_format_case(bear_case, '看空')}\n\n"
        "各股票详细信息（持仓成本、本地技术信号、云端数据）：\n\n"
        + "\n\n---\n\n".join(sections) +
        "\n\n请输出以下三段式结构（用 markdown ### 标题）：\n\n"
        "### 看多论点\n"
        "逐股用你自己的话简要复述看多研究员的论据（不要照抄原文）。\n\n"
        "### 看空论点\n"
        "逐股用你自己的话简要复述看空研究员的论据。\n\n"
        "### 风险管理层综合\n"
        "逐股给出：①权衡两方论据后你认为哪方更有说服力、为什么 ②结合持仓成本、浮盈浮亏、"
        "风险偏好背离信息给出操作建议（持有/减仓/加仓/清仓/观望）③明确这是基于当前信息的判断，"
        "不是确定性预测，语气克制，避免武断措辞。若组合概览中提示了集中度风险，在这一段里呼应一下。"
    )


def risk_manager_node(state: State) -> dict:
    # 云端调研（以及深度模式下的看多/看空辩论）结果已返回本地，进入本地综合分析
    emit_event({"type": "return_arrow"})
    emit("🔒 本地 AI 结合你的持仓与成本，综合分析中…")

    sections       = _build_stock_sections(state)
    portfolio_text = _portfolio_text(state)
    bull_case      = state.get("bull_case") or {}
    bear_case      = state.get("bear_case") or {}
    deep           = bool(bull_case or bear_case)

    prompt = (
        _build_debate_prompt(portfolio_text, sections, bull_case, bear_case)
        if deep else
        _build_light_prompt(portfolio_text, sections)
    )

    try:
        # MODEL_PRO 是推理模型，同样字数的正文比 deepseek-chat 要多花不少 token 在
        # reasoning_content 上，这里的 max_tokens 是实测多支股票场景校准过的，
        # 别直接照搬 deepseek-chat 时代的数值，会出现 finish_reason=length 但正文截断/为空。
        resp   = llm.chat.completions.create(
            model=MODEL_PRO,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=8000 if deep else 4000,
            temperature=0.5,
        )
        advice = resp.choices[0].message.content.strip()
        emit("✅ 本地综合建议生成完毕")
    except Exception as e:
        emit(f"⚠️  建议生成失败（{type(e).__name__}）")
        advice = "（建议生成失败，请检查 LLM_API_KEY）"

    return {"fusion_result": {
        "advice":     advice,
        "cloud_info": state["agent_b_result"],
    }}


# ================= 路由：是否跑辩论架构 =================

def route_after_agent_b(state: State) -> list[str]:
    """深度模式 fan-out 到看多/看空研究员（并行）；轻量模式直接进风险管理层。"""
    if state.get("deep_mode"):
        return ["bull_researcher", "bear_researcher"]
    return ["fusion"]


# ================= 组图 =================

def build_graph():
    g = StateGraph(State)
    g.add_node("agent_a",         agent_a_node)
    g.add_node("human_confirm",   human_confirm_node)
    g.add_node("agent_b",         agent_b_node)
    g.add_node("bull_researcher", bull_researcher_node)
    g.add_node("bear_researcher", bear_researcher_node)
    # 节点名保留 "fusion"（不是 "risk_manager"），app.py 依赖 result.get("fusion_result") 这个既有约定
    g.add_node("fusion",          risk_manager_node)

    g.add_edge(START, "agent_a")
    g.add_edge("agent_a", "human_confirm")
    g.add_conditional_edges(
        "human_confirm", route_after_confirm,
        {"agent_b": "agent_b", END: END},
    )
    g.add_conditional_edges(
        "agent_b", route_after_agent_b,
        ["bull_researcher", "bear_researcher", "fusion"],
    )
    g.add_edge("bull_researcher", "fusion")
    g.add_edge("bear_researcher", "fusion")
    g.add_edge("fusion", END)
    return g.compile(checkpointer=MemorySaver())


research_graph = build_graph()
