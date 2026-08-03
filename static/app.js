/* ── refs ── */
const seqEl      = document.getElementById('seq');
const colLocal   = document.getElementById('colLocal');
const colCloud   = document.getElementById('colCloud');
const colFusion  = document.getElementById('colFusion');
const xchangeGate   = document.getElementById('xchangeGate');
const xchangeReturn = document.getElementById('xchangeReturn');
const actionArea = document.getElementById('actionArea');

/* ── ticker → name lookup, populated from /holdings (used to label progress groups) ── */
const tickerNames = {};

/* ── node builders: flat node when no ticker, grouped/collapsible bubble when ticker given ── */
function pushProgress(container, side, text, ticker) {
  if (!ticker) {
    const n = document.createElement('div');
    n.className = `seq-node ${side}`;
    n.innerHTML = side === 'cloud'
      ? `<div class="seq-dot cloud"></div><span class="seq-label">${text}</span>`
      : `<span class="seq-label">${text}</span><div class="seq-dot local"></div>`;
    container.appendChild(n);
    container.scrollTop = container.scrollHeight;
    return;
  }

  let group = container.querySelector(`.stock-progress-group[data-ticker="${ticker}"]`);
  if (!group) {
    const label = tickerNames[ticker] || ticker;
    const dotHtml = `<div class="seq-dot ${side}" style="margin-top:10px;"></div>`;
    const bubbleHtml = `
      <div class="stock-progress-group" data-ticker="${ticker}" data-count="0">
        <div class="stock-progress-header" onclick="toggleProgressGroup(this)">
          <span class="stock-progress-name">${label}</span>
          <span class="stock-progress-latest"></span>
          <span class="stock-progress-count">0条</span>
          <span class="stock-progress-toggle">展开 ▼</span>
        </div>
        <div class="stock-progress-body"></div>
      </div>`;
    const wrap = document.createElement('div');
    wrap.className = `seq-node ${side}`;
    wrap.style.alignItems = 'flex-start';
    wrap.innerHTML = side === 'cloud' ? (dotHtml + bubbleHtml) : (bubbleHtml + dotHtml);
    container.appendChild(wrap);
    group = wrap.querySelector('.stock-progress-group');
  }

  const body   = group.querySelector('.stock-progress-body');
  const latest = group.querySelector('.stock-progress-latest');
  const count  = group.querySelector('.stock-progress-count');

  const line = document.createElement('div');
  line.className = 'stock-progress-line';
  line.textContent = text;
  body.appendChild(line);

  latest.textContent = text;
  const n = parseInt(group.dataset.count, 10) + 1;
  group.dataset.count = String(n);
  count.textContent = `${n}条`;

  container.scrollTop = container.scrollHeight;
}

function toggleProgressGroup(headerEl) {
  const group = headerEl.closest('.stock-progress-group');
  const body  = group.querySelector('.stock-progress-body');
  const btn   = group.querySelector('.stock-progress-toggle');
  const open  = body.classList.toggle('open');
  btn.textContent = open ? '收起 ▲' : '展开 ▼';
}

function addLocal(text, ticker)  { pushProgress(colLocal,  'local', text, ticker); }
function addCloud(text, ticker)  { pushProgress(colCloud,  'cloud', text, ticker); }
function addFusion(text, ticker) { pushProgress(colFusion, 'local', text, ticker); }

