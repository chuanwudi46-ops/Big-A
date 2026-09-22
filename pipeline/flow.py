#!/usr/bin/env python3
"""主力资金流：大盘（沪深两市）与板块。

产物 `web/data/flow.json`，供前端「主力资金」视图渲染。

为什么单开一份产物，而不是塞进 meta.json / index.json
--------------------------------------------------------------------------
它是**盘中变化最快**的一块（每分钟都在变），而评分 30 分钟才一版。分开写，
前端才能对资金流派独立的实时补丁，不必等整版产物刷新。体积上也属于中等：
分时曲线 240 点 × 4 条，塞进 index.json 会让每个用户的首屏多下几十 KB。

口径说明（重要，别想当然）
--------------------------------------------------------------------------
- 「大盘」= 沪市（上证指数口径 1.000001）+ 深市（深证成指口径 0.399001），
  与东财「大盘资金流」页面的算法一致。
- 主力 = 大单 + 超大单。接口直接给「主力」这一列，不需要自己加
  （实测恒等式 `大单 + 超大单 == 主力` 逐行成立，也顺带验证了字段顺序没搞错）。
- 单位一律换算成**亿元**（字段后缀 `_yi`），与项目里 `release_mv_yi` 的约定一致。
  原始接口给的是元（8~11 位数字），直接下发既占体积又容易看错位数。
- `curve` 里的值是**自开盘累计**到该时刻的净流入（不是每分钟增量），
  所以最后一点 = 当日累计值；画出来天然是一条累计曲线。

历史趋势为什么是「自建」的（2026-09-22 探针实测，别改回去）
--------------------------------------------------------------------------
东财给历史资金流的接口是 `fflow/daykline`，但**海外 runner 上拿不到**：
`push2his` 直接连不上（RemoteDisconnected），`push2delay` / `push2` 虽然 200，
却只回**当天 1 条**（即使带了 `ut` 参数）。也就是说生产环境根本没有历史来源。

所以这里不依赖它：拿**分时数据的当日累计值**当当日收盘值，逐日累积到
`web/data/flow_hist.parquet`。同一个交易日里被覆盖多次（盘中 12 次 score），
最后落定的那次就是收盘值 —— `drop_duplicates(keep="last")` 天然实现这一点。
代价是**头 20 天趋势是空的**（攒一天多一格），这点必须如实告诉使用者，
不能拿一条只有两三个点的曲线假装是「近 20 日趋势」。
"""
from __future__ import annotations

import pandas as pd

import clock
import sources

# 趋势展示回看多少个交易日。20 日 ≈ 一个月，足够看出「持续流入 / 持续流出」，
# 又不至于把产物撑大（4 条序列 × 20 点）。不足 20 天时前端要标出实际天数。
HISTORY_DAYS = 20

_KEEP_COLS = ("date", "main", "small", "medium", "large", "xlarge",
              "main_pct", "close", "chg_pct")


def _yi(v) -> float | None:
    """元 -> 亿元（保留两位）。None / NaN 原样返回 None。"""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if pd.isna(f):
        return None
    return round(f / 1e8, 2)


def _num(v, digits: int = 2) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if pd.isna(f) else round(f, digits)


def _norm_cache(cache: pd.DataFrame | None) -> pd.DataFrame:
    if cache is None or cache.empty or "date" not in cache.columns:
        return pd.DataFrame(columns=["secid"] + list(_KEEP_COLS))
    c = cache.copy()
    c["date"] = c["date"].astype(str).str.slice(0, 10)
    if "secid" not in c.columns:
        return pd.DataFrame(columns=["secid"] + list(_KEEP_COLS))
    for col in _KEEP_COLS:
        if col not in c.columns:
            c[col] = None
    return c[["secid"] + list(_KEEP_COLS)]


def refresh_daily(cache: pd.DataFrame | None = None) -> pd.DataFrame:
    """**尽力而为**地补历史日线（只在 warmup 调一次）。

    海外 runner 上这通常是白跑（见模块 docstring），但成本只有几个快速失败的
    连接，而一旦某个域名哪天放开了，就能一次补回几十天历史 —— 值得每天试一次。
    单边失败不影响另一边。
    """
    frames = [_norm_cache(cache)]
    for _key, secid, name in sources.MARKET_SECIDS:
        try:
            rows = sources.fund_flow_daily(secid, attempts=1)
        except Exception as e:  # noqa: BLE001
            print(f"[info] 资金流历史不可用（{name}）：{str(e)[:90]}")
            continue
        df = _rows_to_frame(rows, secid)
        if not df.empty:
            frames.append(df)
            print(f"[warmup] 资金流历史 {name}: {len(df)} 条")
    frames = [f for f in frames if not f.empty]
    if not frames:
        return pd.DataFrame(columns=["secid"] + list(_KEEP_COLS))
    out = pd.concat(frames, ignore_index=True)
    return (out.drop_duplicates(subset=["secid", "date"], keep="last")
               .sort_values(["date", "secid"]).reset_index(drop=True))


