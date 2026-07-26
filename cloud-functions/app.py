"""
隐私确认闸门 —— v3：Agent A 全方位分析后带问题确认
=================================================
流程：
  本地 A 全方位分析 → LLM 生成研究问题 → 用户确认 → 云端 B 调研 → LLM fusion

运行：
    uvicorn app:app --reload --port 8000
"""

import asyncio
import json
import os
import threading
import urllib.request
from pathlib import Path
from typing import TypedDict, Literal
from uuid import uuid4
from datetime import datetime, timedelta

# 清除所有代理环境变量，NO_PROXY=* 告诉 requests 对任何地址都不走代理。
for _k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
           "ALL_PROXY", "all_proxy", "SOCKS_PROXY", "socks_proxy"):
    os.environ.pop(_k, None)
os.environ["NO_PROXY"] = "*"
os.environ["no_proxy"] = "*"

import pandas as pd
import requests as _req
import baostock as bs

# urllib.request 默认会读 macOS 系统代理（getproxies()），
# 装一个空的 ProxyHandler 覆盖掉，让 fetch_valuation 直连腾讯财经。
urllib.request.install_opener(
    urllib.request.build_opener(urllib.request.ProxyHandler({}))
)

from openai import OpenAI
from tavily import TavilyClient
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from langgraph.graph import StateGraph, START, END
from langgraph.types import interrupt, Command
from langgraph.checkpoint.memory import MemorySaver


# ================= 配置 =================
# 云端由 EdgeOne 注入环境变量，本地调试请 export LLM_API_KEY=... 或自建 .env loader

llm = OpenAI(
    api_key=os.environ.get("LLM_API_KEY", ""),
    base_url="https://api.deepseek.com",
)
tavily = TavilyClient(api_key=os.environ.get("TAVILY_API_KEY", ""))

# BaoStock 用自己的 socket 协议，不走 HTTP/代理，登录一次全局复用
_bs_login = bs.login()

# 按信噪比分层的域名白名单
DOMAINS_FACTUAL = [
    "cninfo.com.cn", "sse.com.cn", "szse.cn",
    "csrc.gov.cn", "cls.cn",
]
DOMAINS_NEWS = [
    "cls.cn", "yicai.com", "caixin.com",
    "eastmoney.com", "gelonghui.com",
]
DOMAINS_OPINION = [
    "xueqiu.com", "gelonghui.com", "yicai.com",
]
_DOMAIN_MAP = {
    "factual": DOMAINS_FACTUAL,
    "news":    DOMAINS_NEWS,
    "opinion": DOMAINS_OPINION,
}


# ================= 进度推送 =================
# 用 thread-local 存当前请求的回调，agent 内部调 _emit() 即可

_emitter_storage: threading.local = threading.local()

def _emit_event(event: dict) -> None:
    fn = getattr(_emitter_storage, "callback", None)
    if fn:
        fn(event)

def _emit(msg: str) -> None:
    _emit_event({"type": "progress", "text": msg})


# ================= State =================

class State(TypedDict, total=False):
    selected_tickers: list[str]   # 用户在选股面板勾选的股票代码；空列表表示全部
    agent_a_result: list[dict]
    research_questions: list[dict]
    user_decision: Literal["approved", "rejected"]
    agent_b_result: list[dict]
    fusion_result: dict


# ================= 持仓 =================

HOLDINGS_FILE = Path(__file__).parent.parent / "holdings.json"

def load_holdings() -> tuple[list[dict], str]:
    """从 holdings.json 读取持仓列表和整体风险偏好。"""
    try:
        data = json.loads(HOLDINGS_FILE.read_text(encoding="utf-8"))
        return data["holdings"], data.get("risk_profile", "moderate")
    except FileNotFoundError:
        raise RuntimeError(f"找不到持仓文件：{HOLDINGS_FILE}")
    except (KeyError, json.JSONDecodeError) as e:
        raise RuntimeError(f"持仓文件格式错误：{e}")

HOLDINGS, RISK_PROFILE = load_holdings()