/* ── expandable search result ── */
let srCounter = 0;
function addSearchResult(data) {
  const id = 'sr' + (srCounter++);
  const previewText = data.reason || data.query;
  const preview = previewText.length > 22
    ? previewText.slice(0, 22) + '…'
    : previewText;

  const rawCount = data.raw_count ?? data.snippets.length;
  const filtered = data.snippets.length;
  const countLabel = rawCount > filtered
    ? `过滤后 ${filtered}/${rawCount} 条`
    : `找到 ${filtered} 条`;
  const snippetRows = filtered
    ? data.snippets.map(s =>
        `<div class="sr-snippet">${s.slice(0, 300)}${s.length > 300 ? '…' : ''}</div>`
      ).join('')
    : '<div class="sr-no-data">未找到相关信息</div>';

  const n = document.createElement('div');
  n.className = 'seq-node cloud';
  n.style.alignItems = 'flex-start';
  n.innerHTML = `
    <div class="seq-dot cloud" style="margin-top:10px;"></div>
    <div class="sr-bubble">
      <div class="sr-header" onclick="toggleSr('${id}')">
        <span class="sr-preview">${preview}</span>
        <span class="sr-meta">${countLabel}</span>
        <span class="sr-toggle-btn" id="${id}-btn">展开 ▼</span>
      </div>
      <div class="sr-body" id="${id}">
        <div class="sr-question">${data.reason || '（无推理说明）'}</div>
        <div class="sr-query-hint">实际检索词：${data.query}</div>
        ${snippetRows}
      </div>
    </div>`;
  colCloud.appendChild(n);
}

function toggleSr(id) {
  const body = document.getElementById(id);
  const btn  = document.getElementById(id + '-btn');
  const open = body.classList.toggle('open');
  btn.textContent = open ? '收起 ▲' : '展开 ▼';
}

/* ── portfolio summary card ── */
function renderPortfolioSummary(summary) {
  if (!summary || !summary.total_value) return '';

  const industryRows = (summary.industry_concentration || []).slice(0, 5).map(ind => `
    <div class="pf-industry-row">
      <span class="pf-industry-label">${ind.industry}</span>
      <div class="pf-industry-bar-track">
        <div class="pf-industry-bar-fill" style="width:${Math.min(ind.weight_pct, 100)}%"></div>
      </div>
      <span class="pf-industry-pct">${ind.weight_pct}%</span>
    </div>`).join('');

  const warnRows = (summary.warnings || [])
    .map(w => `<div class="pf-warn-item">⚠️ ${w}</div>`).join('');

  return `
    <div class="portfolio-card">
      <div class="portfolio-title">📊 组合概览</div>
      ${industryRows ? `<div class="pf-industry-list">${industryRows}</div>` : ''}
      ${warnRows || '<div class="pf-warn-item pf-ok">暂无明显集中度风险</div>'}
    </div>`;
}

/* ── gate ── */
let gatePreviewData = null;
let gateExpanded = true;

