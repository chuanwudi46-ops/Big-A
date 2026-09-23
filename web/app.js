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

/* ================= 数据新鲜度（时间语义一律是北京时间） =================
   产物里的 `updated` 由 pipeline/clock.py 写成**北京时间**。
   为什么不能直接 `new Date(s)`：JS 按手机本地时区解析，人在国外时「3 分钟前」
   会被算成 8 小时前 —— 而本项目的时间语义永远是北京时间。故显式按 UTC+8 解析。
   ====================================================================== */

function parseCN(s) {
  const m = /^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2}):(\d{2})/.exec(String(s || ''));
  return m ? Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4] - 8, +m[5], +m[6]) : NaN;
}

function agoText(s) {
  const t = parseCN(s);
  if (!Number.isFinite(t)) return '';
  const min = Math.floor((Date.now() - t) / 60000);
  if (min < 1) return '刚刚';
  if (min < 60) return `${min} 分钟前`;
  const h = Math.floor(min / 60);
  return h < 24 ? `${h} 小时前` : `${Math.floor(h / 24)} 天前`;
}

/** 超过约 26 小时没更新就标黄提示（周末与节假日属正常，只是提醒「这份不是今天的」） */
function isStale(s) {
  const t = parseCN(s);
  return Number.isFinite(t) && (Date.now() - t) > 26 * 3600 * 1000;
}

/* ================== 实时补丁：拉全部行业板块快照 ==================
   两个必须遵守的接口事实（踩过坑，勿改）：
   1) clist 单页 pz 被服务端硬截断到 100 —— 必须按 total 翻页，否则 496 个
      板块只拿到前 100 个，自选板块很可能不在其中、实时补丁静默失效。
   2) secids 参数在 clist 上不可用（rc:102 / data:null），只能全量拉再本地索引。
   3) push2 = 实时行情（手机首选）；push2delay = 延时行情（海外 runner 才需要，
      手机端仅作兜底）。顺序不能反，否则盘中看到的是延时价。
   4) 排序键必须用 **f12 代码（静态）+ po=0 升序**，不能用 f3 涨幅：
      涨幅盘中一直在变，翻页边界会串位 —— 实测 496 条里会重复 1 条、漏掉 1 个板块。
      我们只做本地索引、不关心顺序，故换稳定排序键零副作用。
   ================================================================= */
const LIVE_HOSTS = ['push2.eastmoney.com', 'push2delay.eastmoney.com'];
// f62 主力净流入 + f184 主力净占比 —— 「主力资金」的**板块排行**与「板块评分」的
// 资金流补丁都要用（实测两者同在一个 clist 响应里，一起要不额外花钱）。
// ⚠️ 只要**实际被读**的字段：四档拆解（f66/f72/f78/f84）**大盘那一路才需要**，
// 而大盘块的数据来自后端 flow.json（分时接口不吃 cb=，浏览器补不了），
// 板块排行则只显示主力合计 —— 所以这里要了也没人读，纯属多传 4 列 × 120 行的流量。
const LIVE_FIELDS = 'f2,f3,f8,f12,f14,f62,f104,f105,f184';
const LIVE_PAGE = 100;               // 服务端硬上限，改大无效
// 缓存键版本位：**只要「新增了会被读的字段」就必须升**（snap2 -> snap3），
// 否则用户端 IndexedDB 里还是那份缺新字段的旧快照，「修了等于没修」。
// 反向不成立：**砍掉没人读的字段不用升** —— 旧快照是多出来的超集，仍然满足新需求，
// 升了只会让所有用户白重下一遍。
// 见 china-finance-data-from-overseas-runner 手册「步骤 7 ④」。
const LIVE_CACHE_KEY = 'snap3';

function liveURL(host, pn) {
  return `https://${host}/api/qt/clist/get`
    + `?pn=${pn}&pz=${LIVE_PAGE}&po=0&np=1&fltt=2&invt=2&fid=f12&fs=m:90+t:2`
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
  const cached = await Store.get(LIVE_CACHE_KEY);
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
          mainInflow: r.f62,        // 主力净流入（元）
          mainPct: r.f184,          // 主力净占比（%）
        }));
      if (!rows.length) continue;
      await Store.set(LIVE_CACHE_KEY, { ts: Date.now(), rows, host });
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
  // updated 是北京时间（pipeline/clock.py 保证）。原先直接打印，而 CI runner 是 UTC，
  // 于是收盘 15:43 的数据在手机上显示成「早上 07:43」——看着像一整天没更新。
  // 这里再补一个相对时间，并在超过约一天时标黄。
  const stale = isStale(meta?.updated) ? ' stale' : '';
  return `宏观 <b>${m ?? '-'}</b><em class="${cls}">${zone}</em>${demo}
    <span class="ts${stale}">${meta?.updated || ''}<span class="ago" id="agoTs">${
    agoText(meta?.updated)}</span>${meta?.intraday ? ' · 盘中快照' : ''}</span>`;
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

