#!/usr/bin/env python3
"""大盘关键位：均线体系（动态）+ 摆动高低点（静态）两套支撑压力。

思路
--------------------------------------------------------------------------
支撑压力位有两类来源，性质完全不同，**必须都给，不能只给一套**：

1) **均线体系（动态、会走）** —— 5 / 10 / 20 / 30 / 60 日线与年线。
   它代表**市场的持仓成本**：某条均线就是最近 N 天买入者的平均成本。
   惯例判定：现价在均线**上方** → 该均线是回踩支撑（成本在这里、有人护）；
   在**下方** → 是反弹压力（套牢盘在这里、有人解套）。所以同一条均线
   是支撑还是压力，**取决于现价相对它的位置**，不能写死。
   另配斜率（走平 / 上翘 / 下弯）与多空排列 —— 走平的均线支撑力最强，
   下弯的均线最容易一碰就破。

2) **摆动高低点（静态、不走）** —— 前高 / 前低。
   它代表**价格记忆**：历史上价格在此处被反复拒绝或承接。

为什么要分周期：同一个点位在不同级别上的意义完全不同。
**日线**前高是当天/当周就要面对的一线位置，周线前高是短线压力，
月线/年线前高是中期天堑 —— 分开列才看得清路。

日线档怎么做才不噪音（2026-09-16 补）：
裸的日线摆动点几乎天天都有，直接把 k=2 的摆动点列出来会得到一堆相距
0.1% 的「压力位」，等于没有。故日线档做两件事：
  1) 摆动识别窗口放大到 **k=5**（左右各 5 根），只留真正的结构高低点；
  2) 同价位**聚类合并**（默认 0.8% 内算同一个位置区），合并后取「离现价最近」
     的那一档，并把命中次数记为 `touches`（被反复测试 3 次的位置显然比
     只碰过一次的更硬）。周线/月线/年线保持 merge_pct=0，输出与历史完全一致。

产物：web/data/levels.json，供前端渲染「大盘位置」视图。
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib

import pandas as pd

import clock

# 各周期的 (key, 标签, 回看根数, 摆动识别窗口 k, 同价位聚类阈值 %, 最多档数)
# 周线看 2 年、月线看 5 年、年线看 10 年；年线本身只有几根，k 取 1 即可。
# 日线看约半年（120 根）：太短则整段都在一个箱体里、太长则远端的点位已失效。
# 只有日线需要聚类（merge_pct>0），其余周期保持 0 = 与历史输出逐字节一致。
PERIODS = [
    ("day", "日线", 120, 5, 0.8, 3),
    ("week", "周线", 104, 2, 0.0, 3),
    ("month", "月线", 60, 2, 0.0, 3),
    ("year", "年线", 10, 1, 0.0, 3),
]

# 一根 bar 覆盖多少个交易日：用于把「回看根数」换算成年，仅供提示文案使用
BARS_PER_YEAR = {"day": 252, "week": 52, "month": 12, "year": 1}

INDICES = [
    ("sh000001", "上证指数"),
    ("sh000300", "沪深300"),
]

# 均线体系：(窗口, 标签)。用户明确要的六条：5 / 10 / 20 / 30 / 60 日线 + 年线。
# 年线取 **250 日**（A 股惯例；也有用 240 的，但东财/同花顺默认 250，跟它对齐才不会
# 出现「软件上站上年线、我们这儿还差一点」这种对不上的尴尬）。
# ⚠️ 标签里不要写「日线」以外的口径（如「季线」）—— 60 日线的俗称是「季线」，
# 但用户是按日数提的，写成日数最不容易歧义。
MA_WINDOWS = [
    (5, "5日线"), (10, "10日线"), (20, "20日线"),
    (30, "30日线"), (60, "60日线"), (250, "年线"),
]

# 判定「均线密集」的阈值（5/10/20/30/60 五条线的极差 ÷ 中位价）。
# 密集意味着方向未选、随时可能变盘，是均线体系里信息量最大的一种形态。
MA_DENSE_PCT = 2.0


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
    if kind == "day":
        # 日线不聚合，但补出 start/end 两列（period_levels 用它们取日期），
        # 语义与聚合后的 bar 保持一致：start = end = 该日
        out = df.copy()
        out["start"] = d.values
        out["end"] = d.values
        return out.reset_index(drop=True).sort_values("end").reset_index(drop=True)
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


def cluster_levels(pts: list[dict], close: float, tol_pct: float) -> list[dict]:
    """把相邻（价差 <= tol_pct%）的同类型摆动点合并成一个「位置区」。

    只在日线档使用。合并规则：
    - 按价格排序后贪心聚簇，同一簇内只保留**离现价最近**的那一档 ——
      实盘最先碰到的是它，更远的同簇点位暂不构成独立参考；
    - `touches` 记录该位置区被测试过几次（越多越硬，前端会标出来）；
    - 日期取簇内**最近一次**测试的日期（最新证据优先）；
    - 额外给出 price_lo / price_hi，让前端能显示这是一个区间而不是一个点。
    """
    if tol_pct <= 0 or len(pts) <= 1:
        return pts
    out: list[dict] = []
    for kind in ("high", "low"):
        grp = sorted([p for p in pts if p["kind"] == kind], key=lambda p: p["price"])
        if not grp:
            continue
        cur = [grp[0]]
        for p in grp[1:]:
            base = cur[-1]["price"]
            if base > 0 and (p["price"] - base) / base * 100.0 <= tol_pct:
                cur.append(p)
            else:
                out.append(_merge(cur, close))
                cur = [p]
        out.append(_merge(cur, close))
    return out


def _merge(group: list[dict], close: float) -> dict:
    best = min(group, key=lambda p: abs(p["price"] - close))
    m = dict(best)
    m["touches"] = len(group)
    m["price_lo"] = round(min(p["price"] for p in group), 2)
    m["price_hi"] = round(max(p["price"] for p in group), 2)
    m["date"] = max(p["date"] for p in group)      # 最近一次被测试的日期
    return m


def period_levels(bars: pd.DataFrame, close: float, k: int,
                  top: int = 3, merge_pct: float = 0.0) -> dict:
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
    pts = cluster_levels(pts, close, merge_pct)      # merge_pct=0 时原样返回

    res = sorted([p for p in pts if p["kind"] == "high" and p["price"] > close],
                 key=lambda p: p["price"])[:top]
    sup = sorted([p for p in pts if p["kind"] == "low" and p["price"] < close],
                 key=lambda p: -p["price"])[:top]

    def fmt(p: dict) -> dict:
        gap = (p["price"] / close - 1) * 100
        out = {"price": round(p["price"], 2),
               "gap_pct": round(gap, 2),
               "date": p["date"],
               "near": abs(gap) < 0.3}
        # touches>1 才输出：代表这是一个被反复测试的「位置区」，不是单点
        if p.get("touches", 1) > 1:
            out["touches"] = int(p["touches"])
            out["zone"] = [p["price_lo"], p["price_hi"]]
        return out

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


def _high_phrase(p: dict) -> str:
    """「创新高」的说法按周期换算：日线说交易日数，其余说年数"""
    bars = int(p.get("bars") or 0)
    if p.get("key") == "day":
        return f"创 {bars} 个交易日新高"
    return f"创近 {max(1, round(bars / BARS_PER_YEAR.get(p['key'], 1)))} 年新高"


def ma_levels(df: pd.DataFrame, close: float) -> dict:
    """均线体系：动态支撑压力。

    现价在均线**上方** → 该均线是**支撑**（那里是最近 N 天买入者的平均成本，
    回踩到成本区通常有承接）；在**下方** → 是**压力**（反弹到成本区会遇到解套抛压）。
    所以 role 由现价与均线的相对位置决定，**不能写死**。

    `slope_pct` 是这条均线近 5 根的变化率，决定这条线的含金量：
    走平的最硬（成本高度集中），下弯的最容易一碰就破。

    ⚠️ 必须拿**完整日线**来算，不能复用某个周期的回看片段：MA60 / 年线在
    120 根的日线片段上算不出来，会**静默少两条线**（页面看着正常，实际缺档）。
    """
    if df is None or df.empty:
        return {}
    c = pd.to_numeric(df["close"], errors="coerce").astype(float)
    lines: list[dict] = []
    for win, label in MA_WINDOWS:
        if len(c) < win:
            continue
        s = c.rolling(win).mean()
        cur = s.iloc[-1]
        if not pd.notna(cur) or float(cur) <= 0:
            continue
        cur = float(cur)
        prev = s.iloc[-6] if len(s) >= 6 and pd.notna(s.iloc[-6]) else None
        slope = None if prev is None or float(prev) <= 0 else (cur / float(prev) - 1) * 100
        gap = (close / cur - 1) * 100
        lines.append({
            "key": f"ma{win}", "label": label, "window": win,
            "price": round(cur, 2),
            "gap_pct": round(gap, 2),
            "above": close >= cur,                       # 现价是否在该均线上方
            "role": "support" if close >= cur else "resistance",
            "slope_pct": None if slope is None else round(slope, 3),
        })
    if not lines:
        return {}

    # 多空排列只看短中期五条：年线太慢，混进来会让「排列」几乎永远判不出来
    seq = [l for l in lines if l["window"] != 250]
    arrangement = None
    if len(seq) == 5:
        px = [l["price"] for l in seq]
        if all(px[i] > px[i + 1] for i in range(4)):
            arrangement = "多头排列"
        elif all(px[i] < px[i + 1] for i in range(4)):
            arrangement = "空头排列"
        else:
            arrangement = "交织"
    lo = min(l["price"] for l in seq) if seq else None
    hi = max(l["price"] for l in seq) if seq else None
    mid = ((lo + hi) / 2) if seq else 0.0
    dense = bool(seq and mid > 0 and (hi - lo) / mid * 100 <= MA_DENSE_PCT)

    sup = [l for l in lines if l["role"] == "support"]
    res = [l for l in lines if l["role"] == "resistance"]
    # 「最近」= 离现价最近：支撑取下方最高的一条，压力取上方最低的一条
    near_sup = max(sup, key=lambda l: l["price"]) if sup else None
    near_res = min(res, key=lambda l: l["price"]) if res else None

    # 年线的得而复失 / 失而复得是最有意义的状态切换：只报「变化」，
    # 否则「站上年线」会连续几十天重复出现在提示里，把版面占满
    year_cross = None
    if len(c) >= 251:
        s_y = c.rolling(250).mean()
        if pd.notna(s_y.iloc[-2]):
            was = float(c.iloc[-2]) >= float(s_y.iloc[-2])
            now = close >= float(s_y.iloc[-1])
            if was and not now:
                year_cross = "失守年线"
            elif not was and now:
                year_cross = "收复年线"

    def _ph(item: dict | None, word: str, empty: str) -> str:
        if item is None:
            return empty
        return f"{item['label']} {item['price']}（{item['gap_pct']:+.2f}%）"

    bits = []
    if near_sup:
        bits.append("下方最近均线支撑 " + _ph(near_sup, "", ""))
    if near_res:
        bits.append("上方最近均线压力 " + _ph(near_res, "", ""))
    if arrangement:
        bits.append(arrangement)
    if dense:
        bits.append(f"5/10/20/30/60 日线密集（极差 {(hi - lo) / mid * 100:.2f}%，方向待选）")
    if year_cross:
        bits.append(year_cross)

    return {
        "lines": lines,
        "arrangement": arrangement,
        "dense": dense,
        "above_count": sum(1 for l in lines if l["above"]),
        "below_count": sum(1 for l in lines if not l["above"]),
        "nearest_support": near_sup,
        "nearest_resistance": near_res,
        "year_cross": year_cross,
        "text": "；".join(bits),
    }


def analyze(name: str, code: str, daily: pd.DataFrame) -> dict:
    """对单个指数出一份多周期关键位报告"""
    df = normalize(daily)
    if df.empty:
        raise ValueError(f"{name} 无可用日线")
    close = float(df["close"].iloc[-1])
    prev = float(df["close"].iloc[-2]) if len(df) > 1 else close

    # 均线用**完整日线**算（MA60 / 年线在 120 根片段上算不出来）
    ma = ma_levels(df, close)

    periods = []
    for kind, label, lookback, k, merge_pct, top in PERIODS:
        bars = to_bars(df, kind)
        if bars.empty:
            continue
        seg = bars.tail(lookback).reset_index(drop=True)
        lv = period_levels(seg, close, k, top=top, merge_pct=merge_pct)
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
            notes.append(f"已站上{p['label']}区间上沿（{_high_phrase(p)}）")
        elif p["at_range_low"]:
            notes.append(f"处于{p['label']}区间下沿")
    # 日线是唯一做了「同价位聚类」的档位：touches>1 说明这是被反复测试的位置区，
    # 比单点更硬，值得单独提示（后端只会为日线输出 touches 字段）
    dayp = next((p for p in periods if p["key"] == "day"), None)
    if dayp:
        for word, arr in (("压力", dayp["resistance"]), ("支撑", dayp["support"])):
            hit = next((x for x in arr if x.get("touches", 1) > 1), None)
            if hit:
                lo, hi = hit["zone"]
                notes.append(f"日线{word} {hit['price']} 为位置区（{lo}~{hi}，"
                             f"{hit['touches']} 次测试，最近 {hit['date']}）")
                break
    nres = summary["nearest_resistance"]
    if nres and nres["near"]:
        notes.append(f"上方 {nres['price']} 为{nres['period']}关键前高"
                     f"（形成于 {nres['date']}），能否放量突破决定方向")

    # ---- 均线形态提示：与摆动档位的提示**合并成一列 notes**，
    # 前端沿用既有结构即可显示，不用为均线再开一块渲染逻辑
    if ma:
        if ma.get("year_cross"):
            notes.append(f"**{ma['year_cross']}**（年线 {ma['lines'][-1]['price']}）")
        if ma.get("dense"):
            notes.append(f"5/10/20/30/60 日线高度密集，方向选择临近"
                         f"（当前{ma['arrangement']}）")
        n_line = len(ma["lines"])
        if ma["above_count"] == n_line:
            notes.append(f"站上全部 {n_line} 条均线（5/10/20/30/60 日线 + 年线）")
        elif ma["below_count"] == n_line:
            notes.append(f"跌破全部 {n_line} 条均线，均线全数转为上方压力")
        elif ma.get("arrangement") == "多头排列":
            notes.append("5/10/20/30/60 日线多头排列 —— 回踩均线不破视为趋势延续")
        elif ma.get("arrangement") == "空头排列":
            notes.append("5/10/20/30/60 日线空头排列 —— 反弹到均线附近先按压力看")
    summary["ma_text"] = ma.get("text", "")
    summary["notes"] = notes

    return {
        "code": code, "name": name,
        "date": df["date"].iloc[-1].strftime("%Y-%m-%d"),
        "close": round(close, 2),
        "pct": round((close / prev - 1) * 100, 2) if prev else None,
        "ma": ma,
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
        "updated": clock.now().strftime("%Y-%m-%d %H:%M:%S"),
        "indices": out,
    }


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    import sources  # noqa: E402

    rep = build(sources.index_daily)
    print(json.dumps(rep, ensure_ascii=False, indent=1)[:3000])