function showGate(threadId, preview) {
  gatePreviewData = preview;

  const portfolioCard = renderPortfolioSummary(preview.portfolio_summary);

  const stockBlocks = preview.research_questions.map(item => {
    const f = item.findings;
    const valParts = [];
    if (f.pe_ttm) {
      let peText = `PE=${f.pe_ttm.toFixed(1)}x`;
      if (f.pe_percentile != null) peText += `(分位${f.pe_percentile}%)`;
      valParts.push(peText);
    }
    if (f.pb) {
      let pbText = `PB=${f.pb.toFixed(2)}x`;
      if (f.pb_percentile != null) pbText += `(分位${f.pb_percentile}%)`;
      valParts.push(pbText);
    }
    if (f.mcap_yi)  valParts.push(`市值${f.mcap_yi.toFixed(0)}亿`);
    const signals = [
      `均线：${f.ma_signal}`,
      `量能：${f.vol_signal}`,
      `偏离MA20：${f.ma20_deviation}%`,
      `近5日：${f.price_change_5d}%`,
      ...(valParts.length ? [valParts.join('  ')] : []),
    ].join('　');
    const signals2Parts = [];
    if (f.rsi_signal)  signals2Parts.push(f.rsi_signal);
    if (f.macd_signal) signals2Parts.push(`MACD：${f.macd_signal}`);
    if (f.kdj_signal)  signals2Parts.push(`KDJ：${f.kdj_signal}`);
    if (f.turnover)    signals2Parts.push(`换手率：${f.turnover}%（5日均${f.turnover_avg5}%）`);
    if (f.trend_signal) signals2Parts.push(f.trend_signal);
    if (f.risk_signal)  signals2Parts.push(f.risk_signal);
    const signals2 = signals2Parts.join('　');
    const tags = (item.industry_tags || [])
      .map(t => `<span class="stock-tag">${t}</span>`).join('');

    const RISK_LABEL   = { low: '低风险', medium: '中等风险', high: '高风险' };
    const RISK_CLASS   = { low: 'risk-low', medium: 'risk-medium', high: 'risk-high' };
    const PERIOD_LABEL = { short: '短线', mid: '中线', long: '长线' };
    const PERIOD_CLASS = { short: 'period-short', mid: 'period-mid', long: 'period-long' };
    const rl = item.risk_level || 'medium';
    const hp = item.hold_period || 'mid';
    const metaBadges = [
      `<span class="meta-badge ${RISK_CLASS[rl] || 'risk-medium'}">${RISK_LABEL[rl] || rl}</span>`,
      `<span class="meta-badge ${PERIOD_CLASS[hp] || 'period-mid'}">${PERIOD_LABEL[hp] || hp}</span>`,
    ].join('');

    // 确认清单即将发往云端，故不显示任何持仓/成本，避免误会
    // 主展示是 reason（为什么要查），query（实际检索词）作为次要信息展示，保持透明
    const TYPE_LABEL = { factual: '公告', news: '新闻', opinion: '分析' };
    const qs = item.questions
      .map(q => {
        const t = q.type || 'news';
        const label = TYPE_LABEL[t] || t;
        return `<div class="q-item">
          <span class="q-type ${t}">${label}</span>
          <div class="q-item-text">
            <span>· ${q.reason || q.query}</span>
            <div class="q-query-hint">检索词：${q.query}</div>
          </div>
        </div>`;
      }).join('');
    return `
      <div class="stock-block">
        <div class="stock-head">
          <span class="stock-ticker">${item.ticker}</span>
          <span class="stock-name">${item.name}</span>
          ${tags}
        </div>
        <div class="stock-meta">${metaBadges}</div>
        ${item.notes ? `<div class="stock-note">📌 ${item.notes}</div>` : ''}
        <div class="stock-signals">${signals}</div>
        ${signals2 ? `<div class="stock-signals" style="margin-top:3px;font-size:11px;">${signals2}</div>` : ''}
        <div class="qs-label">将带以下问题发给云端：</div>
        ${qs}
      </div>`;
  }).join('');

  xchangeGate.innerHTML = `
    <div class="gate-wrap">
      <div class="gate-toggle-row" onclick="toggleGate()">
        <span class="gate-toggle-title">⏸ 研究清单（${preview.research_questions.length} 支股票）</span>
        <span class="gate-toggle-icon" id="gateIcon">▲ 折叠</span>
      </div>
      <div class="gate-body" id="gateBody">
        ${portfolioCard}
        ${stockBlocks}
        <div class="btn-row" id="gateBtns">
          <button class="btn-ghost" id="cancelBtn">取消，只看本地</button>
          <button class="btn-confirm" id="confirmBtn">确认发送</button>
        </div>
      </div>
    </div>`;

  document.getElementById('confirmBtn').onclick = () => resolveFlow(threadId, 'approved');
  document.getElementById('cancelBtn').onclick  = () => resolveFlow(threadId, 'rejected');
}