/* 数据更新提示条
   盘中每 30 分钟会出一版新产物。这里每 5 分钟静默探一次 meta.json，
   发现 updated 变了只**提示**、不强制刷新 —— 用户可能正在看某个板块，
   直接 reload 会打断。回前台时也补查一次：手机上切回来最容易看到旧内容。 */
function startFreshWatch(meta) {
  const seen = meta?.updated || '';
  const check = async () => {
    if (document.getElementById('freshBar')) return;
    try {
      const m = await fetchJSON(`${DATA}meta.json`);
      if (!m?.updated || m.updated === seen) return;
      const el = document.createElement('div');
      el.id = 'freshBar';
      el.className = 'fresh-bar';
      el.innerHTML = `数据已更新到 <b>${String(m.updated).slice(11, 16)}</b>`
        + '（北京时间） · 点此刷新';
      el.addEventListener('click', () => location.reload());
      const header = document.querySelector('header');
      (header || document.body).insertBefore(el, (header || document.body).firstChild);
    } catch { /* 离线或产物缺失：静默，不打扰 */ }
  };
  setInterval(check, 5 * 60 * 1000);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) check(); });
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

/* ================= 大盘关键位（周线 / 月线 / 年线） =================
   数据来自 levels.json。压力位取「现价上方最近的摆动前高」，
   支撑位取「现价下方最近的摆动前低」—— 已经被突破的位置没有参考价值，
   所以后端只会返回对应方向的档位。
   ================================================================= */

function fmtPct(v, digits = 2) {
  if (v == null || !Number.isFinite(v)) return '—';
  return (v >= 0 ? '+' : '') + v.toFixed(digits) + '%';
}

/** 位置分位 → 色档。高位偏红、低位偏绿，与 A 股涨跌配色一致 */
function posClass(p) {
  if (p == null || !Number.isFinite(p)) return '';
  if (p >= 0.66) return 'hi';
  if (p >= 0.33) return 'mid';
  return 'lo';
}

function levelCell(x, kind) {
  if (!x) return `<span class="lv-none">${kind === 'res' ? '上方无压力' : '下方无支撑'}</span>`;
  // touches>1：该位置被反复测试过（后端只对日线档做了同价位合并，故只有日线会出现）
  const t = x.touches > 1 ? `<u class="lvtouch">×${x.touches}</u>` : '';
  return `<span class="lv-${kind}${x.near ? ' near' : ''}">${x.price.toFixed(2)}${t}`
    + `<i>${fmtPct(x.gap_pct)}</i></span>`;
}

/* 均线体系（5/10/20/30/60 日线 + 年线）—— 动态支撑压力。
   现价在均线**上方** → 该均线是支撑；在**下方** → 是压力。所以同一张表里
   既有支撑也有压力，不能按「压力位一列 / 支撑位一列」摆（那栏位会空一半）。 */
function maRows(ma) {
  if (!ma || !ma.lines || !ma.lines.length) return '';
  const rows = ma.lines.map((l) => {
    const sup = l.role === 'support';
    const s = l.slope_pct;
    const arrow = s == null ? '' : (s > 0.05 ? '↗' : (s < -0.05 ? '↘' : '→'));
    const scls = s == null ? '' : (s > 0.05 ? 'up' : (s < -0.05 ? 'dn' : ''));
    return `<div class="ma-row ${sup ? 'sup' : 'res'}">
      <b>${l.label}<u class="maslope ${scls}">${arrow}</u></b>
      <span class="ma-price">${l.price.toFixed(2)}</span>
      <span class="ma-gap">${fmtPct(l.gap_pct)}</span>
      <em class="ma-role">${sup ? '支撑' : '压力'}</em>
    </div>`;
  }).join('');
  const arr = ma.arrangement
    ? `<em class="ma-arr ${ma.arrangement === '多头排列' ? 'up'
        : (ma.arrangement === '空头排列' ? 'dn' : '')}">${ma.arrangement}</em>` : '';
  return `<div class="ma-wrap">
    <div class="ma-th"><b>均线${arr}</b><span>点位</span><span>距离</span><em>性质</em></div>
    ${rows}
    ${ma.text ? `<p class="ma-sum">${ma.text}</p>` : ''}
  </div>`;
}

