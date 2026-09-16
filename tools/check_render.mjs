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
  ok(/lv-res/.test(html) || /lv-none/.test(html), '压力位单元格已渲染（有档位或"上方无压力"）');
  ok(/lv-sup/.test(html) || /lv-none/.test(html), '支撑位单元格已渲染');
  ok((I.summary?.text || '').length === 0 || html.includes(I.summary.text.slice(0, 12)),
    '综合结论文案已渲染');
  ok(/lv-pos/.test(html), '区间位置行已渲染');
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

console.log(failed ? `\n✗ ${failed} 项未通过` : '\n✓ 全部通过');
process.exit(failed ? 1 : 0);
