/* ==========================================================================
   板块评分工作台 · 纯前端 PWA
   流程：读静态 JSON 秒开（IndexedDB 缓存）-> 1 次 JSONP 拉实时快照补丁 -> 重渲染
   ========================================================================== */

const DATA = 'data/';
const MAX_SEL = 12;
const SNAP_TTL = 3 * 60 * 1000;      // 实时快照 3 分钟，避免反复打开触发限流
const LS_SEL = 'wb.sel';

const $ = (s) => document.querySelector(s);
const clamp = (n, a, b) => Math.min(Math.max(n, a), b);

/* ======================= IndexedDB：无依赖极简封装 =======================
   单库单表 kv，键：snap / idx / meta / sector:{code}
   不可用时自动降级到 localStorage，再不行退化为内存，功能不中断。
   ========================================================================= */
const Store = (() => {
  const DB_NAME = 'wb-workbench';
  const STORE = 'kv';
  let dbp = null;
  const mem = new Map();

  function open() {
    if (dbp) return dbp;
    dbp = new Promise((resolve) => {
      if (typeof indexedDB === 'undefined') return resolve(null);
      let req;
      try { req = indexedDB.open(DB_NAME, 1); } catch { return resolve(null); }
      req.onupgradeneeded = () => {
        const db = req.result;
        if (!db.objectStoreNames.contains(STORE)) db.createObjectStore(STORE);
      };
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => resolve(null);
      req.onblocked = () => resolve(null);
    });
    return dbp;
  }

  async function get(key) {
    const db = await open();
    if (!db) {
      if (mem.has(key)) return mem.get(key);
      try { return JSON.parse(localStorage.getItem('wb.' + key)) ?? null; } catch { return null; }
    }
    return new Promise((resolve) => {
      try {
        const tx = db.transaction(STORE, 'readonly');
        const r = tx.objectStore(STORE).get(key);
        r.onsuccess = () => resolve(r.result ?? null);
        r.onerror = () => resolve(null);
      } catch { resolve(null); }
    });
  }

  async function set(key, val) {
    mem.set(key, val);
    const db = await open();
    if (!db) {
      try { localStorage.setItem('wb.' + key, JSON.stringify(val)); } catch {}
      return;
    }
    try {
      const tx = db.transaction(STORE, 'readwrite');
      tx.objectStore(STORE).put(val, key);
    } catch {}
  }

  return { get, set };
})();

/* ============================ 工具函数 ============================ */

/** JSONP：用 <script> 注入绕开浏览器跨域限制 */
function jsonp(url, timeout = 8000) {
  return new Promise((resolve, reject) => {
    const name = 'wbcb' + Math.floor(Math.random() * 1e9);
    const script = document.createElement('script');
    const timer = setTimeout(() => { cleanup(); reject(new Error('timeout')); }, timeout);
    function cleanup() {
      clearTimeout(timer);
      delete window[name];
      if (script.parentNode) script.parentNode.removeChild(script);
    }
    window[name] = (d) => { cleanup(); resolve(d); };
    script.onerror = () => { cleanup(); reject(new Error('network')); };
    script.src = `${url}&cb=${name}`;
    document.head.appendChild(script);
  });
}

function ls(key, fallback) {
  try { const v = JSON.parse(localStorage.getItem(key)); return v ?? fallback; }
  catch { return fallback; }
}
function lsSet(key, val) { try { localStorage.setItem(key, JSON.stringify(val)); } catch {} }

/* ================== 实时补丁：拉全部行业板块快照 ==================
   两个必须遵守的接口事实（踩过坑，勿改）：
   1) clist 单页 pz 被服务端硬截断到 100 —— 必须按 total 翻页，否则 496 个
      板块只拿到前 100 个，自选板块很可能不在其中、实时补丁静默失效。
   2) secids 参数在 clist 上不可用（rc:102 / data:null），只能全量拉再本地索引。
   3) push2 = 实时行情（手机首选）；push2delay = 延时行情（海外 runner 才需要，
      手机端仅作兜底）。顺序不能反，否则盘中看到的是延时价。
   ================================================================= */
