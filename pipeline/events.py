#!/usr/bin/env python3
"""事件日历：把「未来可能引起较大波动的事件」整理成前端可用的一张表。

数据来源（三条腿，任何一条断了都不影响其余）
--------------------------------------------------------------------------
1. **东财财经日历**（真实事件表，主力）
   报表 `RPT_CPH_FECALENDAR`（报表名是从 data.eastmoney.com/cjrl 的前端 JS 里挖出来的，
   不是猜的）。实测覆盖：美国 CPI / 非农 / 核心 PCE / ISM / GDP、中国 LPR / PMI / GDP、
   各国央行议息会议、政策与行业会议、新股申购、MLF 到期等，
   取 4 个月区间约 1500 条、耗时约 1.3 s。
2. **规则推算**：股指期货/期权交割日（每月第 3 个周五）。
3. **本仓库既有数据**：解禁市值（`warmup` 产出的 release.parquet）—— 与筹码供给罚分同源。
   另有一份 `curated` 人工清单兜住「年度固定政策会议」这类日历源可能没有的事件。

为什么要做「归类合并」
--------------------------------------------------------------------------
原始日历里同一件事会有多个口径（美国非农就有 4 条、CPI 有环比/同比/核心……），
直接铺出来会被噪音淹没。故用 `config/events_rules.json` 里的规则把原始条目
映射到「规范事件」，再按 (日期, 规则id) 合并成一条，并保留原始明细供展开查看。

数据真实性
--------------------------------------------------------------------------
每条事件都带 `src` 字段标明出处：`eastmoney`（日历源）/ `rule`（规则推算）/
`release`（本仓库解禁数据）/ `curated`（人工惯例清单，前端会标注「惯例推算」）。
**不把推断出来的日期伪装成官方公布日期**。

产物：web/data/events.json
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib

import pandas as pd

ROOT = pathlib.Path(__file__).resolve().parents[1]
CFG = pathlib.Path(__file__).resolve().parent / "config" / "events_rules.json"
DATA = ROOT / "web" / "data"

WEEKDAY_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


# --------------------------------------------------------------------- 规则
def load_rules(fp: pathlib.Path | None = None) -> dict:
    return json.loads((fp or CFG).read_text(encoding="utf-8"))


def _hit(hay: str, rule: dict) -> bool:
    """规则匹配：match 全中 + match_any/match_any2 各至少中一个 + exclude 全不中"""
    for s in rule.get("match") or []:
        if s not in hay:
            return False
    for key in ("match_any", "match_any2"):
        pats = rule.get(key) or []
        if pats and not any(p in hay for p in pats):
            return False
    for s in rule.get("exclude") or []:
        if s in hay:
            return False
    return True


def classify_one(name: str, typ: str, rules: list[dict]) -> dict | None:
    """返回命中的第一条规则（规则表按顺序排列，兜底规则在最后）"""
    hay = f"{name} {typ}"
    for r in rules:
        if _hit(hay, r):
            return r
    return None


def _clean_detail(nm: str) -> str:
    """把「美国:非农就业人数:季调(报告期:2026年09月)」压成好读的一行"""
    txt = nm.replace("(报告期:", "｜").replace(")", "").replace("（报告期:", "｜")
    return txt[:70]


def _time_of(ts: pd.Timestamp) -> str | None:
    if ts.hour == 0 and ts.minute == 0:
        return None                     # 只有日期没有时刻的事件（会议、申购）
    return ts.strftime("%H:%M")


def build_events(raw: pd.DataFrame, cfg: dict, today: dt.date,
                 release: pd.DataFrame | None = None) -> list[dict]:
    """把原始日历表转成规范事件列表（已按规则合并）"""
    rules = cfg.get("rules") or []
    look = int((cfg.get("window") or {}).get("lookback_days", 7))
    hori = int((cfg.get("window") or {}).get("horizon_days", 120))
    lo = today - dt.timedelta(days=look)
    hi = today + dt.timedelta(days=hori)

    buckets: dict[tuple, dict] = {}
    dropped = 0
    for row in raw.itertuples(index=False):
        ts = pd.Timestamp(row.date)
        d = ts.date()
        if d < lo or d > hi:
            continue
        rule = classify_one(str(row.name), str(row.type), rules)
        if rule is None:
            dropped += 1                # 未命中任何规则的事件直接丢弃（保持产物精简）
            continue
        key = (d, rule["id"])
        b = buckets.get(key)
        if b is None:
            b = buckets[key] = {"date": d, "rule": rule, "times": [], "detail": []}
        b["times"].append(ts)
        if len(b["detail"]) < 6:
            b["detail"].append(_clean_detail(str(row.name)))

    # 允许同一规则内的「同日/相邻日」事件合并（如美联储议息会议与其新闻发布会
    # 相差一天，实际是同一次会议；合并后取**较晚**那一天 —— 那才是决议公布、
    # 市场真正反应的时刻）。
    window = {}
    for r in rules:
        wd = int(r.get("merge_window_days") or 0)
        if wd > 0:
            window[r["id"]] = wd
    merged: list[dict] = []
    for rid in {k[1] for k in buckets}:
        items = sorted((b for k, b in buckets.items() if k[1] == rid),
                       key=lambda b: b["date"])
        if rid not in window:
            merged.extend(items)
            continue
        wd = window[rid]
        cur: list[dict] = []
        for b in items:
            if cur and (b["date"] - cur[-1]["date"]).days <= wd:
                cur.append(b)
            else:
                if cur:
                    merged.append(_fuse(cur))
                cur = [b]
        if cur:
            merged.append(_fuse(cur))

    out: list[dict] = []
    for b in merged:
        r = b["rule"]
        times = sorted(b["times"])
        last_day = max(t.date() for t in times)
        same_day = [t for t in times if t.date() == last_day]
        d = last_day
        out.append({
            "date": d.isoformat(),
            "weekday": WEEKDAY_CN[d.weekday()],
            "time": _time_of(min(same_day)),
            "name": r["name"],
            "cat": r.get("cat", "global"),
            "level": int(r.get("level", 1)),
            "why": r.get("why", ""),
            "src": "eastmoney",
            "n": len(b["detail"]),
            "detail": b["detail"],
        })

    out.extend(gen_market(cfg, today, hi, release))
    out.extend(gen_curated(cfg, today, hi, out))
    return out


def _fuse(group: list[dict]) -> dict:
    """把同一次会议的相邻日条目合成一条（时间取决议公布那天的较早时刻）"""
    return {"date": max(b["date"] for b in group),
            "rule": group[0]["rule"],
            "times": [t for b in group for t in b["times"]],
            "detail": [x for b in group for x in b["detail"]][:6]}


# --------------------------------------------------------------- 规则推算事件
def third_friday(year: int, month: int) -> dt.date:
    """每月第 3 个周五 = 中金所股指期货/期权交割日"""
    d = dt.date(year, month, 1)
    fridays = [d + dt.timedelta(days=i) for i in range(31)
               if (d + dt.timedelta(days=i)).month == month
               and (d + dt.timedelta(days=i)).weekday() == 4]
    return fridays[2]


def gen_market(cfg: dict, today: dt.date, hi: dt.date,
               release: pd.DataFrame | None) -> list[dict]:
    """A 股制度性事件：股指交割日（规则）+ 解禁高峰（本仓库数据）"""
    out: list[dict] = []
    d = today.replace(day=1)
    while d <= hi:
        f = third_friday(d.year, d.month)
        if today <= f <= hi:
            out.append({
                "date": f.isoformat(), "weekday": WEEKDAY_CN[f.weekday()],
                "time": None, "name": "股指期货 / 期权交割日",
                "cat": "market", "level": 2, "src": "rule", "n": 1,
                "detail": ["每月第 3 个周五为中金所股指期货与期权结算日"],
                "why": "交割周尾盘常出现结算价博弈，波动放大；贴水收敛也会影响期现套利",
            })
        d = (d + dt.timedelta(days=32)).replace(day=1)

    thr = float((cfg.get("generated") or {}).get("release_mv_yi", 150))
    if release is None or release.empty:
        return out
    col_d = next((c for c in ("解禁时间", "解禁日期", "date") if c in release.columns), None)
    col_v = next((c for c in ("实际解禁市值", "解禁市值", "value") if c in release.columns), None)
    col_n = next((c for c in ("股票简称", "名称", "name") if c in release.columns), None)
    if not col_d or not col_v:
        return out
    rel = release.copy()
    rel[col_d] = pd.to_datetime(rel[col_d], errors="coerce")
    rel[col_v] = pd.to_numeric(rel[col_v], errors="coerce").fillna(0.0)
    for day, g in rel.dropna(subset=[col_d]).groupby(rel[col_d].dt.date):
        if day < today or day > hi:
            continue
        yi = float(g[col_v].sum()) / 1e8
        if yi < thr:
            continue
        top = g.nlargest(5, col_v)
        detail = [f"{r[col_n]} {r[col_v] / 1e8:.1f} 亿" for _, r in top.iterrows()] \
            if col_n else []
        out.append({
            "date": day.isoformat(), "weekday": WEEKDAY_CN[day.weekday()],
            "time": None, "name": f"解禁市值 {yi:.0f} 亿（{len(g)} 只）",
            "cat": "market", "level": 3 if yi >= thr * 3 else 2,
            "src": "release", "n": len(g), "detail": detail,
            "why": "筹码供给冲击：与「筹码供给罚分」维度同源，持仓板块当日承压概率上升",
        })
    return out


def gen_curated(cfg: dict, today: dt.date, hi: dt.date, existing: list[dict]) -> list[dict]:
    """人工维护的年度政策会议（惯例窗口）。已被日历源覆盖的窗口不再重复添加。"""
    out: list[dict] = []
    for e in (cfg.get("curated") or {}).get("events") or []:
        for year in {today.year, hi.year}:
            for m in e.get("months") or []:
                lo = dt.date(year, m, int(e.get("day_lo", 1)))
                last = (dt.date(year + (m == 12), (m % 12) + 1, 1) - dt.timedelta(days=1)).day
                hi_day = min(int(e.get("day_hi", lo.day)), last)
                end = dt.date(year, m, hi_day)
                if end < today or lo > hi:
                    continue
                # 日历源已经有同名/同类事件 → 不再重复添加。
                # 只能按**关键词**比对，不能按「窗口内有没有别的 cn 事件」——
                # 实测那样会把 12 月中旬的「中国月度经济数据」误判成已覆盖，
                # 从而把中央经济工作会议整条吞掉。
                keys = e.get("dedupe_by") or [e["name"].split("（")[0]]
                if any(k in x["name"] or any(k in d for d in (x.get("detail") or []))
                       for x in existing for k in keys):
                    continue
                out.append({
                    "date": max(lo, today).isoformat(),
                    "weekday": WEEKDAY_CN[max(lo, today).weekday()],
                    "time": None, "name": e["name"], "cat": e.get("cat", "cn"),
                    "level": int(e.get("level", 2)), "src": "curated", "n": 1,
                    "detail": [f"惯例窗口 {lo:%m-%d} ~ {end:%m-%d}，具体日期以官方公告为准"],
                    "why": e.get("why", ""),
                    "certainty": e.get("certainty", "惯例推算"),
                    "date_end": end.isoformat(),
                })
    return out


# ----------------------------------------------------------------------- 入口
def load_calendar(cache: pathlib.Path | None = None) -> pd.DataFrame:
    """优先读 warmup 落盘的 parquet；缺失时回源（保证单跑 score 也能出日历）"""
    fp = (cache or DATA) / "events_raw.parquet"
    if fp.exists():
        try:
            df = pd.read_parquet(fp)
            if not df.empty:
                return df
        except Exception as e:  # noqa: BLE001
            print(f"[warn] events_raw.parquet 读取失败：{e}")
    import sources  # 同目录模块
    today = dt.date.today()
    return sources.econ_calendar(
        (today - dt.timedelta(days=10)).strftime("%Y-%m-%d"),
        (today + dt.timedelta(days=180)).strftime("%Y-%m-%d"))


def build(raw: pd.DataFrame, cfg: dict, today: dt.date,
          release: pd.DataFrame | None = None) -> dict:
    ev = build_events(raw, cfg, today, release)
    maxn = int((cfg.get("window") or {}).get("max_events", 220))
    # 低优先级事件只在总量超标时被裁掉，且优先裁最远的未来 ——
    # 高优先级（level>=2）一律保留，日历的价值就在这几条上
    ev.sort(key=lambda e: (e["date"], -e["level"], e.get("time") or ""))
    high = [e for e in ev if e["level"] >= 2]
    low = [e for e in ev if e["level"] < 2]
    if len(high) + len(low) > maxn:
        low = low[:max(0, maxn - len(high))]
    ev = sorted(high + low, key=lambda e: (e["date"], -e["level"], e.get("time") or ""))

    today_s = today.isoformat()
    upcoming = [e for e in ev if e["date"] >= today_s and e["level"] >= 3]
    next_high = upcoming[0] if upcoming else None
    by_cat: dict[str, int] = {}
    for e in ev:
        by_cat[e["cat"]] = by_cat.get(e["cat"], 0) + 1
    for e in ev:
        e["past"] = e["date"] < today_s
        if "level_label" not in e:
            e["level_label"] = (cfg.get("levels") or {}).get(str(e["level"]), "")

    return {
        "updated": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "as_of": today_s,
        "lookback_days": int((cfg.get("window") or {}).get("lookback_days", 7)),
        "horizon_days": int((cfg.get("window") or {}).get("horizon_days", 120)),
        "count": len(ev),
        "counts": {"high": len([e for e in ev if e["level"] >= 3]),
                   "mid": len([e for e in ev if e["level"] == 2]),
                   "low": len([e for e in ev if e["level"] == 1]),
                   "by_cat": by_cat},
        "next_high": next_high,
        "cats": cfg.get("cats") or {},
        "levels": cfg.get("levels") or {},
        "sources": [
            "东方财富财经日历 RPT_CPH_FECALENDAR（真实事件表）",
            "规则推算：每月第 3 个周五 = 股指期货/期权交割日",
            "解禁市值：warmup 产出的 release.parquet",
            "人工惯例清单：年度政策会议（已标注「惯例推算」）",
        ],
        "events": ev,
    }


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    import sources  # noqa: E402

    _cfg = load_rules()
    _today = dt.date.today()
    _raw = sources.econ_calendar((_today - dt.timedelta(days=10)).strftime("%Y-%m-%d"),
                                 (_today + dt.timedelta(days=180)).strftime("%Y-%m-%d"))
    _rel = pd.DataFrame()
    try:
        _rel = pd.read_parquet(DATA / "release.parquet")
    except Exception:  # noqa: BLE001
        pass
    rep = build(_raw, _cfg, _today, _rel)
    print(json.dumps({k: v for k, v in rep.items() if k != "events"},
                     ensure_ascii=False, indent=1))
    for e in rep["events"]:
        mark = {"3": "★★★", "2": "★★", "1": "★"}[str(e["level"])]
        print(f"{e['date']} {e['weekday']} {e.get('time') or '--:--'} {mark} "
              f"[{e['cat']}/{e['src']}] {e['name'][:34]} ({e['n']})")