/* ── Structured result (expandable cloud node) ── */
let structCounter = 0;
function addStructuredResult(data) {
  const id = 'struct' + (structCounter++);
  const newsCount = (data.news || []).length;
  const epsCount  = (data.consensus_eps || []).length;
  const hasHot    = !!data.hot_reason;
  const parts = [];
  if (newsCount)  parts.push(`新闻${newsCount}条`);
  if (hasHot)     parts.push('题材');
  if (epsCount)   parts.push(`预期${epsCount}条`);
  const metaText = parts.length ? parts.join('·') : '无数据';

  // News rows
  const newsRows = newsCount
    ? (data.news || []).map(n => `
        <div class="struct-news-item">
          <div class="struct-news-date">${n['发布时间'] || ''}</div>
          <div class="struct-news-title">${n['新闻标题'] || ''}</div>
          <div class="struct-news-body">${(n['新闻内容'] || '').slice(0, 120)}…</div>
        </div>`).join('')
    : '<div class="struct-no-data">新闻拉取失败或无数据</div>';

  // Hot reason
  const hotRow = hasHot
    ? `<div class="struct-pill">🔥 ${data.hot_reason}</div>`
    : '<div class="struct-no-data">今日未上强势股榜</div>';

  // EPS rows
  const epsRows = epsCount
    ? (data.consensus_eps || []).map(row =>
        `<div class="struct-pill">${Object.entries(row).map(([k,v]) => `${k}: ${v}`).join('  ')}</div>`
      ).join('')
    : '<div class="struct-no-data">预期数据拉取失败或无数据</div>';

  const n = document.createElement('div');
  n.className = 'seq-node cloud';
  n.style.alignItems = 'flex-start';
  n.innerHTML = `
    <div class="seq-dot cloud" style="margin-top:10px;"></div>
    <div class="struct-bubble">
      <div class="struct-header" onclick="toggleStruct('${id}')">
        <span class="struct-title">🗂 ${data.name} 结构化数据</span>
        <span class="struct-meta">${metaText}</span>
        <span class="struct-toggle" id="${id}-btn">展开 ▼</span>
      </div>
      <div class="struct-body" id="${id}">
        <div class="struct-section-title">📰 个股新闻</div>
        ${newsRows}
        <div class="struct-section-title">🔥 题材归因</div>
        ${hotRow}
        <div class="struct-section-title">📊 分析师一致预期 EPS</div>
        ${epsRows}
      </div>
    </div>`;
  colCloud.appendChild(n);
}

function toggleStruct(id) {
  const body = document.getElementById(id);
  const btn  = document.getElementById(id + '-btn');
  const open = body.classList.toggle('open');
  btn.textContent = open ? '收起 ▲' : '展开 ▼';
}

/* ── Debate result (bull/bear researcher bubble, expandable) ──
   A股配色习惯：红涨绿跌，所以看多用红色系、看空用绿色系（跟国际"绿涨红跌"相反，
   保持跟本项目其他地方 .pnl-up/.pnl-down 的配色逻辑一致）。 */
let debateCounter = 0;
function addDebateResult(data) {
  const id = 'debate' + (debateCounter++);
  const isBull = data.side === 'bull';
  const icon   = isBull ? '🐂' : '🐻';
  const label  = isBull ? '看多' : '看空';
  const side   = isBull ? 'bull' : 'bear';

  const pointRows = (data.points || [])
    .map(p => `<div class="debate-point">${p}</div>`).join('');

  const n = document.createElement('div');
  n.className = 'seq-node local';
  n.style.alignItems = 'flex-start';
  n.innerHTML = `
    <div class="debate-bubble ${side}">
      <div class="debate-header" onclick="toggleDebate('${id}')">
        <span class="debate-title">${icon} ${data.name} · ${label}论据</span>
        <span class="debate-meta">${(data.points || []).length}条</span>
        <span class="debate-toggle" id="${id}-btn">展开 ▼</span>
      </div>
      <div class="debate-body" id="${id}">${pointRows}</div>
    </div>
    <div class="seq-dot local" style="margin-top:10px;"></div>`;
  colFusion.appendChild(n);
  colFusion.scrollTop = colFusion.scrollHeight;
}

function toggleDebate(id) {
  const body = document.getElementById(id);
  const btn  = document.getElementById(id + '-btn');
  const open = body.classList.toggle('open');
  btn.textContent = open ? '收起 ▲' : '展开 ▼';
}

function toggleGate() {
  const body = document.getElementById('gateBody');
  const icon = document.getElementById('gateIcon');
  if (!body) return;
  gateExpanded = !gateExpanded;
  body.style.display = gateExpanded ? '' : 'none';
  icon.textContent   = gateExpanded ? '▲ 折叠' : '▼ 展开';
}