const LIVE_HOSTS = ['push2.eastmoney.com', 'push2delay.eastmoney.com'];
const LIVE_FIELDS = 'f2,f3,f8,f12,f14,f62,f104,f105';
const LIVE_PAGE = 100;               // 服务端硬上限，改大无效

function liveURL(host, pn) {
  return `https://${host}/api/qt/clist/get`
    + `?pn=${pn}&pz=${LIVE_PAGE}&po=1&np=1&fltt=2&invt=2&fid=f3&fs=m:90+t:2`
    + `&fields=${LIVE_FIELDS}`;
}

/** 拉一页，返回 { rows, total } */
async function livePage(host, pn) {
  const d = await jsonp(liveURL(host, pn));
  const data = d?.data || {};
  return { rows: data.diff || [], total: data.total || 0 };
}

/** 翻页拉全量（首屏拿到 total 后再并发补齐剩余页） */
async function liveAll(host) {
  const first = await livePage(host, 1);
  if (!first.rows.length) throw new Error('empty');
  const pages = Math.ceil(Math.min(first.total, 800) / LIVE_PAGE);
  if (pages <= 1) return first.rows;
  const rest = await Promise.all(
    Array.from({ length: pages - 1 }, (_, i) =>
      livePage(host, i + 2).then((r) => r.rows).catch(() => []))
  );
  return first.rows.concat(...rest);
}

async function liveSnapshot() {
  const cached = await Store.get('snap2');
  if (cached && Date.now() - cached.ts < SNAP_TTL) return cached.rows;

  for (const host of LIVE_HOSTS) {
    try {
      const raw = await liveAll(host);
      const rows = raw
        .filter((r) => r && r.f12 && r.f14)
        .map((r) => ({
          code: String(r.f12),
          name: r.f14,
          price: r.f2,
          pct: r.f3,
          turnover: r.f8,
          up: r.f104,
          down: r.f105,
          mainInflow: r.f62,
        }));
      if (!rows.length) continue;
      await Store.set('snap2', { ts: Date.now(), rows, host });
      return rows;
    } catch { /* 换下一个域名 */ }
  }
  return cached?.rows || [];      // 全失败则回退旧快照，不阻断渲染
}

/** 把实时数据折算成对静态评分的微调（只动与当日强相关的三个因子） */
function applyPatch(detail, live) {
  if (!live || !Number.isFinite(live.pct)) return detail;
  const f = { ...detail.factors };
  f.momentum = clamp(f.momentum + clamp(live.pct / 10, -0.3, 0.3) * 0.35, 0, 1);
  if (Number.isFinite(live.turnover)) {
    f.turnover = clamp(f.turnover + clamp((2 - live.turnover) / 10, -0.2, 0.2), 0, 1);
  }
  if (Number.isFinite(live.mainInflow)) {
    f.fund_flow = clamp(live.mainInflow > 0
      ? 0.65 + clamp(live.mainInflow / 5e9, 0, 0.3)
      : 0.35 - clamp(-live.mainInflow / 5e9, 0, 0.3), 0, 1);
  }
  const W = { momentum: 0.10, trend: 0.10, volume_price: 0.08, turnover: 0.08,
              fund_flow: 0.10, valuation: 0.10, cycle: 0.08, clearing: 0.06,
              policy: 0.06, news: 0.12, resonance: 0.12 };
  const base = 100 * Object.keys(W).reduce((s, k) => s + W[k] * (f[k] ?? 0.5), 0);
  let score = base - (detail.penalty || 0);
  if (detail.blacklisted) score = Math.min(score, 45);
  score = Math.round(clamp(score, 0, 100) * 10) / 10;
  return { ...detail, factors: f, score, base, live_pct: live.pct, patched: true };
}

/* ============================ 渲染片段 ============================ */

const FACTOR_NAMES = {
  momentum: '动量', trend: '趋势结构', volume_price: '量价健康',
  turnover: '换手率位置', fund_flow: '主力资金', valuation: '估值位置',
  cycle: '周期位置', clearing: '出清度', policy: '政策强度',
  news: '新闻情绪', resonance: '板块共振',
};
const MACRO_NAMES = { liquidity: '流动性', volume_price: '量价关系',
                      policy: '政策', sentiment: '情绪' };