def _rows_to_frame(rows: list[dict], secid: str) -> pd.DataFrame:
    """把 sources 解析出的资金流行转成 DataFrame（带 secid 列）。

    分时行的时间戳带时分（同日多条），日线只有日期 —— 这里统一**截到「日」**。
    日线去重才不会把当天的 240 个分时点当成 240 天（那会把整段趋势挤成一天）。
    """
    if not rows:
        return pd.DataFrame(columns=["secid"] + list(_KEEP_COLS))
    df = pd.DataFrame(rows)
    if "date" not in df.columns:
        return pd.DataFrame(columns=["secid"] + list(_KEEP_COLS))
    df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.strftime("%Y-%m-%d")
    df = df.dropna(subset=["date"])
    df["secid"] = secid
    for c in _KEEP_COLS:
        if c not in df.columns:
            df[c] = None
    return df[["secid"] + list(_KEEP_COLS)].copy()


def _curve(series: dict[str, dict[str, float]]) -> tuple[list[str], dict[str, list]]:
    """把两市的「时刻 -> 累计主力净流入」拼成对齐的曲线。

    两市理论上都是 240 个点、时间戳一致；但接口偶发少点，所以按**时刻字典**
    对齐而不是按下标 —— 按下标对齐一旦错位，整条曲线就整体歪了。
    """
    tl = list(series.get("sh") or series.get("sz") or {})
    curve: dict[str, list] = {}
    for k in ("sh", "sz"):
        m = series.get(k) or {}
        curve[k] = [_yi(m[t]) if t in m else None for t in tl]
    tot: list = []
    for t in tl:
        vals = [series[k][t] for k in ("sh", "sz")
                if t in (series.get(k) or {}) and series[k][t] is not None]
        tot.append(_yi(sum(vals)) if vals else None)
    curve["sum"] = tot
    return tl, curve


def _market_block(series_last: dict[str, dict | None]) -> dict:
    """两市 + 合计的当日快照。缺失的一边保留 None，**不猜**。"""
    out: dict = {}
    for key, _secid, name in sources.MARKET_SECIDS:
        last = series_last.get(key)
        if not last:
            out[key] = {"name": name, "main_yi": None}
            continue
        out[key] = {
            "name": name,
            "main_yi": _yi(last.get("main")),
            "xlarge_yi": _yi(last.get("xlarge")),
            "large_yi": _yi(last.get("large")),
            "medium_yi": _yi(last.get("medium")),
            "small_yi": _yi(last.get("small")),
            "main_pct": _num(last.get("main_pct")),
            "point": str(last.get("date", ""))[-5:],
        }
    tot = {"name": "两市合计"}
    for f in ("main", "xlarge", "large", "medium", "small"):
        vals = [out[k].get(f + "_yi") for k, _s, _n in sources.MARKET_SECIDS]
        vals = [v for v in vals if v is not None]
        tot[f + "_yi"] = round(sum(vals), 2) if vals else None
    out["total"] = tot
    return out


def _today_row(series_last: dict[str, dict | None]) -> pd.DataFrame:
    """把「今日」写进历史表：两市各一行 + 合计一行。

    合计单独存一行（secid=total）而不是每次现算 —— 沪市或深市某天缺失时，
    现算的合计会静默变小（缺一边），那是**看着合理但已经错了**的数据。
    """
    d = clock.today().strftime("%Y-%m-%d")
    rows = []
    for key, secid, _name in sources.MARKET_SECIDS:
        last = series_last.get(key)
        if not last:
            continue
        rows.append({"date": d, "secid": secid, "main": last.get("main"),
                     "small": last.get("small"), "medium": last.get("medium"),
                     "large": last.get("large"), "xlarge": last.get("xlarge"),
                     "main_pct": last.get("main_pct"), "close": last.get("close"),
                     "chg_pct": last.get("chg_pct")})
    if len(rows) == len(sources.MARKET_SECIDS):
        rows.append({
            "date": d, "secid": "total",
            "main": sum(r["main"] or 0 for r in rows) or None,
            "small": sum(r["small"] or 0 for r in rows) or None,
            "medium": sum(r["medium"] or 0 for r in rows) or None,
            "large": sum(r["large"] or 0 for r in rows) or None,
            "xlarge": sum(r["xlarge"] or 0 for r in rows) or None,
            "main_pct": None, "close": None, "chg_pct": None})
    out = pd.DataFrame(rows)
    if out.empty:
        return pd.DataFrame(columns=["secid"] + list(_KEEP_COLS))
    return out[["secid"] + list(_KEEP_COLS)]