# ================= 实时估值（腾讯财经 CDN，零注册）=================

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
        _emit(f"⚠️ {ticker} 估值拉取失败（{type(e).__name__}），跳过")
        return {"price": 0, "pe_ttm": 0, "pb": 0, "mcap_yi": 0}


# ================= 技术指标计算 =================

def _calc_rsi(close: "pd.Series", period: int = 14) -> float:
    delta    = close.diff()
    avg_gain = delta.clip(lower=0).rolling(period).mean().iloc[-1]
    avg_loss = (-delta.clip(upper=0)).rolling(period).mean().iloc[-1]
    if avg_loss == 0:
        return 100.0
    return float(round(100 - 100 / (1 + avg_gain / avg_loss), 1))


def _rsi_signal(rsi: float) -> str:
    if rsi >= 80:   return f"RSI={rsi}，强超买"
    if rsi >= 70:   return f"RSI={rsi}，超买"
    if rsi <= 20:   return f"RSI={rsi}，强超卖"
    if rsi <= 30:   return f"RSI={rsi}，超卖"
    if rsi >= 50:   return f"RSI={rsi}，偏强"
    return               f"RSI={rsi}，偏弱"


def _calc_macd(close: "pd.Series", fast=12, slow=26, signal=9) -> tuple[float, float, float]:
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    dif      = ema_fast - ema_slow
    dea      = dif.ewm(span=signal, adjust=False).mean()
    hist     = (dif - dea) * 2
    return round(float(dif.iloc[-1]), 4), round(float(dea.iloc[-1]), 4), round(float(hist.iloc[-1]), 4)


def _macd_signal(dif: float, dea: float, hist: float, prev_hist: float) -> str:
    cross = "DIF上穿DEA金叉" if dif > dea else "DIF下穿DEA死叉"
    trend = "柱状图扩大" if abs(hist) > abs(prev_hist) else "柱状图收缩"
    zone  = "零轴上方" if dif > 0 else "零轴下方"
    return f"{cross}，{trend}，{zone}"


def _calc_kdj(df: "pd.DataFrame", period: int = 9) -> tuple[float, float, float]:
    low_n  = df["最低"].rolling(period).min()
    high_n = df["最高"].rolling(period).max()
    rsv    = (df["收盘"] - low_n) / (high_n - low_n + 1e-8) * 100
    k      = rsv.ewm(com=2, adjust=False).mean()   # alpha=1/3
    d      = k.ewm(com=2, adjust=False).mean()
    j      = 3 * k - 2 * d
    return round(float(k.iloc[-1]), 1), round(float(d.iloc[-1]), 1), round(float(j.iloc[-1]), 1)


def _kdj_signal(k: float, d: float, j: float, prev_k: float, prev_d: float) -> str:
    cross = ""
    if prev_k <= prev_d and k > d:
        cross = "K/D金叉，"
    elif prev_k >= prev_d and k < d:
        cross = "K/D死叉，"
    overbought = "J超买（>90）" if j > 90 else ("J超卖（<10）" if j < 10 else "")
    return f"{cross}K={k} D={d} J={j}" + (f"，{overbought}" if overbought else "")


# ================= 本地分析 =================