function scoreColor(s) {
  if (s >= 75) return '#ef4444';          // A 股习惯：红涨绿跌
  if (s >= 60) return '#f97316';
  if (s >= 45) return '#94a3b8';
  if (s >= 30) return '#22c55e';
  return '#16a34a';
}

function ringSVG(score) {
  const r = 34, c = 2 * Math.PI * r;
  const off = c * (1 - clamp(score, 0, 100) / 100);
  return `<svg class="ring" viewBox="0 0 80 80" aria-label="评分 ${score}">
    <circle cx="40" cy="40" r="${r}" stroke="#1f2937" stroke-width="8" fill="none"/>
    <circle cx="40" cy="40" r="${r}" stroke="${scoreColor(score)}" stroke-width="8" fill="none"
      stroke-dasharray="${c.toFixed(1)}" stroke-dashoffset="${off.toFixed(1)}"
      transform="rotate(-90 40 40)" stroke-linecap="round"/>
    <text x="40" y="47" text-anchor="middle" font-size="21" fill="#e5e7eb">${score}</text>
  </svg>`;
}

function factorBars(factors) {
  return Object.entries(FACTOR_NAMES).map(([k, label]) => {
    const v = factors[k] ?? 0.5;
    return `<div class="row"><span>${label}</span>
      <div class="track"><i class="${v >= 0.6 ? 'hot' : 'cold'}"
        style="width:${(v * 100).toFixed(0)}%"></i></div>
      <b>${(v * 100).toFixed(0)}</b></div>`;
  }).join('');
}

function macroCard(meta) {
  const d = meta?.macro_detail;
  if (!d) return '';
  const rows = Object.entries(MACRO_NAMES).map(([k, label]) => {
    const item = d[k] || {};
    const v = item.score ?? 0.5;
    const pct = Math.round(v * 100);
    let note = '';
    if (k === 'sentiment' && item.inverted) {
      note = `原始 ${pct} → 逆向 ${100 - pct}`;
    } else if (k === 'liquidity' && item.lpr_1y != null) {
      note = `LPR ${item.lpr_1y}% · 两融 ${item.margin_growth != null
        ? (item.margin_growth * 100).toFixed(2) + '%' : '—'}`;
    } else if (k === 'volume_price' && item.pattern != null) {
      note = `形态 ${(item.pattern * 100).toFixed(0)} · 广度 ${(item.breadth * 100).toFixed(0)}`;
    } else if (k === 'policy' && item.hits != null) {
      note = `命中 ${item.hits} 条 · 权重 ${item.weight}`;
    }
    return `<div class="mrow"><span>${label}</span>
      <div class="mtrack"><i style="width:${pct}%"></i></div>
      <b>${pct}</b><em>${note}</em></div>`;
  }).join('');
  return `<section class="card"><h3>宏观四要素</h3>${rows}
    <p class="src">情绪为逆向指标：亢奋→扣分，低迷→加分</p></section>`;
}

function newsList(news) {
  if (!news || !news.length) return '<p class="src">暂无匹配新闻</p>';
  return news.map((n) => {
    const cls = n.sent > 0.15 ? 'p' : (n.sent < -0.15 ? 'n' : 'z');
    return `<div class="news"><i class="${cls}"></i>
      <span>${n.t}</span><p>${n.title}</p></div>`;
  }).join('');
}

function penaltyNote(d) {
  const p = d.penalty_detail;
  if (!p) return '';
  const parts = [`罚分 −${d.penalty}`];
  if (p.release_mv_yi) parts.push(`解禁 ${p.release_mv_yi} 亿（占市值 ${(p.release_ratio * 100).toFixed(2)}%）`);
  else parts.push('近 30 日无解禁');
  if (p.reduction_cnt) parts.push(`减持公告 ${p.reduction_cnt} 条`);
  return `<p class="src">${parts.join(' · ')}</p>`;
}

function renderMacroBar(meta) {
  const m = meta?.macro_score;
  const zone = meta?.macro_zone ?? '-';
  const cls = zone === '进攻' ? 'up' : (zone === '防守' ? 'down' : 'flat');
  const demo = meta?.demo ? '<em class="demo">演示数据</em>' : '';
  return `宏观 <b>${m ?? '-'}</b><em class="${cls}">${zone}</em>${demo}
    <span class="ts">${meta?.updated || ''}${meta?.intraday ? ' · 盘中快照' : ''}</span>`;
}