function renderLevels(lv, sel = 0) {
  if (!lv || !lv.indices || !lv.indices.length) return '';
  const i = Math.min(Math.max(sel, 0), lv.indices.length - 1);
  const I = lv.indices[i];

  const seg = lv.indices.length > 1
    ? `<div class="seg" id="idxSeg">${lv.indices.map((x, k) =>
        `<button data-i="${k}" class="${k === i ? 'on' : ''}">${x.name}</button>`).join('')}</div>`
    : '';

  const rows = I.periods.map((p) => `<div class="lv-row">
      <b>${p.label}</b>${levelCell(p.resistance[0], 'res')}${levelCell(p.support[0], 'sup')}
    </div>`).join('');

  const posLine = I.periods.map((p) => `<span><b>${p.label}</b>`
    + `${p.position == null ? '—' : (p.position * 100).toFixed(0) + '%'}`
    + `<em>${p.position_text || ''}</em></span>`).join('');

  const notes = (I.summary?.notes || []).map((n) => `<li>${n}</li>`).join('');

  const all = I.periods.map((p) => `<div class="lv-allp">
      <b>${p.label}<em>${p.bars} 根</em></b>
      <div><span>压力</span>${p.resistance.map((x) => x.price.toFixed(0)).join(' / ') || '—'}</div>
      <div><span>支撑</span>${p.support.map((x) => x.price.toFixed(0)).join(' / ') || '—'}</div>
    </div>`).join('');

  return `<section class="card lv">
    <div class="lv-head"><h3>大盘关键位</h3>${seg}</div>
    <p class="lv-quote"><b>${I.name}</b><span class="lv-close">${I.close}</span>
      <em class="${(I.pct ?? 0) >= 0 ? 'up' : 'dn'}">${fmtPct(I.pct)}</em>
      <span class="lv-date">${I.date}</span></p>
    ${maRows(I.ma)}
    <p class="lv-cap">摆动前高 / 前低（静态价格记忆，不会随行情移动）</p>
    <div class="lv-grid">
      <div class="lv-row lv-th"><b></b><span>压力位</span><span>支撑位</span></div>
      ${rows}
    </div>
    <p class="lv-pos">${posLine}</p>
    ${I.summary?.text ? `<p class="lv-sum">${I.summary.text}</p>` : ''}
    ${notes ? `<ul class="lv-notes">${notes}</ul>` : ''}
    <details class="lv-more"><summary>全部档位</summary>${all}</details>
    <p class="src">均线取 5 / 10 / 20 / 30 / 60 日线与年线（250 日）：现价在均线上方即为
      支撑、下方即为压力，↗↘ 是这条均线近 5 日斜率（走平的支撑/压力最硬）。
      摆动档位则取日线 120 个交易日内 k=5 的摆动高低点，并把 0.8% 内的同价位合并成
      「位置区」（×N 表示被测试 N 次，次数越多越硬）；周线 / 月线 / 年线取各自周期的
      摆动高低点。压力位只列现价上方、支撑位只列现价下方 —— 已被突破的位置没有参考价值。</p>
  </section>`;
}

/* ================== 主力资金（大盘沪深两市 + 板块排行） ==================
   数据来自 flow.json：
   - market   沪 / 深 / 合计的当日累计主力净流入与五档拆解（亿元）
   - curve    分时累计曲线（**自开盘累计**，不是每分钟增量）
   - history  近 N 个交易日趋势 —— 本系统**自己逐日攒的**（见 pipeline/flow.py）
   - boards   板块主力净流入排行（后端值）

   盘中还有一路更快的来源：liveSnapshot() 直接抓东财 clist 的 f62/f184，
   那是**此刻**的板块值，用它覆盖后端榜单并把来源标成「实时」。

   ⚠️ 大盘合计**没法**做前端实时：fflow 接口不吃 `cb=` 回调参数（实测加了 cb 直接 502），
   浏览器跨域也拿不到 —— 它只能靠后端每 30 分钟一版。所以这一块必须显示
   时间戳 + 相对时间，让人一眼看出它有多旧（数据是几点几分的就是几点几分的，不装新）。
   ========================================================================= */

function fmtYi(v, digits = 1) {
  if (v == null || !Number.isFinite(v)) return '—';
  return (v > 0 ? '+' : '') + v.toFixed(digits) + ' 亿';
}

/** 极简折线：只画趋势，不画坐标轴（手机上没人读轴）。
 *  0 轴一定画进来 —— 否则「全红」或「全绿」的一段会被归一化成看着很剧烈的曲线。 */