def analyze_stock(h: dict) -> dict:
    item   = dict(h)
    ticker = h["ticker"]
    name   = h["name"]

    _emit(f"💹 拉取 {name}（{ticker}）实时价格…")
    valuation = fetch_valuation(ticker)
    item.update({
        "price":   valuation["price"],
        "mcap_yi": valuation["mcap_yi"],
        "pe_ttm":  valuation["pe_ttm"],   # 占位，BaoStock 成功后会覆盖
        "pb":      valuation["pb"],
    })
    if valuation["price"]:
        _emit(f"  当前价={valuation['price']}  市值={valuation['mcap_yi']:.0f}亿")

    _emit(f"📡 拉取 {name}（{ticker}）历史行情（BaoStock）…")

    try:
        prefix = "sh" if ticker.startswith(("6", "9")) else ("bj" if ticker.startswith("8") else "sz")
        end    = datetime.now().strftime("%Y-%m-%d")
        start  = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")

        rs = bs.query_history_k_data_plus(
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
        _emit(f"  PE(TTM)={last_pe:.1f}x  PB={last_pb:.2f}x  市值={valuation['mcap_yi']:.0f}亿")

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
        rsi        = _calc_rsi(df["收盘"])
        rsi_sig    = _rsi_signal(rsi)

        # ── MACD ──
        dif, dea, hist     = _calc_macd(df["收盘"])
        prev_dif, prev_dea, prev_hist = _calc_macd(df["收盘"].iloc[:-1])
        macd_sig   = _macd_signal(dif, dea, hist, prev_hist)

        # ── KDJ ──
        k, d, j             = _calc_kdj(df)
        prev_k, prev_d, _   = _calc_kdj(df.iloc[:-1])
        kdj_sig    = _kdj_signal(k, d, j, prev_k, prev_d)

        # ── 换手率 ──
        turnover     = round(float(last["换手率"]), 2)
        turnover_avg = round(float(df["换手率"].iloc[-6:-1].mean()), 2)

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
        })

        _emit(f"📈 {name}：{ma_signal}，{vol_signal}，近5日 {item['price_change_5d']:+.1f}%")
        _emit(f"  {rsi_sig}  |  MACD {macd_sig}  |  KDJ {kdj_sig}")
        _emit(f"  换手率 {turnover}%（5日均 {turnover_avg}%）")

    except Exception as e:
        _emit(f"⚠️  {name} 行情获取失败（{type(e).__name__}），使用模拟数据")
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
        })

    return item


# ================= LLM①：生成研究问题 =================

def llm_generate_questions(
    ticker: str,
    name: str,
    findings: dict,
    risk_level: str = "medium",
    hold_period: str = "mid",
    notes: str = "",
    risk_profile: str = "moderate",
) -> list[dict]:
    """返回 list[{question: str, type: 'factual'|'news'|'opinion'}]"""
    _emit(f"🤖 为 {name} 生成研究问题...")

    pe_str    = f"{findings['pe_ttm']:.1f}x"  if findings.get('pe_ttm') else "未知"
    pb_str    = f"{findings['pb']:.2f}x"      if findings.get('pb')     else "未知"
    mcap_str  = f"{findings['mcap_yi']:.0f}亿" if findings.get('mcap_yi') else "未知"

    turnover_str = f"{findings['turnover']}%（5日均{findings['turnover_avg5']}%）" if findings.get("turnover") else "未知"

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

持仓风险偏好：{risk_str}（整体组合：{risk_profile}）
持有周期：{period_str}{notes_line}

请根据以上信号和持仓风格，生成 2~3 个需要通过公开信息检索才能回答的研究问题。

每个问题必须标注类型，格式为"类型: 问题内容"，每行一个，不加序号：
- factual: 需要从公告/财报中找具体数据的问题（如营收、净利润、合同公告）
- news: 需要从近期新闻中找的问题（如政策变化、行业动态、公司事件）
- opinion: 需要从市场分析/行业研究中找的问题（如竞争格局、市场地位）

要求：
- 问题具体可验证，紧扣上面的信号
- 结合持仓风险偏好和持有周期聚焦重点（短线侧重催化剂，长线侧重基本面，高风险重视下行风险）
- 不提及持仓数量和成本价