/* 演示数据警示
   meta.demo 为真说明产物来自 make_demo.py 的**合成数据**，数字没有任何实际意义。
   必须显著标注，避免把假数字当成真评分看待。横幅插在 header 内，只插一次。 */
function demoBanner(meta) {
  if (!meta?.demo || document.getElementById('demoBar')) return;
  const el = document.createElement('div');
  el.id = 'demoBar';
  el.className = 'demo-bar';
  el.innerHTML = '⚠ 当前为<b>合成演示数据</b>，评分与新闻均无实际意义。'
    + '请在仓库 Actions 手动运行 <code>warmup</code> → <code>score</code> 生成真实数据';
  const header = document.querySelector('header');
  if (header) header.insertBefore(el, header.firstChild);
  else document.body.insertBefore(el, document.body.firstChild);
}

function renderChips(boards, current) {
  return boards.map((b) => {
    const tone = b.score >= 60 ? 'hot' : (b.score >= 40 ? 'mid' : 'cold');
    return `<button class="chip${b.code === current ? ' on' : ''}" data-code="${b.code}">
      ${b.name}<i class="${tone}">${b.score}</i></button>`;
  }).join('') + '<button class="chip add" id="btnManage">＋ 管理</button>';
}

function renderDetail(d) {
  const patch = d.patched
    ? `<span class="live">实时 ${d.live_pct > 0 ? '+' : ''}${d.live_pct}%</span>` : '';
  return `
  <section class="card hero">
    ${ringSVG(d.score)}
    <div class="hero-info">
      <h2>${d.name}${patch}</h2>
      <p class="tag" style="color:${scoreColor(d.score)}">${d.label}</p>
      <p class="act">${d.action_hint}</p>
      ${d.blacklisted ? '<p class="blk">⚠ 命中负面清单，分数已压制</p>' : ''}
      <p class="src">出清阶段：${d.clearing_stage} · 估值口径：${d.valuation_source}</p>
    </div>
  </section>

  <section class="card">
    <h3>因子拆解（11 维 + 筹码罚分）</h3>
    ${factorBars(d.factors)}
    ${penaltyNote(d)}
  </section>

  <section class="card">
    <h3>相关新闻（按板块关键词归属，近 30 条）</h3>
    ${newsList(d.news)}
  </section>

  <section class="card discipline">
    <h3>纪律提示</h3>
    <p>· 单次不超过 5%，累计目标 30% / 50%</p>
    <p>· 不到预设点位不做 T：高抛看压力位，低吸须跌破支撑</p>
    <p>· 换手率回到 3% 以上 → 进入减仓观察</p>
    <p>· 仓位第一：仓位过重时，亏损也可以减</p>
  </section>`;
}

/* ============================== 主流程 ============================== */

const state = { idx: [], meta: {}, selected: [], current: null, version: '' };

async function fetchJSON(path) {
  const r = await fetch(path, { cache: 'no-cache' });
  if (!r.ok) throw new Error(`${path} ${r.status}`);
  return r.json();
}

async function loadDetail(code, version) {
  const key = `sector:${code}`;
  const hit = await Store.get(key);
  if (hit && hit.version === version) return hit.data;    // 与 meta 版本一致即命中
  const d = await fetchJSON(`${DATA}sectors/${code}.json`);
  await Store.set(key, { version, data: d });
  return d;
}

async function renderCurrent() {
  const main = $('#main');
  const code = state.current;
  if (!code) return;

  // 宏观卡与详情一起渲染，避免被后续 innerHTML 覆盖
  const paint = (html) => { main.innerHTML = macroCard(state.meta) + html; };

  $('#macro').innerHTML = renderMacroBar(state.meta);
  $('#chips').innerHTML = renderChips(
    state.selected.map((c) => state.idx.find((b) => b.code === c)).filter(Boolean), code);

  const cached = await Store.get(`sector:${code}`);
  if (cached?.data) paint(renderDetail(cached.data));   // 先渲染缓存，避免白屏

  try {
    const d = await loadDetail(code, state.version);
    paint(renderDetail(d));
    const snap = await liveSnapshot();
    const live = snap.find((r) => r.code === code);
    if (live) paint(renderDetail(applyPatch(d, live)));
  } catch (e) {
    if (!cached?.data) paint(`<div class="skel">该板块数据加载失败：${e.message}</div>`);
  }
}