function spark(vals, w = 300, h = 56) {
  const v = (vals || []).map((x) => (Number.isFinite(x) ? x : null));
  const idx = v.map((x, i) => [i, x]).filter(([, x]) => x != null);
  if (idx.length < 2) return '';
  let lo = Math.min(...idx.map(([, x]) => x), 0);
  let hi = Math.max(...idx.map(([, x]) => x), 0);
  if (hi === lo) hi = lo + 1;
  const pad = 6;
  const px = (i) => pad + i * (w - 2 * pad) / (v.length - 1);
  const py = (y) => pad + (hi - y) / (hi - lo) * (h - 2 * pad);
  const pts = idx.map(([i, x]) => `${px(i).toFixed(1)},${py(x).toFixed(1)}`).join(' ');
  const col = idx[idx.length - 1][1] >= 0 ? 'var(--up)' : 'var(--down)';
  return `<svg class="fspark" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none"
    role="img" aria-label="累计主力净流入走势">
    <line x1="${pad}" y1="${py(0).toFixed(1)}" x2="${w - pad}" y2="${py(0).toFixed(1)}"
      stroke="#3d444d" stroke-width="1" stroke-dasharray="3 3"/>
    <polyline points="${pts}" fill="none" stroke="${col}" stroke-width="1.6"/>
  </svg>`;
}

const yiCls = (v) => (v == null || !Number.isFinite(v) ? '' : (v >= 0 ? 'up' : 'dn'));

function flowMarketCard(fl) {
  const mk = fl?.market || {};
  const t = mk.total || {};
  const sh = mk.sh || {}, sz = mk.sz || {};
  const legs = [['超大单', t.xlarge_yi], ['大单', t.large_yi],
                ['中单', t.medium_yi], ['小单', t.small_yi]]
    .map(([k, v]) => `<div class="fleg"><span>${k}</span>
        <b class="${yiCls(v)}">${fmtYi(v)}</b></div>`).join('');
  const side = (lb, o) => `<span><b>${lb}</b>
      <i class="${yiCls(o.main_yi)}">${fmtYi(o.main_yi)}</i>
      <u>${o.main_pct == null || !Number.isFinite(o.main_pct) ? '' : o.main_pct + '%'}</u></span>`;
  return `<section class="card">
    <h3>主力资金 · 大盘</h3>
    <p class="flow-big ${yiCls(t.main_yi)}">${fmtYi(t.main_yi)}
      <em>两市合计主力净流入</em></p>
    <div class="flegs">${legs}</div>
    ${spark(fl?.curve?.sum)}
    <div class="fside">${side('沪市', sh)}${side('深市', sz)}</div>
    <p class="flow-ts ${isStale(fl?.updated) ? 'stale' : ''}">更新 ${fl?.updated || '—'}
      <i class="ago" id="flowAgo">${agoText(fl?.updated)}</i></p>
    <p class="src">曲线为自开盘累计的主力净流入（亿元），最后一点即当日累计值；
      主力 = 大单 + 超大单，四档（超大 / 大 / 中 / 小）互补、合计为 0。
      板块榜单有实时来源，这一块<b>没有</b> —— 它随后端刷新，看的应是上面那行时间。</p>
  </section>`;
}

function flowHistoryCard(fl) {
  const h = fl?.history || {};
  const days = h.days || 0;
  if (!days) {
    return `<section class="card"><h3>主力资金 · 近 20 日趋势</h3>
      <p class="src">还没有历史数据。这段趋势是本系统<b>逐日累积</b>的
        —— 东财的历史资金流接口在海外服务器上取不到（实测 push2his 连不上、
        其余只回当天 1 条），所以今天起步、明天起逐日增加，攒满 20 天约需一个月。</p>
    </section>`;
  }
  const n = Math.min(days, 10);
  const rows = [];
  for (let i = h.dates.length - 1; i >= Math.max(0, h.dates.length - n); i--) {
    const v = (h.sum || [])[i];
    rows.push(`<div class="fday"><span>${String(h.dates[i]).slice(5)}</span>
      <b class="${yiCls(v)}">${fmtYi(v, 2)}</b></div>`);
  }
  return `<section class="card">
    <h3>主力资金 · 近 ${days} 个交易日${days < 20 ? `（攒到 20 天约需一个月）` : ''}</h3>
    ${spark(h.sum, 300, 46)}
    <div class="fdays">${rows.join('')}</div>
    <p class="src">这段历史是<b>本系统自己逐日记录的</b>，不是数据源给的现成序列；
      当日值在盘中会被最新一次刷新覆盖，收盘那次就是最终值。</p>
  </section>`;
}

