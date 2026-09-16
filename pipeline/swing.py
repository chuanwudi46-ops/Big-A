#!/usr/bin/env python3
"""板块位置：从区间高点回撤多少、从区间低点反弹多少。

为什么用「区间极值」而不是「上一次波段高/低」
--------------------------------------------------------------------------
波段高低点需要主观判断"哪一段是一波"，不同人画出来不一样，回测也没法复现。
区间极值（近 N 日最高价 / 最低价）口径唯一、可复现，且恰好回答两个最实用的
问题：**离天花板还有多远**、**离地板已经涨了多少**。

窗口取 20 / 60 / 250 交易日三档：
- 20 日  —— 月内节奏，看短线是不是追高
- 60 日  —— 季度节奏，看中期趋势有没有走坏
- 250 日 —— 近一年，看大级别位置（默认展示口径）

配合 `position`（现价在区间中的分位）一起看才有意义：
回撤 -30% 却处在区间 80% 分位，说明是**在高位刚回落**；
回撤 -30% 且处在 20% 分位，才是**跌到地板附近**。
"""
from __future__ import annotations

import pandas as pd

# (窗口交易日数, 标签, 简称)
WINDOWS = [(20, "20 个交易日", "20日"),
           (60, "60 个交易日", "60日"),
           (250, "近一年", "250日")]

MAIN_WINDOW = 250


def _series(df: pd.DataFrame) -> tuple[pd.Series, pd.Series, pd.Series, pd.Series]:
    """取 date/high/low/close 四列，列名大小写不敏感。

    板块 K 线来自腾讯或东财两个通道，列名一致但顺序不同；缺列会让
    max/min 静默算错（而不是报错），所以这里显式校验。
    """
    lower = {str(c).lower(): c for c in df.columns}
    need = {}
    for k in ("date", "high", "low", "close"):
        if k not in lower:
            raise ValueError(f"K线缺列 {k}（实际列：{list(df.columns)}）")
        need[k] = lower[k]
    d = pd.to_datetime(df[need["date"]], errors="coerce")
    hi = pd.to_numeric(df[need["high"]], errors="coerce")
    lo = pd.to_numeric(df[need["low"]], errors="coerce")
    cl = pd.to_numeric(df[need["close"]], errors="coerce")
    keep = d.notna() & hi.notna() & lo.notna() & cl.notna()
    return d[keep], hi[keep], lo[keep], cl[keep]


def one_window(d: pd.Series, hi: pd.Series, lo: pd.Series, cl: pd.Series,
               n: int) -> dict | None:
    """单个窗口的回撤 / 涨幅 / 位置"""
    if len(cl) < 2:
        return None
    seg_d, seg_hi, seg_lo, seg_cl = d.tail(n), hi.tail(n), lo.tail(n), cl.tail(n)
    close = float(seg_cl.iloc[-1])
    high, low = float(seg_hi.max()), float(seg_lo.min())
    hi_i = int(seg_hi.to_numpy().argmax())
    lo_i = int(seg_lo.to_numpy().argmin())

    span = high - low
    return {
        "bars": int(len(seg_cl)),
        "high": round(high, 2),
        "high_date": seg_d.iloc[hi_i].strftime("%Y-%m-%d"),
        "low": round(low, 2),
        "low_date": seg_d.iloc[lo_i].strftime("%Y-%m-%d"),
        # 距离高点已过去多少个交易日 —— 判断回撤是"刚开始"还是"磨了很久"
        "days_since_high": int(len(seg_cl) - 1 - hi_i),
        "days_since_low": int(len(seg_cl) - 1 - lo_i),
        # 回撤为负值（如 -0.184 = 距高点 -18.4%），反弹为正值
        "drawdown": None if high <= 0 else round(close / high - 1, 4),
        "rebound": None if low <= 0 else round(close / low - 1, 4),
        "position": None if span <= 0 else round((close - low) / span, 3),
        "at_high": close >= high,
        "at_low": close <= low,
    }


def swing_stats(df: pd.DataFrame) -> dict:
    """板块区间位置统计。返回 {close, windows: {"20"/"60"/"250": {...}}}"""
    d, hi, lo, cl = _series(df)
    if cl.empty:
        raise ValueError("K线为空")
    out: dict[str, dict] = {}
    for n, label, short in WINDOWS:
        w = one_window(d, hi, lo, cl, n)
        if w:
            w["label"] = label
            out[str(n)] = w
    if not out:
        raise ValueError("窗口不足，无法统计")
    return {"close": round(float(cl.iloc[-1]), 2),
            "date": d.iloc[-1].strftime("%Y-%m-%d"),
            "windows": out}


def brief(swing: dict, windows: tuple[int, ...] = (20, 60, 250)) -> dict:
    """精简版，供 index.json 列表展示。

    保留全部三个窗口（前端要能切换 20/60/250 而不必回源单个板块 JSON），
    但只留数字字段 —— 日期、标签这些长字符串进 index 会让产物膨胀十倍。
    """
    src = swing.get("windows") or {}
    out: dict[str, dict] = {}
    for n in windows:
        w = src.get(str(n))
        if not w:
            continue
        out[str(n)] = {"dd": w.get("drawdown"), "rb": w.get("rebound"),
                       "pos": w.get("position"), "dsh": w.get("days_since_high")}
    return out


if __name__ == "__main__":
    import sys
    import pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    import json

    CACHE = pathlib.Path(__file__).resolve().parents[1] / "web" / "data" / "cache"
    files = sorted(CACHE.glob("*.parquet"))[:5]
    if not files:
        print("无本地 K 线缓存，跳过")
        sys.exit(0)
    for fp in files:
        df = pd.read_parquet(fp)
        st = swing_stats(df)
        w = st["windows"]["250"]
        print(f"{fp.stem}: 收 {st['close']}  近一年高点 {w['high']}({w['high_date']}) "
              f"低点 {w['low']}({w['low_date']})  "
              f"回撤 {w['drawdown']*100:+.2f}%  反弹 {w['rebound']*100:+.2f}%  "
              f"位置 {w['position']:.0%}")