/* ========================= 自选管理面板 ========================= */

function openSheet() {
  $('#sheetBody').innerHTML = state.idx.map((b) => {
    const on = state.selected.includes(b.code);
    return `<label class="opt${on ? ' on' : ''}">
      <input type="checkbox" value="${b.code}" ${on ? 'checked' : ''}>
      <span>${b.name}</span><b>${b.score}</b></label>`;
  }).join('');
  $('#sheet').hidden = false;
  $('#mask').hidden = false;
  updateSelCount();
}

function closeSheet() {
  $('#sheet').hidden = true;
  $('#mask').hidden = true;
  persistSelected();
  renderCurrent();
}

function updateSelCount() {
  const n = $('#sheetBody').querySelectorAll('input:checked').length;
  $('#selcount').textContent = `${n} / ${MAX_SEL}`;
  $('#selcount').style.color = n > MAX_SEL ? '#f87171' : '';
}

function persistSelected() {
  const codes = [...$('#sheetBody').querySelectorAll('input:checked')].map((i) => i.value);
  if (codes.length > MAX_SEL) codes.length = MAX_SEL;
  if (!codes.length) return;
  state.selected = codes;
  lsSet(LS_SEL, codes);
}

function bindSheet() {
  $('#sheetBody').addEventListener('change', (e) => {
    if (e.target.type !== 'checkbox') return;
    if ($('#sheetBody').querySelectorAll('input:checked').length > MAX_SEL) {
      e.target.checked = false;
      alert(`最多只能选 ${MAX_SEL} 个板块`);
    }
    e.target.closest('.opt').classList.toggle('on', e.target.checked);
    updateSelCount();
  });
  $('#sheetClose').addEventListener('click', closeSheet);
  $('#mask').addEventListener('click', closeSheet);
  $('#search').addEventListener('input', (e) => {
    const q = e.target.value.trim();
    $('#sheetBody').querySelectorAll('.opt').forEach((el) => {
      el.hidden = q && !el.querySelector('span').textContent.includes(q);
    });
  });
}

/* ============================== boot ============================== */

async function boot() {
  let idx = [], meta = {};
  try {
    [idx, meta] = await Promise.all([
      fetchJSON(`${DATA}index.json`),
      fetchJSON(`${DATA}meta.json`),
    ]);
    await Store.set('idx', { version: meta.updated || '', data: idx });
    await Store.set('meta', { version: meta.updated || '', data: meta });
  } catch {
    const ci = await Store.get('idx');
    const cm = await Store.get('meta');
    idx = ci?.data || ls('wb.idx', []);
    meta = cm?.data || ls('wb.meta', {});
    if (!idx.length) {
      $('#main').innerHTML =
        '<div class="skel">暂无数据<br>请先在 Actions 里手动跑一次 warmup + score</div>';
      return;
    }
  }
  state.idx = idx;
  state.meta = meta;
  state.version = meta.updated || '';

  demoBanner(meta);   // 合成演示数据必须显著标注，避免误当真数据

  state.selected = ls(LS_SEL, []);
  if (!state.selected.length) {
    state.selected = state.idx.slice(0, 5).map((b) => b.code);
    lsSet(LS_SEL, state.selected);
  }
  state.current = state.selected[0];

  // 在详情区顶部插入宏观卡（每次渲染前重建）
  bindSheet();
  $('#chips').addEventListener('click', (e) => {
    if (e.target.closest('#btnManage')) { openSheet(); return; }
    const chip = e.target.closest('.chip');
    if (!chip || !chip.dataset.code) return;
    state.current = chip.dataset.code;
    renderCurrent();
  });
  await renderCurrent();
}

boot();

/* ------------------------------------------------------ PWA 注册 */
if ('serviceWorker' in navigator) {
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('sw.js').catch(() => {});
  });
}