function flowBoardsCard(fl, live) {
  const isLive = !!(live && live.length);
  const src = isLive ? live : (fl?.boards || []);
  if (!src.length) {
    return `<section class="card"><h3>板块主力资金</h3>
      <p class="src">暂无板块资金流数据（本轮 clist 快照可能失败）。</p></section>`;
  }
  const row = (b) => `<button class="srow frow" data-code="${b.code}">
      <span class="snm">${b.name}${Number.isFinite(b.pct)
        ? `<em>${fmtPct(b.pct, 2)}</em>` : ''}</span>
      <span class="sval ${(b.main_yi || 0) >= 0 ? 'rb' : 'dd'}">${fmtYi(b.main_yi, 2)}</span>
      <span class="sval mut">${Number.isFinite(b.main_pct) ? fmtPct(b.main_pct, 2) : '—'}</span>
    </button>`;
  const ups = src.filter((b) => (b.main_yi || 0) > 0).slice(0, 10);
  const dns = src.slice().reverse().filter((b) => (b.main_yi || 0) < 0).slice(0, 10);
  const st = fl?.stats || {};
  const inflowAll = isLive ? src.filter((b) => (b.main_yi || 0) > 0).length
    : (st.inflow_n ?? 0);
  const outflowAll = isLive ? src.filter((b) => (b.main_yi || 0) < 0).length
    : (st.outflow_n ?? 0);
  const head = `<div class="shead frow"><span>板块</span><span>主力净额</span>
      <span>净占比</span></div>`;
  const block = (title, arr) => arr.length ? `<h3 class="fsub">${title}</h3>
      ${head}${arr.map(row).join('')}` : '';
  return `<section class="card">
    <h3>板块主力资金 ${isLive ? '<i class="flive">实时</i>' : ''}</h3>
    <p class="fstat">净流入 <b class="up">${inflowAll}</b> 个
      · 净流出 <b class="dn">${outflowAll}</b> 个
      · 板块合计 <b class="${yiCls(st.net_yi)}">${fmtYi(st.net_yi)}</b></p>
    ${block('主力净流入 TOP10', ups)}
    ${block('主力净流出 TOP10', dns)}
    <p class="src">${isLive
      ? '以上为浏览器直连东财的<b>实时</b>值（f62 主力净额 / f184 净占比），与页面同时刻。'
      : '以上为后端最近一版产物里的值，点开时若网络可达会自动换成实时值。'}
      「净占比」= 主力净额 ÷ 成交额，用于横向比较不同体量的板块；点任意一行看板块详情。</p>
  </section>`;
}

function renderFlow(fl, live) {
  if (!fl) {
    return `<div class="skel">暂无主力资金数据<br>
      它由 pipeline/flow.py 生成 flow.json，请先跑一次 score</div>`;
  }
  return flowMarketCard(fl) + flowHistoryCard(fl) + flowBoardsCard(fl, live);
}

/** 板块资金流的实时值：复用实时补丁已经抓回来的那份快照，不额外请求 */
async function liveFlowBoards() {
  try {
    const snap = await liveSnapshot();
    if (!snap || !snap.length) return null;
    const uni = new Set(state.idx.map((b) => b.code));
    const rows = snap
      .filter((r) => uni.has(r.code) && Number.isFinite(r.mainInflow))
      .map((r) => ({ code: r.code, name: r.name, main_yi: r.mainInflow / 1e8,
                     main_pct: r.mainPct, pct: r.pct }));
    if (!rows.length) return null;
    rows.sort((a, b) => b.main_yi - a.main_yi);
    return rows;
  } catch { return null; }
}

/* ==================== 事件日历（宏观 / 政策 / 市场事件） ====================
   数据来自 events.json。每条事件都带 level（3 高 / 2 中 / 1 低）与 src 出处：
   - eastmoney 东财财经日历真实事件表
   - rule      规则推算（如每月第 3 个周五 = 股指期货交割日）
   - release   本仓库解禁数据（与筹码供给罚分同源）
   - curated   人工惯例清单，前端会标注「惯例」——不把推断日期伪装成官方日期
   ============================================================================ */

const EV_CATS = [['all', '全部'], ['us', '美国'], ['cn', '中国'],
                 ['global', '海外'], ['market', 'A股']];
const EV_LEVELS = [[3, '仅高'], [2, '高 + 中'], [1, '全部']];
const EV_SRC_TAG = { rule: '规则', release: '解禁', curated: '惯例' };

/** 事件日期距今天数（负数 = 已过去） */
function dayDiff(ds) {
  const t = new Date(); t.setHours(0, 0, 0, 0);
  return Math.round((new Date(`${ds}T00:00:00`) - t) / 86400000);
}

function relDay(n) {
  if (n === 0) return '今天';
  if (n === 1) return '明天';
  if (n === 2) return '后天';
  if (n === -1) return '昨天';
  return n > 0 ? `${n} 天后` : `${-n} 天前`;
}

