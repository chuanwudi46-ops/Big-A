/* 前端渲染契约自测（无需浏览器）
 *
 * 为什么需要：`web/app.js` 里的渲染逻辑跑在浏览器里，CI 和本机都测不到。
 * 一旦产物字段改名（比如 swing 的窗口键、levels 的档位结构变了），
 * 页面上只会静默地少显示一块内容（甚至白屏），没有任何报错。
 *
 * 做法：把 app.js 整份丢进 node vm、禁用 boot()、塞入最小 DOM stub，
 * 再用**本地真实产物**（web/data 下的 index.json / levels.json）调用渲染函数，
 * 断言输出的 HTML 里确实出现了关键内容。
 *
 * 用法：node tools/check_render.mjs
 * 退出码 0 = 通过，1 = 不通过。
 */
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const DATA = path.join(ROOT, 'web', 'data');

let failed = 0;
const ok = (cond, label, extra = '') => {
  console.log(`${cond ? '✓' : '✗'} ${label}${extra ? '  ' + extra : ''}`);
  if (!cond) failed++;
};

/* ---------- 1. 把 app.js 装进 vm，禁用 boot ---------- */
let src = fs.readFileSync(path.join(ROOT, 'web', 'app.js'), 'utf8');
if (!/\bboot\(\)\s*;/.test(src)) {
  console.error('✗ 未能在 app.js 里找到 boot() 调用，测试脚本需要同步更新');
  process.exit(1);
}
src = src.replace(/(^|\n)boot\(\)\s*;/, '$1/* boot() disabled by check_render */');

const ctx = {
  console, JSON, Math, Array, Promise, Object, String, Number, Error, Date,
  setTimeout, clearTimeout, isNaN, parseFloat, parseInt,
  fetch: () => Promise.reject(new Error('offline')),
  document: {
    querySelector: () => null,
    querySelectorAll: () => [],
    addEventListener: () => {},
    createElement: () => ({ style: {}, classList: { toggle() {}, add() {} } }),
  },
  window: { addEventListener: () => {}, scrollTo: () => {} },
  localStorage: { getItem: () => null, setItem: () => {} },
  navigator: {},
};
vm.createContext(ctx);
try {
  vm.runInContext(src, ctx);
  ok(true, 'app.js 可在无浏览器环境下加载（无顶层 DOM 依赖）');
} catch (e) {
  ok(false, 'app.js 加载失败', String(e).slice(0, 120));
  process.exit(1);
}

