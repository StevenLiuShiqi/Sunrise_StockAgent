# Sunrise 技术文档

> 一次完整分析的全流程技术说明。按照实际执行顺序编排，每一步都结合具体的代码实现。

---

## 目录

1. [整体架构](#整体架构)
2. [第一步：读取持仓数据](#第一步读取持仓数据)
3. [第二步：选股界面](#第二步选股界面)
4. [第三步：Agent A 本地分析](#第三步agent-a-本地分析)
5. [第四步：LLM 生成研究问题](#第四步llm-生成研究问题)
6. [第五步：隐私确认闸门](#第五步隐私确认闸门)
7. [第六步：Agent B 云端调研](#第六步agent-b-云端调研)
8. [第七步：DeepSeek 综合建议](#第七步deepseek-综合建议)
9. [第八步：结果展示](#第八步结果展示)
10. [流程编排：LangGraph](#流程编排langgraph)
11. [实时进度推送：SSE](#实时进度推送sse)
12. [隐私保障机制](#隐私保障机制)

---

## 整体架构

```
本地浏览器
    │
    ├── index.html          前端界面（纯 HTML/CSS/JS，无框架）
    │
    └── FastAPI (app.py)    后端服务，uvicorn 驱动
            │
            ├── Agent A     本地分析（BaoStock + 腾讯财经 + 技术指标）
            ├── 闸门        用户确认（LangGraph interrupt）
            ├── Agent B     云端调研（AKShare + Tavily）
            └── Fusion      综合建议（DeepSeek LLM）
```

后端框架：**FastAPI**，异步 ASGI 服务。  
流程编排：**LangGraph**（StateGraph），原生支持人工中断/恢复。  
前后端通信：**Server-Sent Events (SSE)**，实时推送每一步进度。

---

## 第一步：读取持仓数据

分析开始前，系统从本地 `holdings.json` 读取持仓信息。这个文件由用户自行维护，不会上传到任何地方。

**文件结构（`holdings.json`）：**

```json
{
  "risk_profile": "moderate",
  "holdings": [
    {
      "ticker": "600570",
      "name": "恒生电子",
      "qty": 3000,
      "cost": 34.20,
      "industry_tags": ["金融科技", "软件"],
      "risk_level": "medium",
      "hold_period": "long",
      "notes": "核心仓位，看好金融IT国产化替代逻辑"
    }
  ]
}
```

| 字段 | 说明 | 是否出本地 |
|------|------|----------|
| `ticker` / `name` | 股票代码和名称 | ✅ 会传给 LLM 和云端检索 |
| `qty` | 持仓数量 | ❌ 绝不出本地 |
| `cost` | 成本价 | ❌ 绝不出本地 |
| `risk_level` | 个股风险偏好（low/medium/high） | 仅传给本地 LLM 生成问题 |
| `hold_period` | 持有周期（short/mid/long） | 仅传给本地 LLM 生成问题 |
| `notes` | 投资备注 | 仅传给本地 LLM 生成问题 |

读取代码在 `load_holdings()`（`app.py:122`），服务启动时执行一次，结果存入全局变量 `HOLDINGS`。  
前端通过 `GET /holdings` 接口拿到展示字段（该接口不返回 `qty` 和 `cost`）。

---

## 第二步：选股界面

前端加载完成后，立即请求 `GET /holdings`，渲染选股卡片。每张卡片显示：股票代码、名称、行业标签、风险级别（色码徽章）、持有周期、备注摘要。

用户可以：
- 单击卡片切换选中状态
- 点击"全选"一键勾选所有持仓
- 点击"开始分析（N 支）"启动流程

确认后，前端用 **EventSource** 连接 `GET /research/stream?tickers=600570,000858,...`，开始接收实时进度事件。

---

## 第三步：Agent A 本地分析

`agent_a_node()`（`app.py:505`）对每支选中的股票依次执行以下分析。所有数据都在本地处理，不经过任何云端。

### 3.1 实时估值 — 腾讯财经 CDN

调用 `fetch_valuation()`（`app.py:137`），从腾讯财经行情 CDN 拉取实时数据：

```
https://qt.gtimg.cn/q=sh600570
```

返回内容是 `~` 分隔的字符串，解析出：

| 字段 | 位置（vals 索引） |
|------|----------------|
| 最新价（price） | vals[3] |
| PE(TTM) | vals[39] |
| PB | vals[46] |
| 总市值（亿） | vals[44] |

接口无需注册，零延迟，内容为 GBK 编码，用 `requests` 直接请求，明确传 `proxies={}` 绕过系统代理。

### 3.2 历史行情 — BaoStock

调用 `bs.query_history_k_data_plus()`，拉取近 **90 天** 日线数据：

```python
bs.query_history_k_data_plus(
    "sh.600570",
    "date,close,high,low,volume,turn,peTTM,pbMRQ",
    start_date=..., end_date=...,
    frequency="d", adjustflag="2",   # 后复权
)
```

**BaoStock 的特点**：使用私有 socket 协议（非 HTTP），不走系统代理，无注册门槛，数据稳定。返回的 PE/PB 是日频历史值，比腾讯财经的实时估算更准，会覆盖第一步的占位数据。

### 3.3 技术指标计算

基于 BaoStock 返回的日线数据，用 **pandas** 在本地计算四类指标：

**均线信号（MA5 / MA20）**  
判断金叉/死叉，以及现价对 MA20 的偏离度。

**RSI(14)** — `_calc_rsi()`（`app.py:161`）  
```python
avg_gain = delta.clip(lower=0).rolling(14).mean().iloc[-1]
avg_loss = (-delta.clip(upper=0)).rolling(14).mean().iloc[-1]
rsi = 100 - 100 / (1 + avg_gain / avg_loss)
```
生成文字信号：超买（>70）/ 超卖（<30）/ 中性。

**MACD(12/26/9)** — `_calc_macd()`（`app.py:179`）  
用 EWM 计算 DIF、DEA、MACD 柱；与前一根柱对比判断金叉/死叉/持续。

**KDJ(9)** — `_calc_kdj()`（`app.py:195`）  
用 EWM（α=1/3，即 `com=2`）计算 K、D、J 值；J>90 超买，J<10 超卖。

**换手率**  
直接从 BaoStock 的 `turn` 字段取值，与 5 日均值对比判断缩量/放量。

> **序列化注意**：pandas / numpy 的运算结果是 `numpy.float64` 类型，LangGraph 用 msgpack 序列化状态时会报错。所有数值在存入 State 前都显式包裹 `float()`。

---

## 第四步：LLM 生成研究问题

`llm_generate_questions()`（`app.py:346`）把本地技术信号整理成 prompt，调用 **DeepSeek** 生成 2~3 个研究问题：

**模型**：`deepseek-chat`，兼容 OpenAI 接口。  
**Prompt 要素**：均线/量能/RSI/MACD/KDJ/估值信号 + 持仓风险偏好 + 持有周期 + 用户备注。

问题被强制分为三种类型，决定后续去哪里找答案：

| 类型 | 含义 | 后续处理 |
|------|------|---------|
| `factual` | 需要从公告/财报找具体数据 | 由结构化数据覆盖 |
| `news` | 需要从近期新闻找 | 由结构化数据覆盖 |
| `opinion` | 需要市场分析/研究报告 | 由 Tavily 搜索覆盖 |

输出格式：`类型: 问题内容`，每行一个，解析后存为 `list[{question, type}]`。

---

## 第五步：隐私确认闸门

这是整个流程的核心设计。Agent A 完成后，LangGraph 通过 `interrupt()` **原地暂停**图的执行，等待用户确认：

```python
# human_confirm_node()
decision = interrupt({
    "research_questions": state["research_questions"],
    ...
})
```

前端收到 `ready` 事件后，渲染"隐私确认闸门"UI：
- 每支股票一个折叠块，展示技术信号摘要
- 每个研究问题标注类型标签（公告 / 新闻 / 分析）
- 显示持仓的风险偏好和持有周期徽章
- 显示用户备注

用户有两个选择：
- **确认发送** → `POST /research/confirm/{thread_id}` with `{"action": "approved"}`，LangGraph 从断点继续执行，进入 Agent B
- **取消** → 整个流程终止，不发送任何数据到云端

---

## 第六步：Agent B 云端调研

用户确认后，`agent_b_node()`（`app.py:578`）对每支股票执行两层数据收集。

### 6.1 结构化数据层（首选）

`_akshare_research()`（`app.py:459`）通过公开 HTTP 接口拉取：

**① 个股新闻**（`akshare.stock_news_em`）  
东方财富个股新闻接口，取最新 5 条，字段：发布时间 / 新闻标题 / 新闻内容。

**② 题材归因**（同花顺强势股 API）  
```
http://zx.10jqka.com.cn/event/api/getharden/...
```
用股票代码匹配当日强势股榜，取 `reason` 字段。无 TLS，不依赖 akshare。

**③ 分析师一致预期 EPS**（`akshare.stock_profit_forecast_ths`）  
同花顺预期 EPS 数据，取最近 3 条机构预测。

所有结构化数据的进度事件类型为 `cloud_progress`，在前端显示在云端列。

### 6.2 Tavily 网络搜索层（补充）

**只有 `opinion` 类型的问题才走 Tavily**——公告和新闻类问题已由结构化数据覆盖，不浪费搜索额度。

**域名白名单**按问题类型分层，过滤噪声站点：

```python
DOMAINS_OPINION = ["xueqiu.com", "gelonghui.com", "yicai.com"]
```

搜索参数：`search_depth="advanced"`，`days=30`（最近 30 天）。如果带白名单搜索结果为空，自动 fallback 到无限制搜索，`days=60`。

**LLM 二次过滤**（`filter_snippets()`，`app.py:430`）  
把搜到的摘要发给 DeepSeek，让它只返回相关条目的序号，剔除广告和无关内容：
```
max_tokens=20, temperature=0
```
返回 `无` 则清空结果，避免噪声进入 fusion。

---

## 第七步：DeepSeek 综合建议

`fusion_node()`（`app.py:671`）把所有信息汇总，构建最终 prompt：

**每支股票包含：**
- 本地技术信号（均线 / 量能 / RSI / MACD / KDJ）
- 结构化数据（近期新闻标题 / 题材归因 / 分析师预期 EPS）
- Tavily 搜索摘要（仅 opinion 类）

**严格过滤**：`qty`（持仓数量）和 `cost`（成本价）字段**从未**进入任何传给 LLM 的字符串。

**输出要求**：  
每只股票给出①一句话综合判断、②建议操作（持有/减仓/加仓/观望）、③主要风险提示。

模型：`deepseek-chat`，`max_tokens=800`，`temperature=0.5`。

---

## 第八步：结果展示

Fusion 完成后，前端收到 `done` 事件，用 **marked.js** 把 DeepSeek 返回的 Markdown 渲染成 HTML：

```javascript
const adviceHtml = marked.parse(r.advice || '（无建议）')
```

支持：`## 标题`、`**粗体**`、`- 列表`、`---` 分隔线。

CSS 对渲染出来的 Markdown 元素单独定制样式（`.result-advice` 选择器），与整体视觉一致。

---

## 流程编排：LangGraph

整个分析流程用 **LangGraph** 的 `StateGraph` 定义为一张有向图：

```
START → agent_a → human_confirm → agent_b → fusion → END
                       ↓
                    (rejected) → END
```

**State 结构**（`app.py:109`）：

```python
class State(TypedDict, total=False):
    selected_tickers: list[str]      # 用户选中的股票代码
    agent_a_result: list[dict]       # 本地分析结果
    research_questions: list[dict]   # 待确认的研究问题
    user_decision: str               # "approved" | "rejected"
    agent_b_result: list[dict]       # 云端调研结果
    fusion_result: dict              # 最终建议
```

**中断机制**：`human_confirm_node` 调用 `interrupt()`，LangGraph 将当前 State 序列化（msgpack）存入 `MemorySaver`（内存 checkpointer），等待外部 `Command(resume=...)` 恢复。`thread_id` 是每次分析的唯一标识，用于定位 checkpointer 中的对应状态。

---

## 实时进度推送：SSE

前端和后端之间的实时通信全部通过 **Server-Sent Events** 完成，无 WebSocket，无轮询。

**两个 SSE 端点：**

| 端点 | 用途 | 事件类型 |
|------|------|---------|
| `GET /research/stream` | Agent A 阶段 | `progress`、`ready`、`error` |
| `GET /research/watch/{thread_id}` | Agent B + Fusion 阶段 | `cloud_progress`、`structured_result`、`search_result`、`return_arrow`、`progress`、`done`、`error` |

**线程桥接**（`app.py:96`）：LangGraph 在同步线程里运行，FastAPI 的 SSE 是异步的。用 `threading.local` 存当前请求的回调函数，用 `asyncio.run_coroutine_threadsafe` 把同步线程里产生的事件安全地推入 `asyncio.Queue`，再由异步生成器逐条 yield 出去。

---

## 隐私保障机制

| 数据 | 本地计算 | 传给 LLM | 发往云端 |
|------|---------|---------|---------|
| 股票代码/名称 | ✅ | ✅ | ✅ |
| 持仓数量（qty） | ✅ | ❌ | ❌ |
| 成本价（cost） | ✅ | ❌ | ❌ |
| 技术指标信号 | ✅ | ✅（文字描述） | ❌ |
| 研究问题 | 本地生成 | ✅（生成时用） | ✅（搜索用） |
| 风险偏好/备注 | ✅ | ✅（生成问题时） | ❌ |

保障方式不靠承诺：`qty` 和 `cost` 只出现在 `analyze_stock()` 里用于计算盈亏，不写入任何传给 LLM 或 Tavily 的字符串变量。`fusion_node()` 构建 prompt 时只从 `agent_a_result` 的技术信号字段取值，没有读取 qty/cost 的代码路径。
