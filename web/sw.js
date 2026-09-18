/* Service Worker
   策略：
   - data/ 下的 JSON 走「网络优先」，保证评分是当天最新的；失败回退缓存（离线可看）
   - 其余静态资源走「缓存优先 + 后台重验证」（stale-while-revalidate）：先秒开，
     后台带条件请求刷新，下一次加载即可拿到新版
   - 版本号变更时清理旧缓存，避免修复不生效

   为什么静态资源不能用「纯缓存优先」：app.js 一旦入缓存就永不更新，而
   浏览器只在 sw.js 字节变化时才重装 SW —— 改 UI 却不改 sw.js 时，
   已装过 PWA 的用户会一直看到旧界面且毫无报错。发布 UI 改动时**务必同步
   改 VERSION**（sw.js 字节随之变化，触发 SW 重装 + 清旧缓存）。
   当前 VERSION 对应：申万二级 120 板块 + 大盘关键位（日/周/月/年）+ 回撤/涨幅 + 事件日历。
*/
const VERSION = 'wb-v4';
const STATIC_ASSETS = [
  './',
  './index.html',
  './app.js',
  './styles.css',
  './manifest.json',
  './icon.svg',
];

self.addEventListener('install', (e) => {
  e.waitUntil(
    caches.open(VERSION)
      .then((c) => c.addAll(STATIC_ASSETS))
      .then(() => self.skipWaiting())
      .catch(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== VERSION).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (e) => {
  const req = e.request;
  if (req.method !== 'GET') return;

  let url;
  try { url = new URL(req.url); } catch { return; }

  // 只接管同源请求（东财 JSONP 走网络，不缓存）
  if (url.origin !== self.location.origin) return;

  if (url.pathname.includes('/data/')) {
    e.respondWith(
      fetch(req)
        .then((res) => {
          const copy = res.clone();
          caches.open(VERSION).then((c) => c.put(req, copy)).catch(() => {});
          return res;
        })
        .catch(() => caches.match(req))
    );
    return;
  }

  // 静态资源：stale-while-revalidate。先用缓存立即响应（秒开），
  // 同时在后台带 no-cache 条件请求（GitHub Pages 有 ETag，未变更时仅 304），
  // 命中 200 才写回缓存 —— 保证下一次加载自动拿到新版，无需再改 VERSION。
  e.respondWith(
    caches.match(req).then((hit) => {
      const refresh = fetch(new Request(req, { cache: 'no-cache' }))
        .then((res) => {
          if (res && res.status === 200) {
            const copy = res.clone();
            caches.open(VERSION).then((c) => c.put(req, copy)).catch(() => {});
          }
          return res;
        })
        .catch(() => null);
      if (hit) return hit;                       // 有缓存：立刻返回，刷新在后台跑
      return refresh.then((res) => res || new Response('', { status: 504 }));
    })
  );
});
