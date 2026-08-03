/* ── refs ── */
const stockPickerBar = document.getElementById('stockPickerBar');
const chartTicker    = document.getElementById('chartTicker');
const chartPrice     = document.getElementById('chartPrice');
const chartContainer = document.getElementById('chartContainer');
const chartEmpty     = document.getElementById('chartEmpty');
const orderQty       = document.getElementById('orderQty');
const buyBtn         = document.getElementById('buyBtn');
const sellBtn        = document.getElementById('sellBtn');
const orderMsg       = document.getElementById('orderMsg');
const positionsTable = document.querySelector('#positionsTable tbody');
const ordersTable    = document.querySelector('#ordersTable tbody');
const aiFeed         = document.getElementById('aiFeed');

let selectedTicker  = null;
let chart           = null;
let candleSeries    = null;
let volumeSeries    = null;
let latestBar       = null;
let quotePollTimer  = null;
let currentSSE      = null;
let currentWatcher  = null;

/* AI 助理阶段状态：本地分析 → 隐私闸门 → 云端调研 → 本地综合，每个阶段一张卡片 */
const AI_STEPS = ['local', 'gate', 'cloud', 'fusion'];
let localCard  = null;
let cloudCard  = null;
let fusionCard = null;

/* ── small helpers ── */
function rollingMean(values, period) {
  const out = [];
  for (let i = 0; i < values.length; i++) {
    if (i < period - 1) { out.push(null); continue; }
    let sum = 0;
    for (let j = i - period + 1; j <= i; j++) sum += values[j];
    out.push(sum / period);
  }
  return out;
}

function fmtMoney(n) {
  return (n ?? 0).toLocaleString('zh-CN', { maximumFractionDigits: 2 });
}

/* ── stock picker (single-select, click triggers analysis) ── */
async function loadPicker() {
  try {
    const holdings = await fetch('/holdings').then(r => r.json());
    stockPickerBar.innerHTML = '';
    holdings.forEach(h => {
      const chip = document.createElement('div');
      chip.className = 'picker-chip';
      chip.dataset.ticker = h.ticker;
      chip.innerHTML = `
        <div class="picker-chip-name">${h.name}</div>
        <div class="picker-chip-ticker">${h.ticker}</div>`;
      chip.onclick = () => selectStock(h.ticker, h.name);
      stockPickerBar.appendChild(chip);
    });
  } catch (e) {
    stockPickerBar.innerHTML = '<span class="ai-log-empty">加载持仓失败，检查 uvicorn 是否运行。</span>';
  }
}

function selectStock(ticker, name) {
  selectedTicker = ticker;

  document.querySelectorAll('.picker-chip').forEach(c => {
    c.classList.toggle('selected', c.dataset.ticker === ticker);
  });

  chartTicker.textContent = `${name}（${ticker}）`;
  chartPrice.textContent  = '';
  buyBtn.disabled  = false;
  sellBtn.disabled = false;
  orderMsg.textContent = '';

  loadChart(ticker);
  startQuotePolling(ticker);
  showStartPrompt(ticker, name);  // 选股只加载行情，AI 分析要等用户点"开始分析"才跑，避免白花 token
}