function evFiltered() {
  const ev = state.events?.events || [];
  const minLv = Number(state.evLevel) || 2;
  return ev.filter((e) => e.level >= minLv
    && (state.evCat === 'all' || e.cat === state.evCat));
}

/** 未来 N 天内的高优先事件数（用于 tab 徽章） */
function upcomingHigh(days = 7) {
  const ev = state.events?.events || [];
  return ev.filter((e) => e.level >= 3 && dayDiff(e.date) >= 0 && dayDiff(e.date) <= days);
}

function evRow(e) {
  const lv = e.level >= 3 ? 'lv3' : (e.level === 2 ? 'lv2' : 'lv1');
  const stars = e.level >= 3 ? '★★★' : (e.level === 2 ? '★★' : '★');
  const tag = EV_SRC_TAG[e.src] ? `<i class="evsrc">${EV_SRC_TAG[e.src]}</i>` : '';
  const cat = state.events?.cats?.[e.cat]?.label || e.cat;
  const detail = (e.detail || []).length
    ? `<details class="evmore"><summary>${e.n > 1 ? `${e.n} 条明细` : '明细'}</summary>
         <ul>${(e.detail || []).map((d) => `<li>${d}</li>`).join('')}</ul></details>`
    : '';
  return `<div class="ev ${lv}">
    <span class="evmk">${stars}</span>
    <div class="evmain">
      <p class="evt">${e.time ? `<b>${e.time}</b>` : ''}${e.name}
        <i class="evcat ${e.cat}">${cat}</i>${tag}</p>
      ${e.level >= 2 && e.why ? `<p class="evw">${e.why}</p>` : ''}
      ${detail}
    </div>
  </div>`;
}

function renderEvents() {
  const ev = state.events;
  if (!ev || !ev.events) return '<section class="card"><h3>事件日历</h3>'
    + '<p class="src">暂无事件日历数据（需先跑一次 warmup + score 生成 events.json）</p></section>';

  const rows = evFiltered();
  // 按日期分组
  const groups = [];
  for (const e of rows) {
    const g = groups[groups.length - 1];
    if (g && g.date === e.date) g.items.push(e);
    else groups.push({ date: e.date, weekday: e.weekday, items: [e] });
  }

  const nh = ev.next_high;
  const nhLine = nh ? `<p class="evnext">最近的高优先事件：
      <b>${nh.date} ${nh.weekday || ''}${nh.time ? ' ' + nh.time : ''}</b>
      ${nh.name}（${relDay(dayDiff(nh.date))}）</p>` : '';

  const catSeg = EV_CATS.map(([k, lb]) =>
    `<button data-c="${k}" class="${state.evCat === k ? 'on' : ''}">${lb}</button>`).join('');
  const lvSeg = EV_LEVELS.map(([k, lb]) =>
    `<button data-l="${k}" class="${Number(state.evLevel) === k ? 'on' : ''}">${lb}</button>`).join('');

  const body = groups.length ? groups.map((g) => {
    const n = dayDiff(g.date);
    const cls = n < 0 ? ' past' : (n <= 2 ? ' soon' : '');
    return `<div class="evday${cls}">
      <div class="evdh"><b>${g.date.slice(5)}</b><em>${g.weekday || ''}</em>
        <span class="evrel">${relDay(n)}</span>
        <span class="evcnt">${g.items.length}</span></div>
      ${g.items.map(evRow).join('')}
    </div>`;
  }).join('') : '<p class="src">当前筛选条件下没有事件</p>';

  const c = ev.counts || {};
  return `<section class="card">
    <h3>事件日历 · ${rows.length} / ${ev.count} 条</h3>
    ${nhLine}
    <div class="seg wrap" id="evCatSeg">${catSeg}</div>
    <div class="seg wrap" id="evLvSeg">${lvSeg}</div>
    <div class="evlist">${body}</div>
    <p class="src">★★★ 高优先（可能引起较大波动）：美联储议息 / 非农 / 美国 CPI·PCE /
      中国 CPI·PPI·LPR·GDP / 政策会议 / 大额解禁。<br>
      覆盖未来 ${ev.horizon_days || 120} 天、含近 ${ev.lookback_days || 7} 天回顾；
      共 高 ${c.high ?? 0} / 中 ${c.mid ?? 0} / 低 ${c.low ?? 0} 条。<br>
      来源：${(ev.sources || []).join('；')}。<br>
      时间为北京时间；「惯例」标记的事件日期为惯例推算，以官方公告为准。</p>
  </section>`;
}