/* ---------- 2. 大盘关键位 ---------- */
const lvFp = path.join(DATA, 'levels.json');
if (!fs.existsSync(lvFp)) {
  console.log('! 缺少 web/data/levels.json，跳过（先跑一次 pipeline）');
} else {
  const lv = JSON.parse(fs.readFileSync(lvFp, 'utf8'));
  ctx.__lv = lv;
  const html = vm.runInContext('renderLevels(__lv, 0)', ctx);
  const I = lv.indices[0];
  ok(html.includes(I.name), 'levels 卡片渲染出指数名', I.name);
  ok(html.includes(String(I.close)), 'levels 卡片渲染出点位', String(I.close));
  for (const p of I.periods) {
    ok(html.includes(p.label), `levels 卡片含「${p.label}」行`);
  }
  // 日线档是 2026-09-16 新增的：必须有，且 must be the first row
  const keys = I.periods.map((p) => p.key);
  ok(keys.includes('day'), 'levels 含日线档（key=day）', keys.join('/'));
  ok(keys[0] === 'day', '日线档排在第一位');
  const dayp = I.periods.find((p) => p.key === 'day');
  ok(dayp && dayp.bars === 120, '日线档回看 120 根', dayp ? String(dayp.bars) : 'missing');
  ok(dayp && (dayp.resistance.length + dayp.support.length) > 0,
    '日线档至少有一档压力或支撑',
    dayp ? `压力 ${dayp.resistance.length} / 支撑 ${dayp.support.length}` : 'missing');
  const lvLen = dayp ? dayp.resistance.length + dayp.support.length : 0;
  const lvHi = dayp ? dayp.resistance.filter((x) => x.gap_pct > 0).length +
                      dayp.support.filter((x) => x.gap_pct < 0).length : 0;
  ok(lvLen > 0 && lvLen === lvHi, '日线压力全部在现价上方、支撑全部在下方',
    `${lvHi}/${lvLen}`);
  ok(/lv-res/.test(html) || /lv-none/.test(html), '压力位单元格已渲染（有档位或"上方无压力"）');
  ok(/lv-sup/.test(html) || /lv-none/.test(html), '支撑位单元格已渲染');
  ok((I.summary?.text || '').length === 0 || html.includes(I.summary.text.slice(0, 12)),
    '综合结论文案已渲染');
  ok(/lv-pos/.test(html), '区间位置行已渲染');
  // 日线位置区标记：只在有 touches>1 时出现，出现了就必须渲染出来
  const touched = (dayp ? dayp.resistance.concat(dayp.support) : [])
    .filter((x) => x.touches > 1);
  ok(touched.length === 0 || /lvtouch/.test(html),
    '日线「位置区 ×N」标记已渲染', `带 touches 的档位 ${touched.length} 个`);

  /* ---- 均线体系（2026-09-22 新增：5/10/20/30/60 日线 + 年线）----
     这组断言的重点不是「有没有渲染」，而是**性质与位置是否自洽**：
     均线的支撑/压力是由「现价在它上方还是下方」推出来的，
     一旦代码把 role 写死，页面照样显示得很正常 —— 必须靠这里抓。 */
  const ma = I.ma;
  ok(!!ma && Array.isArray(ma.lines) && ma.lines.length > 0,
    'levels 含均线体系', ma ? `${ma.lines.length} 条` : 'missing');
  if (ma && ma.lines.length) {
    const need = ['ma5', 'ma10', 'ma20', 'ma30', 'ma60', 'ma250'];
    const got = ma.lines.map((l) => l.key);
    ok(need.every((k) => got.includes(k)),
      '均线覆盖 5/10/20/30/60 日线与年线', got.join('/'));
    const badRole = ma.lines.find((l) =>
      (l.above && l.role !== 'support') || (!l.above && l.role !== 'resistance'));
    ok(!badRole, '均线性质与「现价在上方/下方」自洽（上方=支撑、下方=压力）',
      badRole ? JSON.stringify(badRole).slice(0, 90) : '');
    const badGap = ma.lines.find((l) =>
      !Number.isFinite(l.price) || !Number.isFinite(l.gap_pct)
      || (l.above && l.gap_pct < 0) || (!l.above && l.gap_pct > 0));
    ok(!badGap, '均线价格与距离符号自洽', badGap ? JSON.stringify(badGap).slice(0, 90) : '');
    ok(ma.lines.every((l) => Number.isFinite(l.price) && typeof l.label === 'string'
      && l.label.length > 0), '每条均线都有点位与中文标签');
    ok(/ma-wrap/.test(html), '均线区块已渲染');
    const maRows = (html.match(/class="ma-row /g) || []).length;
    ok(maRows === ma.lines.length, '均线行数与数据条数一致',
      `${maRows} 行 / ${ma.lines.length} 条`);
    ok(ma.lines.some((l) => html.includes(l.label)), '均线标签渲染进页面');
    ok(html.includes('lv-cap'), '均线区块与摆动档位分开标注');
  }
}

/* ---------- 2b. 事件日历 ---------- */
const evFp = path.join(DATA, 'events.json');
if (!fs.existsSync(evFp)) {
  console.log('! 缺少 web/data/events.json，跳过（先跑一次 pipeline）');
} else {
  const ev = JSON.parse(fs.readFileSync(evFp, 'utf8'));
  ctx.__ev = ev;
  vm.runInContext('state.events = __ev; state.evCat = "all"; state.evLevel = 2;', ctx);

  ok(Array.isArray(ev.events) && ev.events.length > 0, 'events.json 含事件列表',
    `${ev.events?.length} 条`);
  ok(ev.sources && ev.sources.length > 0, 'events.json 标注了数据来源');
  const badField = ev.events.find((e) => !e.date || !e.name || !e.cat
    || ![1, 2, 3].includes(e.level) || !e.src);
  ok(!badField, '每条事件都有 date/name/cat/level/src',
    badField ? JSON.stringify(badField).slice(0, 90) : '');
  const badDate = ev.events.find((e) => !/^\d{4}-\d{2}-\d{2}$/.test(e.date));
  ok(!badDate, '事件日期均为 YYYY-MM-DD 格式', badDate ? badDate.date : '');
  const capped = ev.events.filter((e) => e.level >= 2).length;
  ok(ev.events.length <= 400, '事件条数在合理上限内', `${ev.events.length} 条（高+中 ${capped}）`);
  const future = ev.events.filter((e) => e.date >= ev.as_of);
  ok(future.length > 0, '存在未来事件', `${future.length} 条`);

  // 高优先事件里应该能覆盖用户最关心的四类
  const all = ev.events.map((e) => e.name + (e.detail || []).join(' ')).join(' ');
  for (const kw of ['美联储', '非农', 'CPI', 'LPR']) {
    ok(all.includes(kw), `高优先事件覆盖「${kw}」`);
  }

  const html = vm.runInContext('renderEvents()', ctx);
  ok(html.includes('事件日历'), '事件日历卡片已渲染');
  ok(html.includes('id="evCatSeg"') && html.includes('id="evLvSeg"'),
    '事件筛选控件（类别 / 优先级）已渲染');
  const n3 = ev.events.filter((e) => e.level >= 3).length;
  const shown = (html.match(/class="ev lv/g) || []).length;
  ok(n3 === 0 || shown >= n3, '高优先事件全部渲染出来',
    `渲染 ${shown} 行 / 高优先 ${n3} 条`);
  const days = (html.match(/class="evday/g) || []).length;
  ok(days > 0, '事件按日期分组渲染', `${days} 组`);

  // 优先级筛选：只显示高优先时行数必须变少且不含 lv1
  vm.runInContext('state.evLevel = 3;', ctx);
  const h3 = vm.runInContext('renderEvents()', ctx);
  ok(!/class="ev lv1/.test(h3) && !/class="ev lv2/.test(h3),
    '「仅高」筛选下不出现中/低优先事件');
  vm.runInContext('state.evLevel = 1;', ctx);
  const h1 = vm.runInContext('renderEvents()', ctx);
  ok((h1.match(/class="ev lv/g) || []).length >= shown, '「全部」筛选下事件不少于默认筛选');
  // 类别筛选
  vm.runInContext('state.evLevel = 2; state.evCat = "us";', ctx);
  const hus = vm.runInContext('renderEvents()', ctx);
  ok(!/evcat cn/.test(hus) && !/evcat market/.test(hus), '「美国」筛选下不出现其它类别');
  vm.runInContext('state.evCat = "all";', ctx);

  // tab 徽章：未来 7 天的高优先事件数
  vm.runInContext('paintTabBadge()', ctx);
  const badge = vm.runInContext('upcomingHigh(7).length', ctx);
  ok(Number.isInteger(badge), 'tab 徽章统计可用（未来 7 天高优先事件）', `${badge} 条`);
}

/* ---------- 2c. 主力资金（flow.json，2026-09-22 新增） ---------- */
const flowFp = path.join(DATA, 'flow.json');
if (!fs.existsSync(flowFp)) {
  console.log('! 缺少 web/data/flow.json，跳过（先跑一次 pipeline）');
} else {
  const fl = JSON.parse(fs.readFileSync(flowFp, 'utf8'));
  ctx.__fl = fl;

  ok(!!(fl.market && fl.market.total && fl.market.sh && fl.market.sz),
    'flow.json 含 market（沪 / 深 / 合计）');
  const curveLen = fl.curve?.t?.length || 0;
  ok(Array.isArray(fl.curve?.t), 'flow.json 含分时时刻轴', `${curveLen} 点`);
  ok(['sh', 'sz', 'sum'].every((k) => Array.isArray(fl.curve?.[k])
    && fl.curve[k].length === curveLen), '分时三路长度与时刻轴一致');
  const badCurve = (fl.curve?.sum || []).find((v) => v != null && !Number.isFinite(v));
  ok(badCurve === undefined, '分时值均为有限数或 null（不出现 NaN）');
  ok(['sh', 'sz', 'sum'].every((k) => Array.isArray(fl.history?.[k])
    && fl.history[k].length === (fl.history?.dates || []).length),
    '历史三路长度与日期轴一致', `${fl.history?.days ?? 0} 天`);
  ok((fl.history?.dates || []).length <= 20, '历史最多 20 个交易日',
    `${(fl.history?.dates || []).length} 天`);
  const badBoard = (fl.boards || []).find((b) => !b.code || !b.name
    || !Number.isFinite(b.main_yi));
  ok(!badBoard, '每条板块榜记录都有 code/name/main_yi',
    badBoard ? JSON.stringify(badBoard).slice(0, 90) : '');
  const srt = (fl.boards || []).every((b, i, a) => i === 0 || a[i - 1].main_yi >= b.main_yi);
  ok(srt, '板块榜按主力净流入降序（前端直接取 TOP/BOTTOM，顺序错就全错）');

  const html = vm.runInContext('renderFlow(__fl, null)', ctx);
  ok(html.includes('主力资金'), '主力资金卡片已渲染');
  ok(/class="flow-big/.test(html), '大盘合计主力净流入已渲染');
  ok(curveLen < 2 || /class="fspark"/.test(html),
    '分时曲线已渲染（点数 < 2 时应缺省，而不是画一条假线）');
  ok(/class="fleg"/.test(html), '主力四档拆解已渲染');

  // 自校准断言：历史攒够 / 没攒够，页面文案必须与数据一致 ——
  // 趋势是「本系统逐日累积」的，头几天就是没有数据，不能拿空图冒充趋势。
  if ((fl.history?.days || 0) === 0) {
    ok(html.includes('还没有历史数据'), '历史为空时明说「还没有历史数据」而非画空图');
  } else {
    ok(!html.includes('还没有历史数据'), '有历史数据时不再显示「还没有历史数据」');
  }

  const rows = (html.match(/class="srow frow"/g) || []).length;
  const bs = fl.boards || [];
  const want = Math.min(10, bs.filter((b) => b.main_yi > 0).length)
             + Math.min(10, bs.filter((b) => b.main_yi < 0).length);
  ok(rows === want, '板块榜单行数与数据一致', `${rows} 行 / 预期 ${want}`);

  // 实时覆盖：伪造一份 live 榜单，必须出现「实时」标记并采用 live 的数值
  const live = [{ code: (bs[0] || {}).code || 'BK0001', name: '测试板块名',
                  main_yi: 12.34, main_pct: 5.67, pct: 1.23 }];
  const lh = vm.runInContext(`renderFlow(__fl, ${JSON.stringify(live)})`, ctx);
  ok(lh.includes('flive') && lh.includes('测试板块名') && lh.includes('+12.34'),
    '传入实时值时改用实时值渲染并标注「实时」');
}

/* ---------- 3. 回撤 / 涨幅列表 ---------- */
const idxFp = path.join(DATA, 'index.json');
if (!fs.existsSync(idxFp)) {
  console.log('! 缺少 web/data/index.json，跳过（先跑一次 pipeline）');
} else {
  const idx = JSON.parse(fs.readFileSync(idxFp, 'utf8'));
  ctx.__idx = idx;
  vm.runInContext('state.idx = __idx; state.win = 250; state.sort = "dd";', ctx);

  const withSwing = idx.filter((b) => b.swing && b.swing['250']);
  ok(withSwing.length === idx.length,
    `全部 ${idx.length} 个板块都带 swing 数据`, `实际 ${withSwing.length} 个`);

  for (const win of [20, 60, 250]) {
    const n = idx.filter((b) => b.swing && b.swing[String(win)]).length;
    ok(n === idx.length, `窗口 ${win} 日数据齐全`, `${n}/${idx.length}`);
  }

  const html = vm.runInContext('renderSwingList()', ctx);
  const deepest = [...idx].sort((a, b) => a.swing['250'].dd - b.swing['250'].dd)[0];
  ok(html.includes(deepest.name), '回撤列表渲染出回撤最深的板块', deepest.name);
  const cnt = (html.match(/class="srow"/g) || []).length;
  ok(cnt === idx.length, '回撤列表行数与板块数一致', `${cnt} 行 / ${idx.length} 个`);
  ok(html.includes('id="winSeg"'), '窗口切换控件已渲染');
  ok(html.includes('id="sortSeg"'), '排序切换控件已渲染');
  const bars = (html.match(/class="sbar"/g) || []).length;
  ok(bars === idx.length, '每行都有区间位置条', `${bars} 条`);

  // 切换窗口与排序键后仍应正常（回归：曾把窗口键写成数字而非字符串）
  let crossOk = true;
  for (const w of [20, 60, 250]) {
    for (const s of ['dd', 'rb', 'pos', 'score']) {
      vm.runInContext(`state.win = ${w}; state.sort = "${s}";`, ctx);
      const h = vm.runInContext('renderSwingList()', ctx);
      if (!h.includes('class="srow"') || h.includes('NaN')) crossOk = false;
    }
  }
  ok(crossOk, '窗口 × 排序 12 种组合均可渲染且无 NaN');
}

/* ---------- 4. 板块详情 ---------- */
{
  const secDir = path.join(DATA, 'sectors');
  if (fs.existsSync(secDir)) {
    const files = fs.readdirSync(secDir).filter((f) => f.endsWith('.json'));
    const sample = JSON.parse(fs.readFileSync(path.join(secDir, files[0]), 'utf8'));
    ok('swing' in sample, 'sector JSON 含 swing 字段', files[0]);
    ok('parent' in sample, 'sector JSON 含 parent（上级一级行业）');
    const w = sample.swing?.windows || {};
    ok(['20', '60', '250'].every((k) => k in w), 'sector swing 含 20/60/250 三个窗口');
    const one = w['250'] || {};
    ok(['high', 'low', 'high_date', 'low_date', 'drawdown', 'rebound',
        'position', 'days_since_high', 'days_since_low'].every((k) => k in one),
      'sector swing 含完整字段（含高低点日期与距今天数）');
    const html = vm.runInContext(`renderDetail(${JSON.stringify(sample)})`, ctx);
    ok(html.includes(sample.name), '详情页渲染出板块名');
    ok(html.includes('因子拆解'), '详情页含因子拆解区块');
  }
}

/* ---------- 5. 宏观条与数据新鲜度（时区回归） ----------
   这一段是补的：`renderMacroBar` 原先**没有任何断言**，于是
   「CI runner 是 UTC，把北京时间 15:43 写成 07:43，前端原样打印，
     手机上看起来像一整天没更新」这个 bug 一路静默上了线。

   断言方式刻意不硬编码「小时不能在 1–7 之间」（本地半夜跑流水线会误报），
   而是拿产物时间去比**当前北京时间**：若又变回 UTC，时间戳会落进未来约 8 小时，
   `ageH < 0` 会立刻变红 —— 自校准，且不受节假日长短影响。 */
const metaFp = path.join(DATA, 'meta.json');
if (!fs.existsSync(metaFp)) {
  console.log('! 缺少 web/data/meta.json，跳过（先跑一次 pipeline）');
} else {
  const meta = JSON.parse(fs.readFileSync(metaFp, 'utf8'));
  ctx.__meta = meta;

  ok(vm.runInContext('parseCN("2026-09-18 15:43:57")', ctx)
    === Date.UTC(2026, 8, 18, 7, 43, 57), 'parseCN 显式按 UTC+8 解析（不随手机时区漂移）');
  ok(vm.runInContext('Number.isNaN(parseCN(""))', ctx) === true, 'parseCN 对空串返回 NaN');

  const html = vm.runInContext('renderMacroBar(__meta)', ctx);
  ok(html.includes(String(meta.macro_score)), '宏观条渲染出宏观分', String(meta.macro_score));
  ok(html.includes(String(meta.macro_zone)), '宏观条渲染出区间', String(meta.macro_zone));
  ok(html.includes(meta.updated), '宏观条渲染出更新时间', meta.updated);
  ok(/class="ago" id="agoTs"/.test(html), '宏观条渲染出相对时间槽位');
  const ago = vm.runInContext('agoText(__meta.updated)', ctx);
  ok(typeof ago === 'string' && ago.length > 0, '相对时间文案可计算', ago);
  ok(html.includes(String(meta.updated).slice(0, 10)), '宏观条含产物日期');

  const ageH = (Date.now() - vm.runInContext('parseCN(__meta.updated)', ctx)) / 3600000;
  ok(ageH > -2, '产物时间是北京时间而非 UTC（写成 UTC 会落进未来约 8 小时）',
    `${ageH.toFixed(1)} 小时前`);
  ok(ageH < 24 * 30, '产物时间不是明显损坏/错年份的', `${(ageH / 24).toFixed(1)} 天前`);
}

console.log(failed ? `\n✗ ${failed} 项未通过` : '\n✓ 全部通过');
process.exit(failed ? 1 : 0);