只输出带类型前缀的问题行，不要其他内容。"""

    try:
        resp = llm.chat.completions.create(
            model="deepseek-chat",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=400,
            temperature=0.7,
        )
        raw = resp.choices[0].message.content.strip()
        questions = []
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            if ":" in line:
                q_type, q_text = line.split(":", 1)
                q_type = q_type.strip().lower()
                if q_type not in ("factual", "news", "opinion"):
                    q_type = "news"
                questions.append({"question": q_text.strip(), "type": q_type})
            else:
                questions.append({"question": line, "type": "news"})
        _emit(f"✅ {name} 生成了 {len(questions)} 个问题")
        return questions
    except Exception as e:
        _emit(f"⚠️  问题生成失败（{type(e).__name__}），使用默认问题")
        return [{"question": f"{name} 近期有无重大消息面驱动？", "type": "news"}]


def filter_snippets(question: str, snippets: list[str]) -> list[str]:
    """用 DeepSeek 从搜索结果中过滤掉与问题无关的片段。"""
    if not snippets:
        return []
    joined = "\n---\n".join(f"[{i+1}] {s[:500]}" for i, s in enumerate(snippets))
    prompt = (
        f"以下是搜索「{question}」得到的片段。\n"
        "判断每个片段是否包含可以回答该问题的具体信息（不是泛泛背景）。\n"
        "只输出相关片段的编号，用英文逗号分隔，如 1,3。若全部无关则只输出：无\n\n"
        f"{joined}"
    )
    try:
        resp = llm.chat.completions.create(
            model="deepseek-chat",
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


# ================= Agent B 结构化数据采集 =================

def _akshare_research(ticker: str, name: str) -> dict:
    """
    云端版：akshare 包体过大（~80MB）已从云端依赖中移除。
    题材归因仍走同花顺 HTTP，新闻和 EPS 由 Tavily 覆盖。
    """
    result: dict = {"ticker": ticker, "name": name, "news": [], "hot_reason": None, "consensus_eps": []}

    def cp(text: str) -> None:
        _emit_event({"type": "cloud_progress", "text": text})

    # 题材归因（同花顺强势股榜，无需 akshare）
    cp(f"🔥 {name} 拉取题材归因…")
    try:
        url = "http://zx.10jqka.com.cn/event/api/getharden/date//orderby/date/orderway/desc/charset/GBK/"
        r   = _req.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10, proxies={})
        rows = r.json().get("data") or []
        hit  = [x for x in rows if x.get("code") == ticker]
        result["hot_reason"] = hit[0].get("reason") if hit else None
        cp(f"  ✅ 题材：{result['hot_reason'] or '今日未上强势股榜'}")
    except Exception as e:
        cp(f"  ⚠️ 题材归因拉取失败（{type(e).__name__}）")

    return result


# ================= Agent A =================

def agent_a_node(state: State) -> dict:
    selected = state.get("selected_tickers") or []
    holdings = [h for h in HOLDINGS if not selected or h["ticker"] in selected]
    _emit(f"📋 分析 {len(holdings)} 支股票（持仓共 {len(HOLDINGS)} 支）")

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
        }

        questions = llm_generate_questions(
            analysis["ticker"],
            analysis["name"],
            findings,
            risk_level=analysis.get("risk_level", "medium"),
            hold_period=analysis.get("hold_period", "mid"),
            notes=analysis.get("notes", ""),
            risk_profile=RISK_PROFILE,
        )

        research_questions.append({
            "ticker":        analysis["ticker"],
            "name":          analysis["name"],
            "industry_tags": analysis["industry_tags"],
            "risk_level":    analysis.get("risk_level", "medium"),
            "hold_period":   analysis.get("hold_period", "mid"),
            "notes":         analysis.get("notes", ""),
            "findings":      findings,
            "questions":     questions,
        })

    _emit("🎯 本地分析完成，等待你确认")

    return {
        "agent_a_result":     full_result,
        "research_questions": research_questions,
    }


# ================= 确认节点 =================

def human_confirm_node(state: State) -> dict:
    decision = interrupt({
        "research_questions": state["research_questions"],
    })
    return {"user_decision": decision["action"]}


# ================= 路由 =================

def route_after_confirm(state: State) -> str:
    return "agent_b" if state["user_decision"] == "approved" else END


# ================= Agent B：结构化主力 + Tavily 补充 =================

def agent_b_node(state: State) -> dict:
    _emit_event({"type": "cloud_progress", "text": f"☁️ 开始云端调研，共 {len(state['research_questions'])} 支股票"})
    results = []

    for item in state["research_questions"]:
        ticker = item["ticker"]
        name   = item["name"]

        # ── 第一层：结构化数据（主力信源）──
        _emit_event({"type": "cloud_progress", "text": f"🗂 {name} 开始拉取结构化数据…"})
        structured = _akshare_research(ticker, name)

        # 把结构化结果作为可展开卡片推给前端
        _emit_event({
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
            q_text  = q_obj["question"]
            q_type  = q_obj.get("type", "opinion")
            domains = _DOMAIN_MAP.get(q_type, DOMAINS_OPINION)

            raw_snippets: list[str] = []
            try:
                resp = tavily.search(
                    query=f"{name} {q_text}",
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
                _emit_event({"type": "cloud_progress", "text": f"⚠️ 搜索失败（{type(e).__name__}）"})

            # 兜底：白名单没搜到就放开域名限制
            if not raw_snippets:
                try:
                    resp = tavily.search(
                        query=f"{name} {q_text}",
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

            filtered = filter_snippets(q_text, raw_snippets) if raw_snippets else []

            _emit_event({
                "type":      "search_result",
                "name":      name,
                "question":  q_text,
                "q_type":    q_type,
                "snippets":  [s[:400] for s in filtered],
                "raw_count": len(raw_snippets),
            })
            qa_list.append({"question": q_text, "snippets": filtered})

        # factual / news 问题记录一下（结构化已覆盖，不再单独搜索）
        for q_obj in other_questions:
            qa_list.append({"question": q_obj["question"], "snippets": [], "covered_by": "structured"})

        results.append({
            "ticker":     ticker,
            "name":       name,
            "structured": structured,
            "qa":         qa_list,
        })

    _emit_event({"type": "cloud_progress", "text": "✅ 云端调研完成"})
    return {"agent_b_result": results}


# ================= LLM②：fusion =================

def fusion_node(state: State) -> dict:
    # Signal that cloud results are returning to local for synthesis
    _emit_event({"type": "return_arrow"})
    _emit("🧠 DeepSeek 综合分析中…")

    # 构建脱敏提示，严禁传入 qty / cost
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

            # Tavily 补充（opinion 类）
            for qa in b_map[ticker].get("qa", []):
                if qa.get("covered_by") == "structured":
                    continue
                q       = qa["question"]
                summary = " ".join(qa["snippets"])[:300] if qa["snippets"] else "无相关信息"
                tavily_text += f"  问：{q}\n  网搜摘要：{summary}\n\n"

        cloud_section = ""
        if structured_text:
            cloud_section += f"结构化数据：\n{structured_text}\n"
        if tavily_text:
            cloud_section += f"网络搜索补充：\n{tavily_text}"

        sections.append(
            f"【{name}（{ticker}）】\n"
            f"本地技术信号：\n{findings}\n\n"
            f"云端数据：\n{cloud_section or '  无'}"
        )

    prompt = (
        "你是一位严谨的 A 股研究分析师。请根据以下每只股票的本地技术信号和云端调研信息，"
        "给出综合投资建议。\n\n"
        "要求：\n"
        "- 每只股票给出：①一句话综合判断 ②建议操作（持有/减仓/加仓/观望）③一条主要风险提示\n"
        "- 不要提及持仓数量和成本价\n"
        "- 建议要有依据，紧扣具体信号\n\n"
        + "\n\n---\n\n".join(sections)
    )

    try:
        resp   = llm.chat.completions.create(
            model="deepseek-chat",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=800,
            temperature=0.5,
        )
        advice = resp.choices[0].message.content.strip()
        _emit("✅ 综合建议生成完毕")
    except Exception as e:
        _emit(f"⚠️  建议生成失败（{type(e).__name__}）")
        advice = "（建议生成失败，请检查 LLM_API_KEY）"

    return {"fusion_result": {
        "advice":     advice,
        "cloud_info": state["agent_b_result"],
    }}


# ================= 组图 =================

def build_graph():
    g = StateGraph(State)
    g.add_node("agent_a",       agent_a_node)
    g.add_node("human_confirm", human_confirm_node)
    g.add_node("agent_b",       agent_b_node)
    g.add_node("fusion",        fusion_node)

    g.add_edge(START, "agent_a")
    g.add_edge("agent_a", "human_confirm")
    g.add_conditional_edges(
        "human_confirm", route_after_confirm,
        {"agent_b": "agent_b", END: END},
    )
    g.add_edge("agent_b", "fusion")
    g.add_edge("fusion", END)
    return g.compile(checkpointer=MemorySaver())


research_graph = build_graph()

# 存储每个 thread 的进度队列，供 /research/watch 消费
_watch_queues: dict[str, asyncio.Queue] = {}


# ================= FastAPI =================

app = FastAPI(title="隐私确认闸门 demo")
BASE_DIR = Path(__file__).parent.parent


@app.get("/")
def index():
    return FileResponse(BASE_DIR / "index.html")


@app.get("/holdings")
def get_holdings_list():
    """返回持仓展示字段（不含 qty/cost）供前端选股面板使用。"""
    return [
        {
            "ticker":        h["ticker"],
            "name":          h["name"],
            "industry_tags": h.get("industry_tags", []),
            "risk_level":    h.get("risk_level", "medium"),
            "hold_period":   h.get("hold_period", "mid"),
            "notes":         h.get("notes", ""),
        }
        for h in HOLDINGS
    ]


@app.get("/research/stream")
async def stream_research(tickers: str = ""):
    """
    SSE 接口：Agent A 运行时实时推送进度，完成后推送 ready 事件。
    tickers: 逗号分隔的股票代码，空字符串表示分析全部持仓。
    """
    selected = [t.strip() for t in tickers.split(",") if t.strip()] if tickers else []
    thread_id = str(uuid4())
    config    = {"configurable": {"thread_id": thread_id}}
    loop      = asyncio.get_event_loop()
    queue: asyncio.Queue = asyncio.Queue()

    def run() -> None:
        def callback(event: dict) -> None:
            asyncio.run_coroutine_threadsafe(queue.put(event), loop)
        _emitter_storage.callback = callback

        try:
            result  = research_graph.invoke({"selected_tickers": selected}, config=config)
            preview = result["__interrupt__"][0].value
            asyncio.run_coroutine_threadsafe(
                queue.put({"type": "ready", "thread_id": thread_id, "preview": preview}),
                loop,
            )
        except Exception as e:
            asyncio.run_coroutine_threadsafe(
                queue.put({"type": "error", "text": str(e)}), loop
            )
        finally:
            _emitter_storage.callback = None

    threading.Thread(target=run, daemon=True).start()

    async def generate():
        while True:
            item = await queue.get()
            yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"
            if item["type"] in ("ready", "error"):
                break

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


class ConfirmReq(BaseModel):
    action: str  # "approved" | "rejected"


@app.post("/research/confirm/{thread_id}")
async def confirm_research(thread_id: str, req: ConfirmReq):
    """触发后台继续执行，立即返回。前端随后连接 /research/watch/{thread_id} 获取进度。"""
    loop  = asyncio.get_event_loop()
    queue: asyncio.Queue = asyncio.Queue()
    _watch_queues[thread_id] = queue
    config = {"configurable": {"thread_id": thread_id}}

    def run() -> None:
        if req.action == "rejected":
            # 只需恢复图让它走到 END，无需调用 B/fusion
            try:
                research_graph.invoke(Command(resume=req.model_dump()), config=config)
            except Exception:
                pass
            asyncio.run_coroutine_threadsafe(
                queue.put({"type": "cancelled"}), loop
            )
            return

        def callback(event: dict) -> None:
            asyncio.run_coroutine_threadsafe(queue.put(event), loop)
        _emitter_storage.callback = callback
        try:
            result = research_graph.invoke(Command(resume=req.model_dump()), config=config)
            asyncio.run_coroutine_threadsafe(
                queue.put({"type": "done", "fusion_result": result.get("fusion_result")}), loop
            )
        except Exception as e:
            asyncio.run_coroutine_threadsafe(
                queue.put({"type": "error", "text": str(e)}), loop
            )
        finally:
            _emitter_storage.callback = None

    threading.Thread(target=run, daemon=True).start()
    return {"status": "running"}


@app.get("/research/watch/{thread_id}")
async def watch_research(thread_id: str):
    """SSE：推送 Agent B + fusion 阶段的实时进度。"""
    queue = _watch_queues.get(thread_id)
    if not queue:
        raise HTTPException(status_code=404, detail="thread not found")

    async def generate():
        while True:
            item = await queue.get()
            yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"
            if item["type"] in ("done", "cancelled", "error"):
                _watch_queues.pop(thread_id, None)
                break

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
