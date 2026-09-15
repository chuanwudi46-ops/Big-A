"""评分聚合层：加权汇总 + 宏观矩阵 + 分级

核心设计（与帽子哥框架对齐）
- 板块分 S 决定"买什么 / 卖什么"（方向）
- 宏观分 M 决定"能用多少仓位"（力度）
两者独立，组成决策矩阵，避免宏观弱市时把强板块一并压死。
"""
from __future__ import annotations


def combine(f: dict, w: dict) -> float:
    """加权汇总 -> [0, 100]"""
    tw = sum(w[k] for k in w) or 1.0
    return 100.0 * sum(w[k] * f.get(k, 0.5) for k in w) / tw


def finalize(base: float, penalty: float, blacklisted: bool,
             cap: float = 45.0, blacklist_enabled: bool = True) -> float:
    """扣罚分 + 负面清单压制 -> [0, 100]"""
    s = base - penalty
    if blacklisted and blacklist_enabled:
        s = min(s, cap)
    return round(max(0.0, min(100.0, s)), 1)


def grade(s: float, th: dict) -> tuple[str, str]:
    """分级 + 动作建议"""
    if s >= th["strong"]:
        return "主升共振", "逢低吸：48次补仓法网格分批，单次 5%"
    if s >= th["watch"]:
        return "进入观察", "小仓试探，跌破预设支撑才加"
    if s >= th["neutral"]:
        return "中性", "卧倒，不动"
    if s >= th["weak"]:
        return "转弱", "逢高减：分批累计 30%"
    return "回避", "空仓，等解禁出清后的黄金坑"


def macro_score(liq: float, vp: float, pol: float, sent: float,
                w: dict, inverse: bool = True) -> float:
    """宏观分：股市上涨四要素（流动性/量价/政策/情绪）"""
    s = (1.0 - sent) if inverse else sent
    return round(100.0 * (w["liquidity"] * liq
                          + w["volume_price"] * vp
                          + w["policy"] * pol
                          + w["sentiment"] * s), 1)


def macro_zone(m: float, gate_attack: float = 70.0, gate_neutral: float = 40.0) -> str:
    if m >= gate_attack:
        return "进攻"
    if m >= gate_neutral:
        return "中性"
    return "防守"


def action_matrix(s: float, m: float,
                  gate_attack: float = 70.0, gate_neutral: float = 40.0) -> str:
    """板块分 × 宏观分 -> 最终动作（决策矩阵）"""
    z = 2 if m >= gate_attack else (1 if m >= gate_neutral else 0)
    if s >= 75:
        return ["只观察不动手", "小仓试探（≤半仓）", "按网格分批建仓"][z]
    if s >= 60:
        return ["观察", "观察", "观察，跌破支撑再加"][z]
    if s >= 45:
        return ["卧倒", "卧倒", "卧倒"][z]
    if s >= 30:
        return ["逢高减（加速）", "逢高减", "逢高减 30%"][z]
    return ["回避", "回避", "回避"][z]