/* ── resolve (confirm / cancel) ── */
async function resolveFlow(threadId, action) {
  document.getElementById('gateBtns')?.querySelectorAll('button')
    .forEach(b => b.disabled = true);

  if (action === 'rejected') {
    document.getElementById('gateBtns')?.remove();
    xchangeGate.innerHTML += `
      <div class="xchange-inner">
        <div class="arrow-row send"><span class="arrow-label">🚫 已取消，结果仅保留本地</span></div>
      </div>`;
    await fetch(`/research/confirm/${threadId}`, {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ action }),
    });
    actionArea.innerHTML = `<button class="btn-ghost" onclick="location.reload()">再来一次</button>`;
    return;
  }

  /* Approved: collapse gate, show send arrow, reveal cloud columns */
  document.getElementById('gateBtns')?.remove();
  if (gateExpanded) toggleGate(); // auto-collapse after confirm

  // Show send arrow inside gate exchange
  const arrowDiv = document.createElement('div');
  arrowDiv.className = 'xchange-inner';
  arrowDiv.innerHTML = `<div class="arrow-row send"><span class="arrow-label">研究清单已发送 →</span></div>`;
  xchangeGate.appendChild(arrowDiv);

  // Reveal cloud phase columns
  document.getElementById('colLocalFaded').style.display = '';
  document.getElementById('colCloud').style.display      = '';

  await fetch(`/research/confirm/${threadId}`, {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({ action }),
  });

  const watcher = new EventSource(`/research/watch/${threadId}`);

  watcher.onmessage = function(e) {
    const data = JSON.parse(e.data);

    if (data.type === 'cloud_progress') {
      addCloud(data.text, data.ticker);
    }

    if (data.type === 'structured_result') {
      addStructuredResult(data);
    }

    if (data.type === 'search_result') {
      addSearchResult(data);
    }

    if (data.type === 'debate_result') {
      addDebateResult(data);
    }

    if (data.type === 'return_arrow') {
      /* Show return arrow exchange row + reveal fusion columns */
      xchangeReturn.style.display = '';
      xchangeReturn.innerHTML = `
        <div class="xchange-inner">
          <div class="arrow-row return-local">
            <span class="arrow-label">← 调研结果已返回本地</span>
          </div>
        </div>`;
      document.getElementById('colFusion').style.display    = '';
      document.getElementById('colCloudFaded').style.display = '';
    }

    if (data.type === 'progress') {
      /* During watch phase, plain progress events are local fusion steps */
      addFusion(data.text, data.ticker);
    }

    if (data.type === 'done') {
      watcher.close();
      const r = data.fusion_result || {};
      const adviceHtml = typeof marked !== 'undefined'
        ? marked.parse(r.advice || '（无建议）')
        : (r.advice || '（无建议）').split('\n').filter(l => l.trim()).map(l => `<p>${l}</p>`).join('');
      actionArea.innerHTML = `
        <div class="result-card">
          <div class="result-title">综合建议</div>
          <div class="result-advice">${adviceHtml}</div>
          <div class="btn-row" style="margin-top:16px;">
            <button class="btn-ghost" onclick="location.reload()">再来一次</button>
          </div>
        </div>`;
    }

    if (data.type === 'error') {
      watcher.close();
      actionArea.innerHTML = `<p class="error-msg">出错了：${data.text}</p>
        <button class="btn-ghost" onclick="location.reload()">重试</button>`;
    }
  };

  watcher.onerror = function() {
    watcher.close();
    actionArea.innerHTML = `<p class="error-msg">连接中断，请重试。</p>
      <button class="btn-ghost" onclick="location.reload()">重试</button>`;
  };
}