def _history(hist: pd.DataFrame, days: int = HISTORY_DAYS) -> dict:
    """近 N 个交易日的两市 + 合计主力净流入（亿元）。"""
    empty = {"dates": [], "sh": [], "sz": [], "sum": [], "days": 0}
    if hist is None or hist.empty:
        return empty
    piv = hist.pivot_table(index="date", columns="secid", values="main",
                           aggfunc="last").sort_index().tail(days)
    if piv.empty:
        return empty
    key_of = {"1.000001": "sh", "0.399001": "sz", "total": "sum"}

    def col(secid: str) -> list:
        if secid not in piv.columns:
            return [None] * len(piv)
        return [_yi(v) for v in piv[secid].tolist()]

    out = {"dates": [str(d) for d in piv.index], "days": int(len(piv))}
    for secid, k in key_of.items():
        out[k] = col(secid)
    return out


def build(snap: pd.DataFrame | None = None,
          universe: set[str] | None = None,
          cache: pd.DataFrame | None = None,
          intraday: bool = True) -> tuple[dict, pd.DataFrame]:
    """组装 flow.json，并返回**更新后的历史表**供调用方落盘。

    返回值是二元组（`rep`, `hist`）而不是把 DataFrame 塞进 rep：
    历史表要写 parquet 长期累积，而 rep 是要下发给浏览器的 JSON ——
    把 DataFrame 混进 JSON 会立刻炸在序列化上，分开返回最不容易写错。

    - `snap`：板块快照（含 f62 主力净流入 / f184 净占比），由 main.py 传进来复用，
      **不单独再请求一次** —— clist 是最容易触发对端限流的通道，能省一次是一次
    - `universe`：当前板块宇宙代码集合，把榜单收敛到真正关注的板块
    - `cache`：`flow_hist.parquet` 读出来的历史（可能为空，头几天就是空的）
    """
    series: dict[str, dict[str, float]] = {}
    series_last: dict[str, dict | None] = {}
    minute_ok = False
    for key, secid, name in sources.MARKET_SECIDS:
        rows: list[dict] = []
        try:
            rows = sources.fund_flow_minute(secid)
        except Exception as e:  # noqa: BLE001
            print(f"[warn] 资金流分时不可用（{name}）：{str(e)[:100]}")
        if rows:
            minute_ok = True
            series[key] = {str(r.get("date", ""))[-5:]: r.get("main") for r in rows}
            series_last[key] = rows[-1]
        else:
            series_last[key] = None

    tl, curve = _curve(series)
    market = _market_block(series_last)

    # ---- 历史：缓存 + 今日（今日用分时累计值覆盖，盘中多次运行取最后一次）
    hist = _norm_cache(cache)
    today_df = _today_row(series_last)
    if not today_df.empty:
        hist = pd.concat([hist, today_df], ignore_index=True)
    if not hist.empty:
        hist["date"] = hist["date"].astype(str).str.slice(0, 10)
        hist = (hist.drop_duplicates(subset=["secid", "date"], keep="last")
                    .sort_values(["date", "secid"]).reset_index(drop=True))

    # ---- 板块榜单（复用已经拿到的快照，不再额外请求）
    boards: list[dict] = []
    if snap is not None and not snap.empty and "main_inflow" in snap.columns:
        d = snap
        if universe:
            d = d[d["code"].astype(str).isin(universe)]
        for _, r in d.iterrows():
            mi = _yi(r.get("main_inflow"))
            if mi is None:
                continue
            boards.append({
                "code": str(r.get("code")), "name": str(r.get("name")),
                "main_yi": mi,
                "main_pct": _num(r.get("main_inflow_pct")),
                "pct": _num(r.get("pct")),
            })
        boards.sort(key=lambda x: -(x["main_yi"] or 0))

    inflow_n = sum(1 for b in boards if (b["main_yi"] or 0) > 0)
    stats = {
        "board_count": len(boards),
        "inflow_n": inflow_n,
        "outflow_n": len(boards) - inflow_n,
        "net_yi": round(sum(b["main_yi"] or 0 for b in boards), 2) if boards else None,
    }

    rep = {
        "updated": clock.now().strftime("%Y-%m-%d %H:%M:%S"),
        "intraday": bool(intraday),
        "minute_ok": minute_ok,
        "market": market,
        "curve": {"t": tl, **curve},
        "history": _history(hist),
        "boards": boards,
        "stats": stats,
        "source": "eastmoney fflow/kline（大盘分时）+ clist f62/f184（板块）",
        "note": ("主力 = 大单 + 超大单；单位亿元；分时值为自开盘累计。"
                 "历史为每日本系统累积（首日起逐日增加），当日值盘中会被最新一次刷新覆盖"),
    }
    return rep, hist


if __name__ == "__main__":
    import json
    import pathlib
    import sys

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    r, h = build()
    print("历史表行数 =", len(h))
    if not h.empty:
        print(h.tail(4).to_string(index=False))
    r["boards"] = r["boards"][:3]
    r["curve"] = {k: (v[:5] if isinstance(v, list) else v) for k, v in r["curve"].items()}
    print(json.dumps(r, ensure_ascii=False, indent=1)[:2400])
