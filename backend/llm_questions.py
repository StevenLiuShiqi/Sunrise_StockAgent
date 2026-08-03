"""LLM①：把本地技术信号转成研究问题；以及搜索片段的相关性过滤。

研究问题拆成 query（发给搜索引擎的检索词，短语风格）和 reason（给人看的推理链，
只用于展示 + 综合推理，绝不进入搜索请求）两个字段，避免把"为什么问"的推理从句
混进检索词里拖累搜索效果。
"""

import json

from .config import MODEL_STANDARD, llm
from .json_utils import strip_code_fence
from .progress import emit

_VALID_TYPES = ("factual", "news", "opinion")


def _default_questions(name: str) -> list[dict]:
    return [{"query": f"{name} 近期消息", "reason": "问题生成失败，使用默认问题", "type": "news"}]


def _parse_questions(raw: str, name: str) -> list[dict]:
    try:
        data = json.loads(strip_code_fence(raw))
    except json.JSONDecodeError:
        return _default_questions(name)

    if not isinstance(data, list):
        return _default_questions(name)

    questions = []
    for item in data:
        if not isinstance(item, dict) or not item.get("query"):
            continue
        q_type = str(item.get("type", "news")).strip().lower()
        if q_type not in _VALID_TYPES:
            q_type = "news"
        questions.append({
            "query":  str(item["query"]).strip(),
            "reason": str(item.get("reason", "")).strip(),
            "type":   q_type,
        })

    return questions or _default_questions(name)


