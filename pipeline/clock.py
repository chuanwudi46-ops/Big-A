#!/usr/bin/env python3
"""统一「北京时间」时钟。

**为什么必须有这个模块**：GitHub Actions 的 runner 系统时区是 UTC，而本项目服务的是
A 股。直接用 `dt.datetime.now()` 会踩两个坑，而且**两个都不报错、只是结论错**：

1. 产物里的 `updated` 写的是 UTC —— 前端把它原样打印，于是北京时间 15:43 写的收盘
   数据，在手机上显示成「早上 07:43」，人以为一整天没更新（其实数据是新的）。
2. `stage_score` 的盘中判定 `now.hour < 15` 在 UTC runner 上**恒成立**
   （hour 只会是 5/6/7），连 15:35 的收盘复核也被标成「盘中快照」。

约定：`now()` 返回**naive 的北京时间墙上时间**（不带 tzinfo）。这样它与旧的
`dt.datetime.now()` 在 `strftime` / `isoformat` / `pd.Timestamp` 下行为完全一致，
替换是纯收益、不会连带改产物格式 —— 这一点是硬要求：`backtest.py` 用
`pd.Timestamp(updated)` 解析归档时间，再与 naive 的 K 线日期比较，
**若这里返回 tz-aware，那个比较会直接抛 TypeError**。
"""
from __future__ import annotations

import datetime as dt

# 固定偏移即可：中国全境单一时区且不实行夏令时，不依赖 tzdata。
CN_TZ = dt.timezone(dt.timedelta(hours=8), "CST")

# A 股：09:15 起集合竞价有连续成交，15:00 收盘。
# 只用来判断产物是「盘中快照」还是「已收盘」，**不用于判断交易日**
# （交易日由 macro.is_trading_day 依据交易日历判定）。
SESSION_START = dt.time(9, 15)
SESSION_END = dt.time(15, 0)


def now() -> dt.datetime:
    """当前北京时间（naive，可直接 strftime / isoformat）。"""
    return dt.datetime.now(dt.timezone.utc).astimezone(CN_TZ).replace(tzinfo=None)


def today() -> dt.date:
    """当前北京日期。"""
    return now().date()


def in_session(t: dt.time | None = None) -> bool:
    """是否处于 A 股盘中时段（用于 intraday 标注）。"""
    return SESSION_START <= (t or now().time()) <= SESSION_END


def is_intraday(t: dt.time | None = None) -> bool:
    """产物标注用的盘中判定：与 `in_session` 同义，单独命名以表明调用意图。"""
    return in_session(t)
