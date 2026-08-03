"""
隐私确认闸门 —— v3：Agent A 全方位分析后带问题确认
=================================================
流程：
  本地 A 全方位分析 → LLM 生成研究问题 → 用户确认 → 云端 B 调研 → LLM fusion

核心逻辑在 backend/ 包中按功能拆分（配置、行情、指标、LLM、云端调研、LangGraph 编排）；
这个文件只负责 FastAPI 路由和 SSE 推送的编排。

运行：
    uvicorn app:app --reload --port 8000
"""

import asyncio
import json
import threading
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from langgraph.types import Command
from pydantic import BaseModel

from backend.graph import research_graph
from backend.holdings import HOLDINGS
from backend.progress import emitter_storage

app = FastAPI(title="隐私确认闸门 demo")
BASE_DIR = Path(__file__).parent
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

# 存储每个 thread 的进度队列，供 /research/watch 消费
_watch_queues: dict[str, asyncio.Queue] = {}


@app.get("/")
def index():
    return FileResponse(BASE_DIR / "index.html")


@app.get("/holdings")
def get_holdings_list():
    """返回持仓展示字段供前端选股面板使用（含 qty/cost）。"""
    return [
        {
            "ticker":        h["ticker"],
            "name":          h["name"],
            "qty":           h.get("qty", 0),
            "cost":          h.get("cost", 0),
            "industry_tags": h.get("industry_tags", []),
            "risk_level":    h.get("risk_level", "medium"),
            "hold_period":   h.get("hold_period", "mid"),
            "notes":         h.get("notes", ""),
        }
        for h in HOLDINGS
    ]


@app.get("/research/stream")
async def stream_research(tickers: str = "", deep: bool = False):
    """
    SSE 接口：Agent A 运行时实时推送进度，完成后推送 ready 事件。
    tickers: 逗号分隔的股票代码，空字符串表示分析全部持仓。
    deep: 是否开启深度辩论模式（看多/看空研究员 + 风险管理层），关闭时走单次 fusion 轻量流程。
    """
    selected = [t.strip() for t in tickers.split(",") if t.strip()] if tickers else []
    thread_id = str(uuid4())
    config    = {"configurable": {"thread_id": thread_id}}
    loop      = asyncio.get_event_loop()
    queue: asyncio.Queue = asyncio.Queue()

    def run() -> None:
        def callback(event: dict) -> None:
            asyncio.run_coroutine_threadsafe(queue.put(event), loop)
        emitter_storage.callback = callback

        try:
            result  = research_graph.invoke(
                {"selected_tickers": selected, "deep_mode": deep}, config=config
            )
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
            emitter_storage.callback = None

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
        emitter_storage.callback = callback
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
            emitter_storage.callback = None

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