/* ── chart ── */
async function loadChart(ticker) {
  chartEmpty.textContent   = 'K线加载中…';
  chartEmpty.style.display = '';
  if (chart) { chart.remove(); chart = null; candleSeries = null; volumeSeries = null; }
  latestBar = null;

  let klines;
  try {
    const resp = await fetch(`/stock/${ticker}/klines`);
    if (!resp.ok) throw new Error('HTTP ' + resp.status);
    klines = await resp.json();
  } catch (e) {
    chartEmpty.textContent = 'K线加载失败（数据源可能暂时不可用），稍后重试。';
    return;
  }
  if (!klines.length) {
    chartEmpty.textContent = '没有可用的K线数据。';
    return;
  }
  chartEmpty.style.display = 'none';

  chart = LightweightCharts.createChart(chartContainer, {
    layout: { background: { color: '#ffffff' }, textColor: '#17293D' },
    grid: { vertLines: { color: '#E3E9EF' }, horzLines: { color: '#E3E9EF' } },
    timeScale: { borderColor: '#E3E9EF' },
    rightPriceScale: { borderColor: '#E3E9EF' },
    autoSize: true,
  });

  candleSeries = chart.addSeries(LightweightCharts.CandlestickSeries, {
    upColor: '#C62828', downColor: '#2E7D32',           // A股红涨绿跌
    borderUpColor: '#C62828', borderDownColor: '#2E7D32',
    wickUpColor: '#C62828', wickDownColor: '#2E7D32',
  });
  candleSeries.setData(klines.map(d => ({ time: d.time, open: d.open, high: d.high, low: d.low, close: d.close })));

  const closes = klines.map(d => d.close);
  const times  = klines.map(d => d.time);
  function addMaLine(period, color) {
    const ma     = rollingMean(closes, period);
    const series = chart.addSeries(LightweightCharts.LineSeries, { color, lineWidth: 1, priceLineVisible: false });
    series.setData(
      times.map((t, i) => (ma[i] == null ? null : { time: t, value: Number(ma[i].toFixed(2)) })).filter(Boolean)
    );
  }
  addMaLine(5,  '#D97706');
  addMaLine(20, '#0F2A46');
  addMaLine(60, '#8A4B05');

  volumeSeries = chart.addSeries(LightweightCharts.HistogramSeries, {
    priceFormat: { type: 'volume' },
  }, 1); // paneIndex 1 → 独立的成交量子图
  volumeSeries.setData(klines.map(d => ({
    time: d.time, value: d.volume,
    color: d.close >= d.open ? '#C62828' : '#2E7D32',
  })));
  const volPane = chart.panes()[1];
  if (volPane) volPane.setHeight(100);

  chart.timeScale().fitContent();

  latestBar = { ...klines[klines.length - 1] };
}

/* ── quote polling (2.5s，更新价格文字 + 当前这根K线的收盘价) ── */
function startQuotePolling(ticker) {
  if (quotePollTimer) clearInterval(quotePollTimer);
  pollQuote(ticker);
  quotePollTimer = setInterval(() => pollQuote(ticker), 2500);
}

async function pollQuote(ticker) {
  if (ticker !== selectedTicker) return;
  try {
    const q = await fetch(`/stock/${ticker}/quote`).then(r => r.json());
    if (!q.price) return;

    const prevClose = latestBar ? latestBar.close : q.price;
    chartPrice.textContent = q.price.toFixed(2);
    chartPrice.className   = 'chart-price ' + (q.price >= prevClose ? 'up' : 'down');

    if (candleSeries && latestBar) {
      latestBar = {
        ...latestBar,
        close: q.price,
        high:  Math.max(latestBar.high, q.price),
        low:   Math.min(latestBar.low, q.price),
      };
      candleSeries.update(latestBar);
    }
  } catch (e) { /* 静默失败，下一轮再试 */ }
}

/* ── AI assistant：单股票模式，复用 /research/stream（跟 index.html 同一套接口）
   右侧按"本地分析→隐私闸门→云端调研→本地综合"分阶段展示，每个阶段一张卡片，
   卡片内部逐条追加消息（不是每条消息单独一张卡片，避免像参考图那样信息碎片化）。 ── */

function setAiStep(step) {
  const idx = AI_STEPS.indexOf(step);
  document.querySelectorAll('.ai-step').forEach(el => {
    const elIdx = AI_STEPS.indexOf(el.dataset.step);
    el.classList.toggle('done', elIdx < idx);
    el.classList.toggle('active', elIdx === idx);
  });
}

function clearFeedEmpty() {
  const empty = document.getElementById('aiFeedEmpty');
  if (empty) empty.remove();
}

function createPhaseCard(type, headerHtml) {
  clearFeedEmpty();
  const card = document.createElement('div');
  card.className = `phase-card ${type}`;
  card.innerHTML = `<div class="phase-card-header">${headerHtml}</div><div class="phase-card-body"></div>`;
  aiFeed.appendChild(card);
  aiFeed.scrollTop = aiFeed.scrollHeight;
  return card;
}

function appendPhaseLine(card, text) {
  if (!card) return;
  const body = card.querySelector('.phase-card-body');
  const line = document.createElement('div');
  line.className = 'phase-line';
  line.textContent = text;
  body.appendChild(line);
  aiFeed.scrollTop = aiFeed.scrollHeight;
}

