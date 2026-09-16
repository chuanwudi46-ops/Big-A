"""12 维因子计算

约定：除筹码供给罚分外，全部因子输出 [0, 1] 的分位分（越大越好）。
归一化统一采用滚动 250 日窗口的分位排名，避免绝对阈值随市场漂移。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

WINDOW = 250

# 情绪词典（可扩充）
POS = ["增长", "超预期", "中标", "涨价", "提价", "翻倍", "创新高", "利好", "复苏",
       "突破", "扩大", "签约", "获批", "增持", "回购", "扭亏", "上调", "放量",
       "新高", "加码", "提速"]
NEG = ["下滑", "不及预期", "处罚", "降价", "暴跌", "亏损", "利空", "减持", "退市",
       "违约", "立案", "调查", "停产", "下调", "低于预期", "风险警示", "终止",
       "亏损扩大", "承压", "下调评级"]


def _clip(x: float) -> float:
    return float(min(max(x, 0.0), 1.0))


def _pctrank(s: pd.Series, window: int = WINDOW) -> float:
    s = pd.to_numeric(s, errors="coerce").dropna()
    if len(s) < 20:
        return 0.5
    w = s.tail(window)
    if not np.isfinite(w.iloc[-1]):
        return 0.5
    return float((w <= w.iloc[-1]).mean())


def _kdj(df: pd.DataFrame, n: int = 9, m1: int = 3, m2: int = 3):
    llv = df["low"].rolling(n).min()
    hhv = df["high"].rolling(n).max()
    rsv = (df["close"] - llv) / (hhv - llv).replace(0, np.nan) * 100
    k = rsv.ewm(alpha=1 / m1, adjust=False).mean()
    d = k.ewm(alpha=1 / m2, adjust=False).mean()
    return k, d, 3 * k - 2 * d


# ---------------------------------------------------------------- 1 动量
def momentum(df: pd.DataFrame, bench_r20: float | None = None) -> float:
    c = df["close"]
    if len(c) < 61:
        return 0.5
    r5 = c.iloc[-1] / c.iloc[-6] - 1
    r20 = c.iloc[-1] / c.iloc[-21] - 1
    r60 = c.iloc[-1] / c.iloc[-61] - 1
    raw = 0.5 * r5 + 0.3 * r20 + 0.2 * r60
    base = _clip((raw + 0.15) / 0.30)
    if bench_r20 is None or not np.isfinite(bench_r20):
        return base
    excess = _clip((r20 - bench_r20 + 0.10) / 0.20)
    return _clip(0.5 * base + 0.5 * excess)


# ------------------------------------------------------- 2 趋势结构
def trend(df: pd.DataFrame) -> float:
    if len(df) < 60:
        return 0.5
    c = df["close"]
    ma20 = c.rolling(20).mean()
    ma60 = c.rolling(60).mean()
    above = 1.0 if c.iloc[-1] > ma20.iloc[-1] else 0.0
    bull = 1.0 if ma20.iloc[-1] > ma60.iloc[-1] else 0.0
    k, d, j = _kdj(df)
    golden = 1.0 if (k.iloc[-1] > d.iloc[-1] and k.iloc[-2] <= d.iloc[-2]) else 0.0
    j_ok = _clip((j.iloc[-1] + 10) / 110)
    return _clip(0.25 * above + 0.25 * bull + 0.15 * golden + 0.35 * j_ok)


# ------------------------------------------------------- 3 量价健康
def volume_price(df: pd.DataFrame) -> float:
    if len(df) < 25 or "amount" not in df.columns:
        return 0.5
    c, a = df["close"], df["amount"]
    ret = c.pct_change()
    up = a[ret > 0].tail(20).mean()
    dn = a[ret < 0].tail(20).mean()
    if not np.isfinite(up) or not np.isfinite(dn) or dn <= 0:
        return 0.5
    s = _clip((up / dn - 0.7) / 0.8)
    # 缩量调整 = 健康（帽子哥："缩量调整是好事"）
    if c.iloc[-1] < c.iloc[-6] and a.tail(5).mean() < a.tail(20).mean():
        s = _clip(s + 0.15)
    # 量价背离扣分
    if c.iloc[-1] > c.iloc[-11] and a.tail(5).mean() < a.tail(20).mean() * 0.8:
        s = _clip(s - 0.20)
    return s


# --------------------------------------------------- 4 换手率位置
def turnover(df: pd.DataFrame) -> float:
    """换手率 250 日分位，**低换手加分**（取 1 − 分位）

    2026-09-16 修正方向。原实现是 `_pctrank(df["turnover"])`，即**换手越高分越高**，
    与 `weights.yaml` 的注释（「低分位加分」）和前端实时补丁的方向**都相反** ——
    前者是注释与代码不一致，后者是前后端不一致（前端 `(2 − 换手)/10` 是低换手加分）。

    长历史回测裁定（`docs/backtest_report_hist.md` §5.4 / §6.5.3）：
    换手最高 20% 的板块，未来 20 日 / 60 日超额收益比最低 20% 低 **0.60pt / 1.09pt**，
    且 5 个有效年份符号 **5/5 全负**；把本因子反向后，6 维组合分 IC 从
    -0.016/-0.031/-0.038/+0.018 改善到 **-0.001/-0.014/-0.021/+0.036**。
    """
    if "turnover" not in df.columns:
        return 0.5
    return _clip(1.0 - _pctrank(df["turnover"]))


# --------------------------------------------------- 5 主力资金流
def fund_flow(pct: float | None) -> float:
    """pct 为主力净流入占比（%）。>0 流入，<-0 流出"""
    if pct is None or not np.isfinite(pct):
        return 0.5
    return _clip((float(pct) - 1.0) / 6.0)


# --------------------------------------------------- 6 估值位置
def valuation(df: pd.DataFrame, pe: pd.Series | None = None) -> tuple[float, str]:
    """返回 (分位分, 口径标记)

    **两条支路的方向是相反的，别按「越低越好」一刀切**（2026-09-16 澄清）：

    - `pe` 有值时：PE 越低越好 → 对 PE **取负**后再算分位（`_pctrank(-pe)`）。
    - `pe` 为空时（生产现状，无真实行业 PE）：退化为 **252 日价格分位代理**，
      即 `_pctrank(close)` → **价格越高分越高**。

    回测裁定（`backtest_report_hist.md` §5.5 / §6.5.3）：价格最高 20% 的板块
    未来 120 日超额收益比最低 20% 高 **+2.64pt**，**支持现行代码方向** ——
    所以「价格分位代理」不是 bug，别顺手反转它；要改的是本文档原先把 PE 语义
    写成全局「越低越好」的表述（会误导下一个人把代码改坏）。
    真要引入增量信息，得换**真实行业 PE**（价格分位与动量共用同一份价格）。
    """
    if pe is not None:
        s = pd.to_numeric(pd.Series(pe), errors="coerce").dropna()
        if len(s) > 60:
            return _pctrank(-s), "pe_ttm"
    return _pctrank(df["close"]), "price_proxy"


# --------------------------------------------------- 7 周期位置
def cycle(df: pd.DataFrame, price_signal_hits: int) -> float:
    if len(df) < 61:
        return 0.5
    c = df["close"]
    r60 = c.iloc[-1] / c.iloc[-61] - 1
    price_part = _clip((r60 + 0.10) / 0.25)
    news_part = _clip(price_signal_hits / 5)
    return _clip(0.6 * price_part + 0.4 * news_part)


# --------------------------------------------------- 8 出清度
def clearing(score: float) -> float:
    return _clip(score)


# --------------------------------------------------- 9 政策强度
def policy(hits: int) -> float:
    return _clip(hits / 6)


# --------------------------------------------------- 10 新闻情绪
def sentiment_of(text: str) -> float:
    """[-1, 1] 情绪分"""
    text = str(text)
    p = sum(text.count(w) for w in POS)
    n = sum(text.count(w) for w in NEG)
    if p + n == 0:
        return 0.0
    return (p - n) / (p + n)


def board_news(news: pd.DataFrame, keywords: list[str], now,
               half_life_h: float = 36.0, limit: int = 30):
    """按板块关键词过滤新闻，做时效衰减加总

    返回 (归一化分位分 [0,1], 命中的新闻明细列表)
    """
    if news is None or news.empty or not keywords:
        return 0.5, []
    pat = "|".join(str(k) for k in keywords if k)
    if not pat:
        return 0.5, []
    mask = (news["title"].fillna("").str.contains(pat, regex=True)
            | news["content"].fillna("").str.contains(pat, regex=True))
    hit = news[mask]
    if hit.empty:
        return 0.5, []

    total = 0.0
    hits = []
    for _, r in hit.head(limit).iterrows():
        text = f"{r['title']} {r.get('content') or ''}"
        sent = sentiment_of(text)
        age_h = max((now - r["time"]).total_seconds() / 3600.0, 0.0)
        decay = 0.5 ** (age_h / half_life_h)
        total += sent * decay
        hits.append({
            "t": r["time"].strftime("%m-%d %H:%M"),
            "title": str(r["title"])[:80],
            "sent": round(sent, 2),
        })
    # 加总 ±3 视为满幅，映射到 [0,1]
    return _clip((total / 3.0 + 1.0) / 2.0), hits


def count_hits(news: pd.DataFrame, words: list[str]) -> int:
    if news is None or news.empty or not words:
        return 0
    pat = "|".join(str(w) for w in words if w)
    if not pat:
        return 0
    return int(news["title"].fillna("").str.contains(pat, regex=True).sum())


# --------------------------------------------------- 11 板块共振
def resonance(cons: pd.DataFrame) -> float:
    if cons is None or cons.empty or "涨跌幅" not in cons.columns:
        return 0.5
    p = pd.to_numeric(cons["涨跌幅"], errors="coerce").dropna()
    if len(p) < 3:
        return 0.5
    up_ratio = float((p > 0).mean())
    sd = float(p.std())
    consistent = 0.5 if not np.isfinite(sd) else _clip(1.0 - min(sd / 6.0, 1.0))
    return _clip(0.6 * up_ratio + 0.4 * consistent)


# --------------------------------------------------- 12 筹码供给罚分
def chip_supply(release_ratio: float, reduction_cnt: int,
                release_w: float = 12.0, reduction_w: float = 4.0,
                cap: float = 20.0, release_threshold: float = 0.05,
                reduction_threshold: float = 5.0) -> float:
    """release_ratio: 解禁市值/板块总市值的**原始比例**（如 0.025 = 2.5%）
    reduction_cnt: 近期减持公告条数
    阈值语义：解禁占比达到 release_threshold 即吃满 release_w 分；
             减持达 reduction_threshold 条即吃满 reduction_w 分。
    """
    a = release_w * _clip(release_ratio / release_threshold) if release_ratio > 0 else 0.0
    b = reduction_w * _clip(reduction_cnt / reduction_threshold) if reduction_cnt > 0 else 0.0
    return float(min(cap, a + b))