/** tab 上的徽章：未来 7 天的高优先事件数 */
function paintTabBadge() {
  const btn = document.querySelector('#tabs button[data-view="events"]');
  if (!btn) return;
  const n = upcomingHigh(7).length;
  btn.innerHTML = `事件日历${n ? `<i class="tabbadge">${n}</i>` : ''}`;
}

/* ==================== 回撤 / 涨幅列表 ==================== */

const SWING_WIN = [['20', '20 日'], ['60', '60 日'], ['250', '近一年']];

const SWING_SORTS = {
  dd: ['回撤深 → 浅', (a, b) => (a.dd ?? 0) - (b.dd ?? 0)],
  rb: ['反弹大 → 小', (a, b) => (b.rb ?? 0) - (a.rb ?? 0)],
  pos: ['位置低 → 高', (a, b) => (a.pos ?? 2) - (b.pos ?? 2)],
  score: ['评分高 → 低', (a, b) => (b.score ?? 0) - (a.score ?? 0)],
};

function swingRows(win) {
  return state.idx.map((b) => {
    const w = (b.swing || {})[String(win)] || {};
    return { code: b.code, name: b.name, parent: b.parent || '',
             score: b.score, dd: w.dd, rb: w.rb, pos: w.pos, dsh: w.dsh };
  }).filter((r) => r.dd != null || r.rb != null);
}

function renderSwingList() {
  const win = String(state.win);
  const cmp = SWING_SORTS[state.sort][1];
  const rows = swingRows(win).sort(cmp);

  const winSeg = SWING_WIN.map(([k, lb]) =>
    `<button data-w="${k}" class="${win === k ? 'on' : ''}">${lb}</button>`).join('');
  const sortSeg = Object.entries(SWING_SORTS).map(([k, [lb]]) =>
    `<button data-s="${k}" class="${state.sort === k ? 'on' : ''}">${lb}</button>`).join('');

  const items = rows.map((r) => `<button class="srow" data-code="${r.code}">
      <span class="snm">${r.name}${r.parent ? `<em>${r.parent}</em>` : ''}</span>
      <span class="sval dd">${r.dd == null ? '—' : fmtPct(r.dd * 100, 1)}</span>
      <span class="sval rb">${r.rb == null ? '—' : fmtPct(r.rb * 100, 1)}</span>
      <span class="sbar"><i class="${posClass(r.pos)}"
        style="width:${r.pos == null ? 0 : (r.pos * 100).toFixed(0)}%"></i></span>
    </button>`).join('');

  return `<section class="card">
    <h3>区间位置 · ${rows.length} 个板块</h3>
    <div class="seg wrap" id="winSeg">${winSeg}</div>
    <div class="seg wrap" id="sortSeg">${sortSeg}</div>
    <div class="shead"><span>板块</span><span>距高点</span><span>距低点</span><span>位置</span></div>
    <div class="slist">${items}</div>
    <p class="src">「距高点」= 现价相对区间最高价（负值即回撤）；「距低点」= 现价相对区间最低价；
      位置条为现价在区间中的分位，越靠右越高。点任意一行看板块详情。</p>
  </section>`;
}

/* ============================== 主流程 ============================== */

const state = {
  idx: [], meta: {}, selected: [], current: null, version: '',
  view: 'score',      // score | market（大盘）| flow（主力资金）| swing | events
  win: 250,           // 回撤/涨幅的统计窗口（交易日）
  sort: 'dd',         // 回撤列表排序键
  levels: null,       // 大盘关键位（levels.json：均线 + 摆动档位）
  idxSel: 0,          // 当前查看的指数下标
  events: null,       // 事件日历（events.json）
  evCat: 'all',       // 事件类别筛选
  evLevel: 2,         // 事件优先级下限（默认只看 高 + 中，低优先靠筛选展开）
  flow: null,         // 主力资金（flow.json）
  liveFlow: null,     // 板块资金流的实时值（浏览器直连 clist 折算，可能为 null）
};

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

function paintLevels() {
  $('#lvCard').innerHTML = renderLevels(state.levels, state.idxSel);
}

function syncTabs() {
  document.querySelectorAll('#tabs button').forEach((b) =>
    b.classList.toggle('on', b.dataset.view === state.view));
}