function resetAiPanel() {
  if (currentSSE)     { currentSSE.close();     currentSSE = null; }
  if (currentWatcher) { currentWatcher.close(); currentWatcher = null; }
  localCard = cloudCard = fusionCard = null;
  document.querySelectorAll('.ai-step').forEach(el => el.classList.remove('active', 'done'));
}

/* 选中股票后先停在这一步，等用户主动点"开始分析"才真正调用 /research/stream——
   那一步后端会调 LLM 生成研究问题，选股就自动触发的话很容易白花 token。 */
function showStartPrompt(ticker, name) {
  resetAiPanel();
  aiFeed.innerHTML = `
    <div class="ai-feed-empty" id="aiFeedEmpty">
      <p>已选择 ${name}（${ticker}）</p>
      <button class="btn-start-analysis" id="startAnalysisBtn">开始分析</button>
    </div>`;
  document.getElementById('startAnalysisBtn').onclick = () => startAiAnalysis(ticker);
}

function startAiAnalysis(ticker) {
  resetAiPanel();
  setAiStep('local');
  aiFeed.innerHTML = '';
  localCard = createPhaseCard('local', '🔍 本地分析');
  appendPhaseLine(localCard, '正在读取持仓与实时行情，计算技术指标…');

  currentSSE = new EventSource(`/research/stream?tickers=${encodeURIComponent(ticker)}`);
  currentSSE.onmessage = function(e) {
    const data = JSON.parse(e.data);
    // 只关心阶段是否完成，具体每一步在调什么接口不在这里展示细节
    if (data.type === 'ready') { currentSSE.close(); showGate(data.thread_id, data.preview); }
    if (data.type === 'error') {
      currentSSE.close();
      appendPhaseLine(localCard, `⚠️ 出错了：${data.text}`);
    }
  };
  currentSSE.onerror = function() {
    currentSSE.close();
    appendPhaseLine(localCard, '⚠️ 连接中断，检查 uvicorn 是否在运行');
  };
}

function showGate(threadId, preview) {
  setAiStep('gate');
  const item = preview.research_questions[0];
  if (!item) {
    createPhaseCard('gate', '🔒 待确认发送到云端的研究清单').querySelector('.phase-card-body').innerHTML =
      '<div class="phase-line">没有生成研究问题。</div>';
    return;
  }

  const qs = item.questions.map(q => `
    <div class="gate-q">
      <div class="gate-q-reason">${q.reason || q.query}</div>
      <div class="gate-q-query">检索词：${q.query}</div>
    </div>`).join('');

  const gateCard = createPhaseCard('gate', `🔒 待确认发送到云端的研究清单（${item.name}）`);
  gateCard.querySelector('.phase-card-body').innerHTML = `
    <div class="gate-q-list">${qs}</div>
    <div class="gate-btns">
      <button class="gate-btn-cancel"  id="aiCancelBtn">取消，只看本地</button>
      <button class="gate-btn-confirm" id="aiConfirmBtn">确认发送</button>
    </div>`;

  document.getElementById('aiConfirmBtn').onclick = () => resolveGate(threadId, 'approved', gateCard);
  document.getElementById('aiCancelBtn').onclick  = () => resolveGate(threadId, 'rejected', gateCard);
}