/* ── picker ── */
(function initPicker() {
  const grid     = document.getElementById('pickerGrid');
  const selCount = document.getElementById('selCount');
  const startBtn = document.getElementById('startBtn');
  const selAll   = document.getElementById('selectAll');
  const clrAll   = document.getElementById('clearAll');
  let   selected = new Set();
  let   cards    = [];

  function updateUI() {
    selCount.textContent = selected.size;
    startBtn.disabled    = selected.size === 0;
    startBtn.textContent = selected.size > 0 ? `开始分析（${selected.size} 支）` : '开始分析';
    cards.forEach(el => {
      el.classList.toggle('selected', selected.has(el.dataset.ticker));
    });
  }

  fetch('/holdings').then(r => r.json()).then(holdings => {
    grid.innerHTML = '';
    const RISK_LABEL   = { low: '低风险', medium: '中等风险', high: '高风险' };
    const RISK_CLASS   = { low: 'risk-low', medium: 'risk-medium', high: 'risk-high' };
    const PERIOD_LABEL = { short: '短线', mid: '中线', long: '长线' };
    const PERIOD_CLASS = { short: 'period-short', mid: 'period-mid', long: 'period-long' };

    holdings.forEach(h => {
      tickerNames[h.ticker] = h.name;
      const rl   = h.risk_level  || 'medium';
      const hp   = h.hold_period || 'mid';
      const tags = (h.industry_tags || []).map(t => `<span class="stock-tag">${t}</span>`).join('');
      const card = document.createElement('div');
      card.className   = 'picker-card';
      card.dataset.ticker = h.ticker;
      const posText = (h.qty ? `持 ${h.qty} 股` : '') + (h.cost ? ` · 成本 ${h.cost}` : '');
      card.innerHTML   = `
        <div class="picker-check">✓</div>
        <div class="picker-card-ticker">${h.ticker}</div>
        <div class="picker-card-name">${h.name}</div>
        ${posText ? `<div class="picker-card-pos">${posText}</div>` : ''}
        <div class="picker-card-tags">
          ${tags}
          <span class="meta-badge ${RISK_CLASS[rl]||'risk-medium'}">${RISK_LABEL[rl]||rl}</span>
          <span class="meta-badge ${PERIOD_CLASS[hp]||'period-mid'}">${PERIOD_LABEL[hp]||hp}</span>
        </div>
        ${h.notes ? `<div class="picker-card-note">${h.notes}</div>` : ''}`;
      card.onclick = () => {
        if (selected.has(h.ticker)) selected.delete(h.ticker);
        else selected.add(h.ticker);
        updateUI();
      };
      grid.appendChild(card);
      cards.push(card);
    });
  }).catch(() => {
    grid.innerHTML = '<p style="color:red;font-size:13px;">加载持仓失败，检查 uvicorn 是否运行。</p>';
  });

  selAll.onclick = (e) => { e.preventDefault(); cards.forEach(c => selected.add(c.dataset.ticker)); updateUI(); };
  clrAll.onclick = (e) => { e.preventDefault(); selected.clear(); updateUI(); };

  startBtn.onclick = function() {
    const tickers = [...selected].join(',');
    const deep = document.getElementById('deepModeToggle').checked;
    document.getElementById('pickerWrap').style.display = 'none';
    seqEl.style.display = 'block';

    const source = new EventSource(
      `/research/stream?tickers=${encodeURIComponent(tickers)}&deep=${deep}`
    );

    source.onmessage = function(e) {
      const data = JSON.parse(e.data);
      if (data.type === 'progress') addLocal(data.text, data.ticker);
      if (data.type === 'ready') { source.close(); showGate(data.thread_id, data.preview); }
      if (data.type === 'error') {
        source.close();
        actionArea.innerHTML = `<p class="error-msg">出错了：${data.text}</p>
          <button class="btn-ghost" onclick="location.reload()">重试</button>`;
      }
    };
    source.onerror = function() {
      source.close();
      actionArea.innerHTML = `<p class="error-msg">连接中断，检查 uvicorn 是否在运行。</p>
        <button class="btn-ghost" onclick="location.reload()">重试</button>`;
    };
  };
})();