def llm_generate_questions(
    ticker: str,
    name: str,
    findings: dict,
    risk_level: str = "medium",
    hold_period: str = "mid",
    notes: str = "",
    risk_profile: str = "moderate",
) -> list[dict]:
    """返回 list[{query: str, reason: str, type: 'factual'|'news'|'opinion'}]。

    query 是实际发给搜索引擎的检索词；reason 是给人看的推理链，不会被发送出去搜索。
    """
    emit(f"🤖 为 {name} 生成研究问题...", ticker=ticker)

    pe_str    = f"{findings['pe_ttm']:.1f}x"  if findings.get('pe_ttm') else "未知"
    if findings.get("pe_percentile") is not None:
        pe_str += f"（历史分位{findings['pe_percentile']}%）"
    pb_str    = f"{findings['pb']:.2f}x"      if findings.get('pb')     else "未知"
    if findings.get("pb_percentile") is not None:
        pb_str += f"（历史分位{findings['pb_percentile']}%）"
    mcap_str  = f"{findings['mcap_yi']:.0f}亿" if findings.get('mcap_yi') else "未知"

    turnover_str  = f"{findings['turnover']}%（5日均{findings['turnover_avg5']}%）" if findings.get("turnover") else "未知"
    trend_str     = findings.get("trend_signal") or "未知"
    risk_str_calc = findings.get("risk_signal") or "未知"

    _RISK_LABEL   = {"low": "低风险（防御）", "medium": "中等风险", "high": "高风险（弹性）"}
    _PERIOD_LABEL = {"short": "短线（<1月）", "mid": "中线（1~6月）", "long": "长线（>6月）"}
    risk_str   = _RISK_LABEL.get(risk_level, risk_level)
    period_str = _PERIOD_LABEL.get(hold_period, hold_period)

    notes_line = f"\n持仓备注：{notes}" if notes else ""

    prompt = f"""你是一个股票研究助手。以下是对 {name}（{ticker}）的本地分析结果：

均线信号：{findings['ma_signal']}
量能趋势：{findings['vol_signal']}
价格偏离 MA20：{findings['ma20_deviation']}%
近5日涨跌幅：{findings['price_change_5d']}%
RSI：{findings['rsi_signal']}
MACD：{findings['macd_signal']}
KDJ：{findings['kdj_signal']}
换手率：{turnover_str}
PE(TTM)：{pe_str}
PB：{pb_str}
总市值：{mcap_str}
中期趋势：{trend_str}
风险指标：{risk_str_calc}

持仓风险偏好：{risk_str}（整体组合：{risk_profile}）
持有周期：{period_str}{notes_line}

请根据以上信号和持仓风格，生成 2~3 个需要通过公开信息检索才能回答的研究问题，输出 JSON 数组，
每个元素结构为 {{"type": "factual"|"news"|"opinion", "query": "...", "reason": "..."}}：

- type：
  - factual：需要从公告/财报中找具体数据（如营收、净利润、合同公告、合同负债）
  - news：需要从近期新闻中找（如政策变化、行业动态、公司事件）
  - opinion：需要从市场分析/行业研究中找（如竞争格局、市场地位、机构评级）
- query：**这是要发给搜索引擎的检索词**，短语/关键词风格，不超过约12个汉字，不成句，
  不出现"是否""如何""以验证""以评估""以判断"这类词。一个 query 只问一件事，
  如果本来想问两件事，拆成两个独立的问题对象，各自一个 query。
- reason：一到两句话，说明这个问题跟哪个本地信号（技术指标/估值分位/风险背离等）相关、
  为什么想查。**这部分不会被发送出去搜索**，只用于展示和综合推理，可以写得详细、带逻辑链。
- 禁止生成"综合性判断类"问题：不要问"分歧度""情绪强弱""市场共识如何"这类没有单一信源、
  需要跨多篇文章自己汇总计算的抽象指标，改问能直接搜到的原始素材（比如"机构评级调整"），
  让分歧程度之类的判断留到后续综合分析阶段自己判断。
- factual 类型的 query 也要写成关键词短语（如"五粮液 2024年报 合同负债 预收款"），
  不要写成一句话问句。

好/坏例子对照：
- 差：query="近期是否有关于五粮液核心产品普五出厂价或批发价调整的新闻，以及渠道库存水平变化，以评估RSI超买后回调压力"
- 好：query="五粮液 普五 出厂价 批发价 最新调整"，reason="RSI超买，想确认渠道是否有挺价/去库存动作，判断回调压力"
- 差：query="机构对五粮液未来12个月盈利预测下调/上调的分歧度如何"
- 好：query="五粮液 机构评级 目标价 调整"，reason="PB历史分位仅2.4%，处于历史极低水平，想确认是否有机构因基本面问题下调评级"

要求：
- 结合持仓风险偏好和持有周期聚焦重点（短线侧重催化剂，长线侧重基本面，高风险重视下行风险）
- 若"风险指标"提示实际风险与持仓风险偏好存在背离，生成的问题里应有一条聚焦这个背离点
- 不提及持仓数量和成本价

只输出 JSON 数组本身，不要 markdown 代码块，不要其他任何文字。"""

    try:
        resp = llm.chat.completions.create(
            model=MODEL_STANDARD,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=600,
            temperature=0.7,
        )
        raw       = resp.choices[0].message.content.strip()
        questions = _parse_questions(raw, name)
        emit(f"✅ {name} 生成了 {len(questions)} 个问题", ticker=ticker)
        return questions
    except Exception as e:
        emit(f"⚠️  问题生成失败（{type(e).__name__}），使用默认问题", ticker=ticker)
        return _default_questions(name)


def filter_snippets(query: str, snippets: list[str]) -> list[str]:
    """用 DeepSeek 从搜索结果中过滤掉与检索词无关的片段。"""
    if not snippets:
        return []
    joined = "\n---\n".join(f"[{i+1}] {s[:500]}" for i, s in enumerate(snippets))
    prompt = (
        f"以下是搜索「{query}」得到的片段。\n"
        "判断每个片段是否包含可以回答该问题的具体信息（不是泛泛背景）。\n"
        "只输出相关片段的编号，用英文逗号分隔，如 1,3。若全部无关则只输出：无\n\n"
        f"{joined}"
    )
    try:
        resp = llm.chat.completions.create(
            model=MODEL_STANDARD,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=20,
            temperature=0,
        )
        raw = resp.choices[0].message.content.strip()
        if raw == "无" or not raw:
            return []
        indices = [int(x.strip()) - 1 for x in raw.split(",") if x.strip().isdigit()]
        return [snippets[i] for i in indices if 0 <= i < len(snippets)]
    except Exception:
        return snippets  # 过滤失败时保留原始结果