async function renderCurrent() {
  $('#macro').innerHTML = renderMacroBar(state.meta);

  // 「大盘」视图：内容由 levels.json 驱动，装在 #body 之外的 #lvCard 里
  // （避免被其它视图的整块重绘冲掉），所以这里只切换显隐 + 重绘它本身。
  // 想让它恢复「常驻显示」，把下面这行改成 `$('#lvCard').hidden = false;` 即可。
  $('#lvCard').hidden = state.view !== 'market';
  if (state.view === 'market') paintLevels();

  // 非评分视图（大盘 / 主力资金 / 回撤 / 事件日历）：只重绘自己的内容，
  // 不加载任何板块详情、不显示自选芯片
  if (state.view !== 'score') {
    $('#chips').hidden = true;
    if (state.view === 'market') { $('#body').innerHTML = ''; return; }
    if (state.view === 'flow') {
      // 先渲染后端值（秒出），再尝试换成实时值 —— 换失败了也已经有内容
      $('#body').innerHTML = renderFlow(state.flow, state.liveFlow);
      const lb = await liveFlowBoards();
      if (lb) { state.liveFlow = lb; $('#body').innerHTML = renderFlow(state.flow, lb); }
      return;
    }
    $('#body').innerHTML = state.view === 'events' ? renderEvents() : renderSwingList();
    return;
  }
  $('#chips').hidden = false;

  const code = state.current;
  if (!code) return;

  // 宏观卡与详情一起渲染，避免被后续 innerHTML 覆盖
  const paint = (html) => { $('#body').innerHTML = macroCard(state.meta) + html; };

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

  // 大盘关键位：拿不到就整块隐藏，不能影响评分主流程
  try {
    state.levels = await fetchJSON(`${DATA}levels.json`);
  } catch {
    state.levels = null;
  }

  // 事件日历：同样允许失败（缺 events.json 时该视图给出提示，不影响其它视图）
  try {
    state.events = await fetchJSON(`${DATA}events.json`);
  } catch {
    state.events = null;
  }

  // 主力资金：同样允许失败（后端这块可能因东财限流为空）
  try {
    state.flow = await fetchJSON(`${DATA}flow.json`);
  } catch {
    state.flow = null;
  }
  paintTabBadge();

  demoBanner(meta);   // 合成演示数据必须显著标注，避免误当真数据
  startFreshWatch(meta);   // 盘中每 30 分钟一版新产物：发现新版只提示，不打断

  // 相对时间每 30 秒自更新，否则「3 分钟前」会永远停在打开页面的那一刻
  setInterval(() => {
    const el = document.getElementById('agoTs');
    if (el) el.textContent = agoText(state.meta?.updated);
    const fl = document.getElementById('flowAgo');
    if (fl) fl.textContent = agoText(state.flow?.updated);
  }, 30000);

  // 自选要按当前板块宇宙过滤：切换层级（如一级 31 → 二级 127）后，
  // localStorage 里存的是旧代码，不过滤会出现「选中了但列表里找不到」
  const valid = new Set(state.idx.map((b) => b.code));
  state.selected = ls(LS_SEL, []).filter((c) => valid.has(c));
  if (!state.selected.length) {
    state.selected = state.idx.slice(0, 5).map((b) => b.code);
    lsSet(LS_SEL, state.selected);
  }
  state.current = state.selected[0];

  // 在详情区顶部插入宏观卡（每次渲染前重建）
  bindSheet();
  syncTabs();

  $('#tabs').addEventListener('click', (e) => {
    const b = e.target.closest('button[data-view]');
    if (!b) return;
    state.view = b.dataset.view;
    syncTabs();
    renderCurrent();
  });

  $('#chips').addEventListener('click', (e) => {
    if (e.target.closest('#btnManage')) { openSheet(); return; }
    const chip = e.target.closest('.chip');
    if (!chip || !chip.dataset.code) return;
    state.current = chip.dataset.code;
    renderCurrent();
  });

  // 回撤列表里的窗口 / 排序切换与行点击（委托到 #body，
  // 因为列表内容每次重绘都会被替换，绑在子元素上会失效）
  $('#body').addEventListener('click', (e) => {
    const w = e.target.closest('#winSeg button');
    if (w) { state.win = Number(w.dataset.w); renderCurrent(); return; }
    const s = e.target.closest('#sortSeg button');
    if (s) { state.sort = s.dataset.s; renderCurrent(); return; }
    const c = e.target.closest('#evCatSeg button');
    if (c) { state.evCat = c.dataset.c; renderCurrent(); return; }
    const l = e.target.closest('#evLvSeg button');
    if (l) { state.evLevel = Number(l.dataset.l); renderCurrent(); return; }
    const row = e.target.closest('.srow');
    if (row && row.dataset.code) {
      state.current = row.dataset.code;   // 临时查看，不改动自选
      state.view = 'score';
      syncTabs();
      renderCurrent();
      window.scrollTo({ top: 0, behavior: 'smooth' });
    }
  });

  $('#lvCard').addEventListener('click', (e) => {
    const b = e.target.closest('#idxSeg button');
    if (!b) return;
    state.idxSel = Number(b.dataset.i);
    paintLevels();
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
