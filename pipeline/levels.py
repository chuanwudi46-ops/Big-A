#!/usr/bin/env python3
"""大盘关键位：按周线 / 月线 / 年线给出压力位与支撑位。

思路
--------------------------------------------------------------------------
支撑压力位的本质是「历史上价格在此处被反复拒绝或承接」。最朴素也最稳的
做法是找**摆动高低点（swing high / low）**：一根 K 线的高点比左右各 k 根
都高，它就是一个结构高点，构成上方压力；低点反之。

为什么要分周期：同一个点位在不同级别上的意义完全不同。
周线前高是短线压力，月线/年线前高是中期天堑 —— 分开列才看得清路。
日线级别的噪音太大（几乎每天都有新高新低），所以不做日线档。

产物：web/data/levels.json，供前端渲染「大盘位置」卡片。
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib

import pandas as pd

# 各周期的回看根数与摆动识别窗口 (左, 右)
# 周线看 2 年、月线看 5 年、年线看 10 年；年线本身只有几根，k 取 1 即可
PERIODS = [
    ("week", "周线", 104, 2),
    ("month", "月线", 60, 2),
    ("year", "年线", 10, 1),
]

INDICES = [
    ("sh000001", "上证指数"),
    ("sh000300", "沪深300"),
]


# --------------------------------------------------------------------- 工具
def normalize(df: pd.DataFrame) -> pd.DataFrame:
    """把不同数据源的指数日线统一成 date/open/high/low/close。

    akshare 走新浪源返回 date/open/high/low/close/volume；
    东财兜底通道返回 date/open/close/high/low/... —— 列名一致但顺序不同，
    且两边都可能缺列（缺列会让后续 max/min 静默算错），所以显式校验。
    """
    if df is None or df.empty:
        return pd.DataFrame()
    low = {str(c).lower(): c for c in df.columns}
    need = {"date": None, "open": None, "high": None, "low": None, "close": None}
    for k in need:
        if k in low:
            need[k] = low[k]
    missing = [k for k, v in need.items() if v is None]
    if missing:
        raise ValueError(f"指数日线缺列 {missing}（实际列：{list(df.columns)}）")
    out = pd.DataFrame({
        "date": pd.to_datetime(df[need["date"]]),
        "open": pd.to_numeric(df[need["open"]], errors="coerce"),
        "high": pd.to_numeric(df[need["high"]], errors="coerce"),
        "low": pd.to_numeric(df[need["low"]], errors="coerce"),
        "close": pd.to_numeric(df[need["close"]], errors="coerce"),
    }).dropna(subset=["date", "high", "low", "close"])
    return out.sort_values("date").reset_index(drop=True)


def to_bars(df: pd.DataFrame, kind: str) -> pd.DataFrame:
    """日线聚合成周线 / 月线 / 年线。

    不用 pandas 的 resample：`M`/`Y` 这类频率别名在 pandas 2→3 之间改过名
    （M→ME、Y→YE），跨版本容易踩坑。按字符串键 groupby 反而更稳，
    而且周线要的是 ISO 周（%G-%V），resample 的 W 是自然周起点对齐，不等价。
    """
    if df.empty:
        return df
    d = pd.to_datetime(df["date"])
    if kind == "week":
        key = d.dt.strftime("%G-%V")          # ISO 年-周，跨年周不会串
    elif kind == "month":
        key = d.dt.strftime("%Y-%m")
    else:
        key = d.dt.strftime("%Y")

    tmp = df.assign(_k=key, _d=d)
    g = tmp.groupby("_k", sort=True)
    bars = pd.DataFrame({
        "open": g["open"].first(),
        "high": g["high"].max(),
        "low": g["low"].min(),
        "close": g["close"].last(),
        "start": g["_d"].min(),
        "end": g["_d"].max(),
    }).reset_index(drop=True)
    return bars


def swing_points(bars: pd.DataFrame, k: int = 2) -> list[dict]:
    """识别摆动高低点。

    判据：某根 K 线的高点在 [i-k, i+k] 窗口内是**唯一最大值**。
    要求"唯一"是为了避免平台整理（连续几根同价）被重复识别成一串压力位。
    最后 k 根不参与判断（右侧还没走完，谈不上"摆动"）。
    """
    n = len(bars)
    if n < 2 * k + 3:
        return []
    hi = bars["high"].to_numpy(dtype=float)
    lo = bars["low"].to_numpy(dtype=float)
    out: list[dict] = []
    for i in range(k, n - k):
        wh = hi[i - k:i + k + 1]
        if hi[i] == wh.max() and (wh == hi[i]).sum() == 1:
            out.append({"idx": i, "price": float(hi[i]),
                        "date": bars["end"].iloc[i].strftime("%Y-%m-%d"), "kind": "high"})
        wl = lo[i - k:i + k + 1]
        if lo[i] == wl.min() and (wl == lo[i]).sum() == 1:
            out.append({"idx": i, "price": float(lo[i]),
                        "date": bars["end"].iloc[i].strftime("%Y-%m-%d"), "kind": "low"})
    return out


def period_levels(bars: pd.DataFrame, close: float, k: int, top: int = 3) -> dict:
    """一个周期上的压力位 / 支撑位。

    只在**现价之上**找压力、**现价之下**找支撑 —— 这是关键：把下方的
    摆动高点也列出来毫无意义（已经被突破了）。各自取最近的 top 个。
    现价创出区间新高时上方没有结构压力，此时明确标注出来，
    而不是硬凑一个数字（硬凑会给出误导性的"压力位"）。

    `near` 标记距离 <0.3% 的位置：它其实是"正在测试"的前高/前低，
    是实盘最需要盯的，但按百分比看容易被当成噪音忽略掉。
    """
    if bars.empty:
        return {}
    rng_high = float(bars["high"].max())
    rng_low = float(bars["low"].min())
    pts = swing_points(bars, k)

    # swing 识别会排除最后 k 根（右侧还没走完，谈不上"摆动"），
    # 于是**最近刚创出的那个高点/低点反而漏掉** —— 而它恰恰是现价上方最
    # 直接的压力。故把区间极值也纳入候选。
    hi_i = int(bars["high"].idxmax())
    lo_i = int(bars["low"].idxmin())
    pts.append({"idx": hi_i, "price": rng_high, "kind": "high",
                "date": bars["end"].iloc[hi_i].strftime("%Y-%m-%d")})
    pts.append({"idx": lo_i, "price": rng_low, "kind": "low",
                "date": bars["end"].iloc[lo_i].strftime("%Y-%m-%d")})
    # 区间极值通常本身就是一个 swing 点，按 (类型, 价格) 去重
    uniq: dict[tuple, dict] = {}
    for p in pts:
        uniq.setdefault((p["kind"], round(p["price"], 2)), p)
    pts = list(uniq.values())

    res = sorted([p for p in pts if p["kind"] == "high" and p["price"] > close],
                 key=lambda p: p["price"])[:top]
    sup = sorted([p for p in pts if p["kind"] == "low" and p["price"] < close],
                 key=lambda p: -p["price"])[:top]

    def fmt(p: dict) -> dict:
        gap = (p["price"] / close - 1) * 100
        return {"price": round(p["price"], 2),
                "gap_pct": round(gap, 2),
                "date": p["date"],
                "near": abs(gap) < 0.3}

    span = rng_high - rng_low
    return {
        "bars": int(len(bars)),
        "from": bars["start"].iloc[0].strftime("%Y-%m-%d"),
        "to": bars["end"].iloc[-1].strftime("%Y-%m-%d"),
        "range_high": round(rng_high, 2),
        "range_low": round(rng_low, 2),
        # 区间位置：0 = 区间最低，1 = 区间最高
        "position": None if span <= 0 else round((close - rng_low) / span, 3),
        "resistance": [fmt(p) for p in res],
        "support": [fmt(p) for p in sup],
        "at_range_high": close >= rng_high * 0.999,
        "at_range_low": close <= rng_low * 1.001,
    }


def position_text(pos: float | None) -> str:
    if pos is None:
        return "—"
    if pos >= 0.9:
        return "区间上沿"
    if pos >= 0.7:
        return "偏上"
    if pos >= 0.4:
        return "中枢"
    if pos >= 0.15:
        return "偏下"
    return "区间下沿"


def analyze(name: str, code: str, daily: pd.DataFrame) -> dict:
    """对单个指数出一份多周期关键位报告"""
    df = normalize(daily)
    if df.empty:
        raise ValueError(f"{name} 无可用日线")
    close = float(df["close"].iloc[-1])
    prev = float(df["close"].iloc[-2]) if len(df) > 1 else close

    periods = []
    for kind, label, lookback, k in PERIODS:
        bars = to_bars(df, kind)
        if bars.empty:
            continue
        seg = bars.tail(lookback).reset_index(drop=True)
        lv = period_levels(seg, close, k)
        if not lv:
            continue
        lv["position_text"] = position_text(lv.get("position"))
        periods.append({"key": kind, "label": label, **lv})

    # 综合：跨周期取「最近」的那一档 —— 这才是实盘最先碰到的位置
    all_res = [(p["label"], r) for p in periods for r in p["resistance"]]
    all_sup = [(p["label"], s) for p in periods for s in p["support"]]
    nearest_res = min(all_res, key=lambda x: x[1]["price"]) if all_res else None
    nearest_sup = max(all_sup, key=lambda x: x[1]["price"]) if all_sup else None

    summary = {
        "nearest_resistance": None if nearest_res is None else
            {"period": nearest_res[0], **nearest_res[1]},
        "nearest_support": None if nearest_sup is None else
            {"period": nearest_sup[0], **nearest_sup[1]},
    }

    def phrase(item, word, near_word):
        if item is None:
            return f"上方无{word}（已站上区间上沿）"
        if item["near"]:
            return (f"正测试{word} {item['price']}（{item['period']}{near_word}，"
                    f"{item['gap_pct']:+.2f}%）")
        return (f"{word} {item['price']}（{item['period']}前{near_word[0]}，"
                f"{item['gap_pct']:+.2f}%）")

    summary["text"] = (f"{name} {close:.2f}："
                       + phrase(summary["nearest_resistance"], "压力", "高点")
                       + "；"
                       + phrase(summary["nearest_support"], "支撑", "低点"))

    # 有信息量的额外提示（谁在创新高/新低、谁被逼近）
    notes = []
    for p in periods:
        if p["at_range_high"]:
            span_years = max(1, round(p["bars"] / (52 if p["key"] == "week" else
                                                   12 if p["key"] == "month" else 1)))
            notes.append(f"已站上{p['label']}区间上沿（近 {span_years} 年新高）")
        elif p["at_range_low"]:
            notes.append(f"处于{p['label']}区间下沿")
    nres = summary["nearest_resistance"]
    if nres and nres["near"]:
        notes.append(f"上方 {nres['price']} 为{nres['period']}关键前高"
                     f"（形成于 {nres['date']}），能否放量突破决定方向")
    summary["notes"] = notes

    return {
        "code": code, "name": name,
        "date": df["date"].iloc[-1].strftime("%Y-%m-%d"),
        "close": round(close, 2),
        "pct": round((close / prev - 1) * 100, 2) if prev else None,
        "periods": periods,
        "summary": summary,
    }


def build(index_daily) -> dict:
    """入口：index_daily(symbol) -> DataFrame，由调用方注入以复用 sources 的重试逻辑"""
    out: list[dict] = []
    for code, name in INDICES:
        try:
            out.append(analyze(name, code, index_daily(code)))
        except Exception as e:  # noqa: BLE001
            print(f"[warn] {name} 关键位计算失败：{str(e)[:120]}")
    if not out:
        raise RuntimeError("所有指数关键位均计算失败")
    return {
        "updated": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "indices": out,
    }


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    import sources  # noqa: E402

    rep = build(sources.index_daily)
    print(json.dumps(rep, ensure_ascii=False, indent=1)[:3000])