async function resolveGate(threadId, action, gateCard) {
  gateCard.querySelector('.gate-btns')?.remove();
  const resolvedNote = document.createElement('div');
  resolvedNote.className = 'gate-resolved';
  resolvedNote.textContent = action === 'approved' ? '✅ 已确认发送' : '🚫 已取消，仅保留本地结果';
  gateCard.querySelector('.phase-card-body').appendChild(resolvedNote);

  await fetch(`/research/confirm/${threadId}`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ action }),
  });

  if (action === 'rejected') return;

  setAiStep('cloud');
  cloudCard = createPhaseCard('cloud', '☁️ 云端调研中…');
  appendPhaseLine(cloudCard, '正在从公开渠道调研相关信息…');
  const progressTrack = document.createElement('div');
  progressTrack.className = 'progress-track';
  progressTrack.innerHTML = '<div class="progress-fill"></div>';
  cloudCard.querySelector('.phase-card-header').after(progressTrack);

  let cloudFindingsCount = 0;

  currentWatcher = new EventSource(`/research/watch/${threadId}`);
  currentWatcher.onmessage = function(e) {
    const data = JSON.parse(e.data);

    // 只统计条数做个高层小结，不展示具体调了哪些接口、每条搜索结果内容
    if (data.type === 'structured_result' || data.type === 'search_result') {
      cloudFindingsCount++;
    }

    if (data.type === 'return_arrow') {
      progressTrack.remove();
      cloudCard.querySelector('.phase-card-header').textContent = '☁️ 云端调研（已完成）';
      cloudCard.querySelector('.phase-card-body').innerHTML =
        `<div class="phase-line">已收集 ${cloudFindingsCount} 项相关信息，返回本地综合。</div>`;
      setAiStep('fusion');
      fusionCard = createPhaseCard('fusion', '✅ 本地综合');
      appendPhaseLine(fusionCard, '正在结合你的持仓生成综合建议…');
    }

    if (data.type === 'done') {
      currentWatcher.close();
      fusionCard.querySelector('.phase-card-body').innerHTML = '';
      const advice = (data.fusion_result || {}).advice || '（无建议）';
      const html   = typeof marked !== 'undefined' ? marked.parse(advice) : advice;
      const adviceEl = document.createElement('div');
      adviceEl.className = 'result-advice';
      adviceEl.innerHTML = html;
      fusionCard.querySelector('.phase-card-body').appendChild(adviceEl);
    }

    if (data.type === 'error') {
      currentWatcher.close();
      appendPhaseLine(fusionCard || cloudCard, `⚠️ 出错了：${data.text}`);
    }
  };
  currentWatcher.onerror = function() {
    currentWatcher.close();
    appendPhaseLine(fusionCard || cloudCard, '⚠️ 连接中断');
  };
}

/* ── order form ── */
buyBtn.onclick  = () => placeOrder('buy');
sellBtn.onclick = () => placeOrder('sell');

async function placeOrder(side) {
  if (!selectedTicker) return;
  const qty = parseInt(orderQty.value, 10);
  if (!qty || qty <= 0) {
    orderMsg.textContent = '请输入有效数量';
    orderMsg.className   = 'order-msg error';
    return;
  }

  buyBtn.disabled = true; sellBtn.disabled = true;
  try {
    const resp = await fetch('/paper/order', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ticker: selectedTicker, side, qty }),
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || '下单失败');
    orderMsg.textContent = `${side === 'buy' ? '买入' : '卖出'}成功：${qty}股 @ ${data.order.price}`;
    orderMsg.className   = 'order-msg success';
    await refreshTables();
  } catch (e) {
    orderMsg.textContent = e.message;
    orderMsg.className   = 'order-msg error';
  } finally {
    buyBtn.disabled = false; sellBtn.disabled = false;
  }
}

/* ── positions / orders tables ── */
async function refreshTables() {
  try {
    const positions = await fetch('/paper/positions').then(r => r.json());
    positionsTable.innerHTML = positions.length
      ? positions.map(p => `
          <tr>
            <td>${p.name}（${p.ticker}）</td>
            <td>${p.qty}</td>
            <td>${p.avg_cost.toFixed(2)}</td>
            <td>${p.price.toFixed(2)}</td>
            <td class="${p.pnl_amount >= 0 ? 'pnl-up' : 'pnl-down'}">
              ${p.pnl_amount >= 0 ? '+' : ''}${p.pnl_pct.toFixed(2)}%（${p.pnl_amount >= 0 ? '+' : ''}${fmtMoney(p.pnl_amount)}）
            </td>
          </tr>`).join('')
      : '<tr class="empty-row"><td colspan="5">暂无持仓</td></tr>';
  } catch (e) { /* 保留上一次的表格内容 */ }

  try {
    const orders = await fetch('/paper/orders').then(r => r.json());
    ordersTable.innerHTML = orders.length
      ? orders.map(o => `
          <tr>
            <td>${o.timestamp.replace('T', ' ')}</td>
            <td>${o.name}（${o.ticker}）</td>
            <td class="${o.side === 'buy' ? 'pnl-up' : 'pnl-down'}">${o.side === 'buy' ? '买入' : '卖出'}</td>
            <td>${o.qty}</td>
            <td>${o.price.toFixed(2)}</td>
          </tr>`).join('')
      : '<tr class="empty-row"><td colspan="5">暂无委托</td></tr>';
  } catch (e) { /* 保留上一次的表格内容 */ }
}

/* ── init ── */
loadPicker();
refreshTables();
