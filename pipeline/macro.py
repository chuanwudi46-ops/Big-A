"""宏观分四要素：流动性 / 量价关系 / 政策 / 情绪

对应帽子哥「股市上涨四要素模型」：
    ① 流动性   ② 量价关系   ③ 政策   ④ 情绪
"牛市的结束一定是上面收缩流动性" —— 故流动性权重最高。

重要设计：**情绪要素衡量的是「散户情绪温度」**（涨停热度 / 成交额温度 / 两融增速），
1 = 极度亢奋，0 = 极度低迷。宏观层会做逆向映射（1 − 情绪），
对应帽子哥"散户情绪极差＝无关变量，甚至反向指标"。
不要在这里直接反转，反转由 score.macro_score(inverse=True) 统一完成。

所有函数均返回 [0, 1]；任一原始数据缺失时回退到中性 0.5 而非报错。
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib

import numpy as np
import pandas as pd

import clock
import sources

ROOT = pathlib.Path(__file__).resolve().parents[1]
CACHE_DIR = ROOT / "web" / "data" / "cache"

# 权威信源（命中则提高政策信号权重）
AUTHORITATIVE = ["新华社", "人民日报", "央视", "经济日报", "光明日报",
                 "国务院", "发改委", "财政部", "证监会", "央行",
                 "人民银行", "工信部", "能源局", "统计局"]
# 政策主体（出现在新闻中才算政策信号）
POLICY_ACTORS = ["国务院", "发改委", "工信部", "财政部", "央行", "人民银行",
                 "证监会", "能源局", "住建部", "商务部", "市场监管总局",
                 "政治局", "中央", "部委", "两办"]


def _clip(x) -> float:
    try:
        x = float(x)
    except (TypeError, ValueError):
        return 0.5
    if not np.isfinite(x):
        return 0.5
    return float(min(max(x, 0.0), 1.0))


def _pick_col(df: pd.DataFrame, candidates: tuple[str, ...]) -> str | None:
    """按候选名找列（抗接口字段改名）"""
    for c in candidates:
        if c in df.columns:
            return c
    return None


def _series(df: pd.DataFrame, candidates: tuple[str, ...]) -> pd.Series:
    if df is None or df.empty:
        return pd.Series(dtype=float)
    col = _pick_col(df, candidates)
    if col is None:
        return pd.Series(dtype=float)
    return pd.to_numeric(df[col], errors="coerce").dropna()


# ==========================================================================
# 交易日历
# ==========================================================================
def trade_calendar(refresh: bool = False) -> set[str]:
    """交易日集合（YYYY-MM-DD）。本地缓存，缺失时回退「周一至周五」"""
    fp = CACHE_DIR / "trade_dates.json"
    if not refresh and fp.exists():
        try:
            data = json.loads(fp.read_text(encoding="utf-8"))
            if data.get("dates"):
                return set(data["dates"])
        except Exception:  # noqa: BLE001
            pass

    dates = sources.trade_dates()
    if dates:
        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            fp.write_text(json.dumps({"updated": clock.today().isoformat(),
                                      "dates": dates}, ensure_ascii=False),
                          encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
        return set(dates)
    return set()


def is_trading_day(d: dt.date | None = None) -> bool:
    """精确交易日判断；日历不可用时回退周一至周五"""
    d = d or clock.today()
    key = d.strftime("%Y-%m-%d")
    cal = trade_calendar()
    if cal:
        return key in cal
    return d.weekday() < 5


def last_trading_day(d: dt.date | None = None) -> dt.date:
    """最近一个交易日（含当天）"""
    d = d or clock.today()
    cal = trade_calendar()
    if not cal:
        while d.weekday() >= 5:
            d -= dt.timedelta(days=1)
        return d
    for _ in range(30):
        if d.strftime("%Y-%m-%d") in cal:
            return d
        d -= dt.timedelta(days=1)
    return d


# ==========================================================================
# ① 流动性
# ==========================================================================
def liquidity(margin_df: pd.DataFrame | None = None,
              lpr_df: pd.DataFrame | None = None,
              sf_df: pd.DataFrame | None = None) -> tuple[float, dict]:
    """流动性 = 0.45×两融趋势 + 0.30×利率方向 + 0.25×社融

    两融余额是场内最直接的资金增量代理；LPR 反映货币端松紧，
    但当利率已极低时边际效用递减（帽子哥："降息空间有限，要看财政端"）。
    """
    detail: dict = {}

    # --- 两融趋势（20 日变化率）
    s = _series(margin_df, ("融资余额", "融资融券余额", "余额"))
    if len(s) > 21:
        g = float(s.iloc[-1] / s.iloc[-21] - 1)
        margin = _clip((g + 0.02) / 0.06)          # -2% → 0，+4% → 1
        detail["margin_growth"] = round(g, 4)
    else:
        margin = 0.5
        detail["margin_growth"] = None
    detail["margin"] = round(margin, 3)

    # --- 利率方向
    lr = _series(lpr_df, ("LPR1Y", "LPR_1Y", "1年期LPR", "RATE_1"))
    if len(lr) >= 7:
        now, before = float(lr.iloc[-1]), float(lr.iloc[-7])
        if now < before:
            rate = 0.65
        elif now > before:
            rate = 0.25
        else:
            rate = 0.5
        # 已接近零下限时，继续降息的空间耗尽 → 向中性收敛
        if now < 1.6:
            rate = 0.5 + (rate - 0.5) * 0.4
        detail["lpr_1y"] = round(now, 3)
    else:
        rate = 0.5
        detail["lpr_1y"] = None
    detail["rate"] = round(rate, 3)

    # --- 社融（近 3 期 vs 前 3 期）
    sv = _series(sf_df, ("社会融资规模增量", "社会融资规模", "增量"))
    if len(sv) >= 6:
        recent, prev = float(sv.tail(3).mean()), float(sv.iloc[-6:-3].mean())
        sf = _clip((recent / prev - 1 + 0.2) / 0.4) if prev > 0 else 0.5
        detail["sf_yoy"] = round(recent / prev - 1, 4) if prev > 0 else None
    else:
        sf = 0.5
        detail["sf_yoy"] = None
    detail["social_financing"] = round(sf, 3)

    return _clip(0.45 * margin + 0.30 * rate + 0.25 * sf), detail


# ==========================================================================
# ② 量价关系
# ==========================================================================
def volume_price(idx_df: pd.DataFrame | None = None,
                 breadth: float | None = None) -> tuple[float, dict]:
    """量价关系 = 0.65×指数量价形态 + 0.35×市场广度

    量价形态判别（帽子哥："缩量调整是健康的，冲关时才要量"）：
      放量上涨 → 好；缩量调整 → 健康；放量下跌 → 差。
    """
    detail: dict = {}
    pattern = 0.5

    if idx_df is not None and not idx_df.empty and len(idx_df) > 21:
        c = pd.to_numeric(idx_df["close"], errors="coerce")
        v = pd.to_numeric(idx_df["volume"], errors="coerce")
        if c.notna().sum() > 21 and v.notna().sum() > 21:
            r5 = float(c.iloc[-1] / c.iloc[-6] - 1)
            vr5 = float(v.tail(5).mean() / v.tail(20).mean()) if v.tail(20).mean() else 1.0
            if r5 > 0 and vr5 > 1.05:
                pattern = 0.78                      # 放量上攻
            elif r5 > 0:
                pattern = 0.62                      # 缩量上涨（略谨慎）
            elif r5 < 0 and vr5 < 0.95:
                pattern = 0.60                      # 缩量调整 = 健康
            elif r5 < 0 and vr5 > 1.10:
                pattern = 0.25                      # 放量杀跌
            detail["idx_r5"] = round(r5, 4)
            detail["vol_ratio"] = round(vr5, 3)

    b = 0.5 if breadth is None else _clip(breadth)
    detail["breadth"] = round(b, 3)
    detail["pattern"] = round(pattern, 3)

    return _clip(0.65 * pattern + 0.35 * b), detail


# ==========================================================================
# ③ 政策强度
# ==========================================================================
def policy_strength(news: pd.DataFrame | None, policy_signals: dict,
                    hours: int = 72) -> tuple[float, dict]:
    """政策强度：近 72h 政策关键词加权命中

    权重规则：标题命中 ×2、正文命中 ×1；命中权威机构名 ×1.5（封顶）。
    需要关键词与「政策主体」共现才计入，避免把行业新闻误判为政策。
    """
    detail: dict = {"hits": 0, "weight": 0.0}
    if news is None or news.empty or not policy_signals:
        return 0.5, detail

    words = [w for v in policy_signals.values() for w in v if w]
    if not words:
        return 0.5, detail

    df = news.copy()
    if "time" in df.columns:
        cutoff = pd.Timestamp.now() - pd.Timedelta(hours=hours)
        df = df[df["time"] >= cutoff]
    if df.empty:
        return 0.5, detail

    pat = "|".join(words)
    actor_pat = "|".join(POLICY_ACTORS)
    weight = 0.0
    hits = 0
    for _, r in df.iterrows():
        title = str(r.get("title") or "")
        content = str(r.get("content") or "")
        text = f"{title} {content}"
        if not any(w in text for w in words):
            continue
        actors = any(a in text for a in POLICY_ACTORS)
        # 未与政策主体共现的，仅在标题命中时按半权计入
        if not actors and not any(w in title for w in words):
            continue
        w = (2.0 if any(x in title for x in words) else 1.0)
        if any(a in text for a in AUTHORITATIVE):
            w *= 1.5
        weight += w
        hits += 1

    detail["hits"] = hits
    detail["weight"] = round(weight, 1)
    return _clip(weight / 10.0), detail


# ==========================================================================
# ④ 情绪温度（逆向使用）
# ==========================================================================
def sentiment(idx_df: pd.DataFrame | None = None,
              zt_count: int | None = None,
              dt_count: int | None = None,
              margin_df: pd.DataFrame | None = None) -> tuple[float, dict]:
    """散户情绪温度 [0,1]：1 = 极度亢奋，0 = 极度低迷

    = 0.50×涨停热度 + 0.30×成交额温度 + 0.20×两融增速
    宏观层会做 (1 − 情绪) 逆向映射。
    """
    detail: dict = {}

    # --- 涨停热度
    if zt_count is not None:
        net = zt_count - (dt_count or 0)
        heat = _clip((net - 10) / 70)               # 净涨停 10 → 0，80 → 1
        detail["limit_up"] = zt_count
        detail["limit_down"] = dt_count
    else:
        heat = 0.5
        detail["limit_up"] = None
    detail["limit_heat"] = round(heat, 3)

    # --- 成交额温度（5 日 / 60 日）
    volume_heat = 0.5
    if idx_df is not None and not idx_df.empty and len(idx_df) > 61:
        v = pd.to_numeric(idx_df["volume"], errors="coerce").dropna()
        if len(v) > 61:
            long_ma = float(v.tail(60).mean())
            if long_ma > 0:
                vr = float(v.tail(5).mean() / long_ma)
                volume_heat = _clip((vr - 0.8) / 0.8)
                detail["vol_heat_ratio"] = round(vr, 3)
    detail["volume_heat"] = round(volume_heat, 3)

    # --- 两融增速
    mg = 0.5
    s = _series(margin_df, ("融资余额", "融资融券余额", "余额"))
    if len(s) > 21 and s.iloc[-21] > 0:
        g = float(s.iloc[-1] / s.iloc[-21] - 1)
        mg = _clip((g + 0.02) / 0.06)
    detail["margin_heat"] = round(mg, 3)

    return _clip(0.50 * heat + 0.30 * volume_heat + 0.20 * mg), detail
