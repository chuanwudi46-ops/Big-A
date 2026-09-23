/* 前端实时补丁回归自测（无需浏览器）
 *
 * 为什么需要它：`web/app.js` 的取数逻辑跑在浏览器里，CI 和本机都测不到。
 * 但这里有一个**静默失效**级别的坑 —— 东财 clist 单页硬截断到 100 条，
 * 而 `fs=m:90+t:2` 默认按涨幅降序，一旦不翻页，关注的板块就会大面积
 * 拿不到实时数据、页面却毫无报错。所以每次动 live 相关代码都该跑一遍。
 *
 * 做法：把 app.js 里 live 相关的那段源码原样抽出来丢进 node vm，
 * 用 fetch 冒充 JSONP，再用**线上真实发布的板块清单**核对命中率。
 *
 * 用法：node tools/check_live_paging.mjs
 * 退出码 0 = 通过，1 = 不通过。
 */
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const SITE = 'https://chuanwudi46-ops.github.io/Big-A';
const FALLBACK_HOST = 'push2delay.eastmoney.com';
const PAGE = 100;

/* ---------- 1. 从源码里切出待测片段（保持与线上同一份实现） ---------- */
const src = fs.readFileSync(path.join(ROOT, 'web/app.js'), 'utf8');
const a = src.indexOf('const LIVE_HOSTS');
const b = src.indexOf('async function liveSnapshot');
if (a < 0 || b < 0) {
  console.error('✗ 未能从 web/app.js 定位到 LIVE_HOSTS / liveAll 片段，脚本需要同步更新');
  process.exit(1);
}
const snippet = src.slice(a, b);

/* ---------- 2. 用 fetch 冒充浏览器 JSONP ---------- */
const ctx = {
  console, JSON, Math, Array, Promise, Object, String, Number, Error,
  setTimeout, clearTimeout,
  jsonp: async (url) => {
    const u = new URL(url);
    u.searchParams.delete('cb');
    const r = await fetch(u, {
      headers: { 'User-Agent': 'Mozilla/5.0' },
      signal: AbortSignal.timeout(20000),
    });
    return await r.json();
  },
};
vm.createContext(ctx);
vm.runInContext(snippet, ctx);

/* ---------- 3. 逐项断言 ---------- */
let failed = 0;
const ok = (cond, label, extra = '') => {
  console.log(`${cond ? '✓' : '✗'} ${label}${extra ? '  ' + extra : ''}`);
  if (!cond) failed++;
};

// 3.1 URL 构造：单页大小必须是服务端硬上限 100，域名顺序实时优先
const url1 = vm.runInContext(`liveURL(LIVE_HOSTS[0], 1)`, ctx);
ok(/push2\.eastmoney\.com/.test(url1), '实时域名排在第一（push2 优先，delay 只兜底）', '');
ok(new RegExp(`pz=${PAGE}\\b`).test(url1), `单页 pz=${PAGE}（服务端硬上限，写大无效）`);
const hosts = vm.runInContext(`JSON.stringify(LIVE_HOSTS)`, ctx);
ok(JSON.parse(hosts)[0] === 'push2.eastmoney.com', '域名顺序未写反', hosts);

// 3.2 翻页取全量
const rows = await vm.runInContext(
  `(async () => await liveAll('${FALLBACK_HOST}'))()`, ctx);
ok(Array.isArray(rows) && rows.length > PAGE,
  `翻页取全量（旧实现只会拿到 ${PAGE} 条）`, `实际 ${rows.length} 条`);
const uniq = new Set(rows.map((r) => String(r.f12)));
// 排序键必须静态（fid=f12）。用 f3 涨幅排序时盘中翻页会串位：实测重复 1 条、漏 1 个板块
ok(uniq.size === rows.length,
  '翻页零重复（排序键用的是静态的 f12 代码，非 f3 涨幅）',
  `唯一 ${uniq.size} / 共 ${rows.length}`);

// 3.3 用线上真实板块清单核对命中率
let index = [];
try {
  const r = await fetch(`${SITE}/data/index.json`, { signal: AbortSignal.timeout(20000) });
  index = await r.json();
} catch {
  console.log('! 线上 index.json 拉取失败，跳过命中率核对（离线时属正常）');
}
if (index.length) {
  const want = new Set(index.map((s) => s.code));
  const hit = [...want].filter((c) => uniq.has(c));
  const head = new Set(rows.slice(0, PAGE).map((r) => String(r.f12)));
  const oldHit = [...want].filter((c) => head.has(c));
  ok(hit.length === want.size,
    `线上 ${want.size} 个板块实时补丁命中率 100%`,
    `命中 ${hit.length}/${want.size}；不翻页则只有 ${oldHit.length}/${want.size}`);
}

// 3.4 「主力资金」板块排行所依赖的字段必须真的回得来
// 静默失效风险：东财对不认识的字段**不报错，只是不返回**。一旦 f62 没了，
// liveFlowBoards() 会被 `Number.isFinite(r.mainInflow)` 全部过滤掉、返回 null，
// 然后**不动声色**地退回后端那份收盘旧值 —— 页面看着完全正常，实时能力已经死了。
{
  const need = ['f62', 'f184'];
  const miss = need.filter((k) => !(k in (rows[0] || {})));
  ok(miss.length === 0, 'clist 返回板块排行所需字段（f62 主力净流入 / f184 净占比）',
    miss.length ? `缺 ${miss.join(', ')}` : `${need.length} 个都在`);

  const cnt = (k) => rows.filter((r) => Number.isFinite(r[k])).length;
  const n62 = cnt('f62');
  ok(n62 > 0, 'f62 有可用数值（全为 null 会让实时板块排行整体退化成后端旧值）',
    `${n62}/${rows.length} 条`);
  if (n62 > 0) {
    ok(cnt('f184') / n62 >= 0.9,
      'f184 覆盖率 ≥90%（与 f62 同源，掉了说明请求被削字段）',
      `${cnt('f184')}/${n62}`);
  }
}

console.log(failed ? `\n✗ ${failed} 项未通过` : '\n✓ 全部通过');
process.exit(failed ? 1 : 0);
