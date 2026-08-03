"""Agent B 结构化数据采集：只吃 ticker，不碰任何持仓数据。

三路全部零注册、免费接口，各自独立 try/except。
进度事件全部用 cloud_progress，显示在云端列。
"""

from . import config  # noqa: F401  先清代理环境变量，再 import akshare

import requests as _req

from .progress import emit_event

try:
    import akshare as ak
    AKSHARE_AVAILABLE = True
except ImportError:
    ak = None
    AKSHARE_AVAILABLE = False


def akshare_research(ticker: str, name: str) -> dict:
    result: dict = {"ticker": ticker, "name": name, "news": [], "hot_reason": None, "consensus_eps": []}

    def cp(text: str) -> None:
        emit_event({"type": "cloud_progress", "text": text, "ticker": ticker})

    # ① 个股新闻（东方财富，需要 akshare）
    if AKSHARE_AVAILABLE:
        cp(f"📰 {name} 拉取个股新闻…")
        try:
            df = ak.stock_news_em(symbol=ticker)
            result["news"] = df[["发布时间", "新闻标题", "新闻内容"]].head(5).to_dict("records") if not df.empty else []
            cp(f"  ✅ 新闻 {len(result['news'])} 条")
        except Exception as e:
            cp(f"  ⚠️ 新闻拉取失败（{type(e).__name__}）")

    # ② 题材归因（同花顺强势股榜，HTTP 无 TLS，代理影响小）
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

    # ③ 分析师一致预期 EPS（同花顺，需要 akshare）
    if AKSHARE_AVAILABLE:
        cp(f"📊 {name} 拉取分析师一致预期…")
        try:
            df = ak.stock_profit_forecast_ths(symbol=ticker, indicator="预测年报每股收益")
            result["consensus_eps"] = df.head(3).to_dict("records") if not df.empty else []
            cp(f"  ✅ 预期数据 {len(result['consensus_eps'])} 条")
        except Exception as e:
            cp(f"  ⚠️ 一致预期拉取失败（{type(e).__name__}）")

    return result
