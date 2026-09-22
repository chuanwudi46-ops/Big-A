#!/usr/bin/env python3
"""一次性探针：在**海外 runner**（也就是真正跑生产的机器）上实测主力资金流端点。

为什么必须在 runner 上测，而不是本机：
本机（国内 IP）实测东财各域名会**随时间波动**——同一端点两分钟前通、两分钟后就
RemoteDisconnected，还伴随着 `stock_board_industry_name_em` 一起断（疑似按 IP 短时限流）。
那种环境下得到的「不可用」结论对生产没有参考价值；生产 runner 是海外 IP，行为不同。

照 china-finance-data-from-overseas-runner 手册的硬性要求写：
- 每个端点单独 try/except，一个失败**绝不能**中断后续测试
- 打印 HTTP 状态码 + 响应长度 + 前 130 字符
- **空数组也算失败**（push2delay 的软限流是 200 + 空数据，最坑）
- 不用 `| head` 之类会让管道提前退出、被 SIGPIPE 打断的写法

只用 requests，不依赖仓库里的模块 —— 探针要能在最小依赖下跑起来。
"""
import json
import time

import requests

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
HEAD = {"User-Agent": UA, "Referer": "https://quote.eastmoney.com/"}

UT = "b2884a393a59ad64002292a3e90d46a5"
FF = "f12,f14,f2,f3,f62,f66,f69,f72,f75,f78,f81,f84,f87,f184"
KF1 = "f1,f2,f3,f7"
KF2 = "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65"

H = {
    "delay": "https://push2delay.eastmoney.com",
    "push2": "https://push2.eastmoney.com",
    "his": "https://push2his.eastmoney.com",
    "82": "https://82.push2.eastmoney.com",
}


def probe(label, url, parse=None, tries=2):
    """单端点探测：失败不抛，只报告。"""
    for i in range(tries):
        try:
            r = requests.get(url, headers=HEAD, timeout=20)
            body = r.text or ""
            good = r.status_code == 200 and len(body) > 120
            extra = ""
            if good and parse:
                try:
                    extra = parse(json.loads(body))
                except Exception as e:              # noqa: BLE001
                    extra = f"解析失败:{str(e)[:50]}"
            print(f"  [{'OK ' if good else '?? '}] {label:38s} "
                  f"HTTP {r.status_code} len={len(body):7d} {extra}")
            print(f"        {body[:130]}")
            if good:
                return body
        except Exception as e:                     # noqa: BLE001
            print(f"  [ERR] {label:38s} try{i + 1} {str(e)[:95]}")
        time.sleep(1.2)
    return None


def klines_head_tail(n=3):
    def f(j):
        k = (j.get("data") or {}).get("klines") or []
        if not k:
            return "**klines 为空（软限流！）**"
        return f"klines={len(k)} 首={k[0][:58]} 末={k[-1][:58]}"
    return f


def clist_count(j):
    d = j.get("data") or {}
    diff = d.get("diff") or []
    if not diff:
        return "**diff 为空**"
    s = diff[0]
    return (f"total={d.get('total')} 首条={s.get('f14')} "
            f"f62={s.get('f62')} f184={s.get('f184')} f66={s.get('f66')}")


print("=" * 84)
print("## 0. runner 出口 IP / 地区")
try:
    r = requests.get("https://ipinfo.io/json", timeout=15)
    j = r.json()
    print("  ", {k: j.get(k) for k in ("ip", "city", "region", "country", "org")})
except Exception as e:                             # noqa: BLE001
    print("   IP 查询失败", str(e)[:90])

print("=" * 84)
print("## 1. clist 板块（资金流字段）—— 板块主力净流入排行")
for tag in ("delay", "push2", "82"):
    probe(f"clist 板块 m:90+t:2 @{tag}",
          f"{H[tag]}/api/qt/clist/get?pn=1&pz=100&po=0&np=1&fltt=2&invt=2"
          f"&fid=f12&fs=m:90+t:2&fields={FF}", clist_count)

print("=" * 84)
print("## 2. clist 个股资金流排行（按 f62 降序）")
for tag in ("delay", "push2"):
    probe(f"clist 个股 @{tag}",
          f"{H[tag]}/api/qt/clist/get?pn=1&pz=20&po=1&np=1&fltt=2&invt=2"
          f"&fid=f62&fs=m:0+t:6,m:0+t:13,m:0+t:80,m:1+t:2,m:1+t:23&fields={FF}",
          clist_count)

print("=" * 84)
print("## 3. fflow/kline 大盘分时（klt=1）—— 今日累计主力净流入")
for tag in ("delay", "push2"):
    for sec, nm in (("1.000001", "上证/沪市"), ("0.399001", "深证/深市")):
        probe(f"fflow kline {nm} @{tag}",
              f"{H[tag]}/api/qt/stock/fflow/kline/get?lmt=0&klt=1&secid={sec}"
              f"&fields1={KF1}&fields2={KF2}", klines_head_tail())

print("=" * 84)
print("## 4. fflow/daykline 日线历史（带 ut）—— 近 N 日主力净流入")
for tag in ("his", "delay", "push2"):
    probe(f"fflow daykline 沪市 @{tag}",
          f"{H[tag]}/api/qt/stock/fflow/daykline/get?lmt=0&klt=101&secid=1.000001"
          f"&fields1={KF1}&fields2={KF2}&ut={UT}", klines_head_tail())
probe("fflow daykline 深市 @his",
      f"{H['his']}/api/qt/stock/fflow/daykline/get?lmt=0&klt=101&secid=0.399001"
      f"&fields1={KF1}&fields2={KF2}&ut={UT}", klines_head_tail())

print("=" * 84)
print("## 5. JSONP 回调（cb=）—— 前端实时补丁的前提")
probe("fflow kline +cb @delay",
      f"{H['delay']}/api/qt/stock/fflow/kline/get?lmt=0&klt=1&secid=1.000001"
      f"&fields1={KF1}&fields2={KF2}&cb=wbcb1")
probe("fflow daykline +cb +ut @his",
      f"{H['his']}/api/qt/stock/fflow/daykline/get?lmt=0&klt=101&secid=1.000001"
      f"&fields1={KF1}&fields2={KF2}&ut={UT}&cb=wbcb2")

print("=" * 84)
print("## 6. 板块分时资金流（90.BK0475）—— 板块能不能也做实时")
for tag in ("delay", "push2"):
    probe(f"板块 fflow kline @{tag}",
          f"{H[tag]}/api/qt/stock/fflow/kline/get?lmt=0&klt=1&secid=90.BK0475"
          f"&fields1={KF1}&fields2={KF2}", klines_head_tail())

print("=" * 84)
print("## 7. 腾讯（备选源）——确认基准通道仍然健在")
probe("腾讯 上证日线",
      "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get"
      "?param=sh000001,day,2026-08-01,2026-09-22,60,qfq")

print("=" * 84)
print("## DONE")
