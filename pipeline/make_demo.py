#!/usr/bin/env python3
"""离线演示数据生成器

用途：在没有网络 / 无法访问免费接口时，生成一套**合成数据**用于前端预览与调试。

⚠️ 警告：生成的数据是**随机合成的假数据**，仅用于验证界面与流程，
   不可用于任何投资判断。真实数据请运行 pipeline/main.py。

用法:
    python pipeline/make_demo.py                # 仅生成前端展示数据（web/data/）
    python pipeline/make_demo.py --simulate 60  # 额外生成 60 天仿真K线与历史快照
                                                # 用于离线验证 backtest.py 全链路
    python pipeline/make_demo.py --out /tmp/demo
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

ROOT = pathlib.Path(__file__).resolve().parents[1]
CFG = pathlib.Path(__file__).resolve().parent / "config"

FACTORS = ["momentum", "trend", "volume_price", "turnover", "fund_flow",
           "valuation", "cycle", "clearing", "policy", "news", "resonance"]
W = {"momentum": 0.10, "trend": 0.10, "volume_price": 0.08, "turnover": 0.08,
     "fund_flow": 0.10, "valuation": 0.10, "cycle": 0.08, "clearing": 0.06,
     "policy": 0.06, "news": 0.12, "resonance": 0.12}

DEMO_NEWS = [
    ("半导体设备订单超预期 国产替代提速", 0.8),
    ("发改委部署反内卷 严控新增产能", 0.6),
    ("银行板块息差承压 净息差继续收窄", -0.5),
    ("锂电材料涨价 部分型号供不应求", 0.7),
    ("地产销售面积同比下滑 房企资金承压", -0.6),
    ("白酒批价走弱 渠道库存偏高", -0.7),
    ("电网投资加码 特高压招标提速", 0.6),
    ("券商两融余额回升 交投活跃度提升", 0.4),
]


def grade(s: float):
    if s >= 75:
        return "主升共振", "逢低吸：48次补仓法网格分批，单次 5%"
    if s >= 60:
        return "进入观察", "小仓试探，跌破预设支撑才加"
    if s >= 45:
        return "中性", "卧倒，不动"
    if s >= 30:
        return "转弱", "逢高减：分批累计 30%"
    return "回避", "空仓，等解禁出清后的黄金坑"


def load_boards() -> tuple[list[dict], list[str]]:
    meta_fp = CFG / "sector_meta.json"
    seed_fp = CFG / "keywords_seed.json"
    if meta_fp.exists():
        meta = json.loads(meta_fp.read_text(encoding="utf-8"))
        return meta["boards"], meta.get("blacklist", [])
    seed = json.loads(seed_fp.read_text(encoding="utf-8"))
    # keywords_seed 的板块词典已按层级分组（boards_by_level），
    # 这里优先取二级、再退回一级，最后才用老的扁平 boards 键
    by_level = seed.get("boards_by_level") or {}
    kw_dict = (by_level.get("申万二级行业") or by_level.get("申万一级行业")
               or seed.get("boards") or {})
    boards = [{"code": f"BK{i:04d}", "name": n, "keywords": k,
               "clearing_stage": "未出清", "clearing_score": 0.5}
              for i, (n, k) in enumerate(kw_dict.items(), start=1)]
    return boards, seed.get("blacklist", [])


# --------------------------------------------------------------------------
# 仿真模式：生成 K 线缓存 + 历史评分快照，使 backtest.py 可离线跑通
# --------------------------------------------------------------------------
def simulate(out: pathlib.Path, boards: list[dict], days: int = 60,
             warmup_gap: int = 10, seed: int = 20260911) -> None:
    rng = np.random.default_rng(seed)
    cache = out / "cache"
    cache.mkdir(parents=True, exist_ok=True)

    dates = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=days)

    # 每期的"真实"评分（平滑后作为信号）
    score_hist: dict[str, np.ndarray] = {}
    for b in boards:
        s = pd.Series(rng.uniform(20, 90, len(dates)), index=dates)
        score_hist[b["code"]] = s.rolling(5, min_periods=1).mean().to_numpy()

    # 价格路径：漂移项由当期评分驱动 → 评分与未来收益正相关
    for b in boards:
        s = score_hist[b["code"]]
        px = [1000.0]
        for i in range(len(dates)):
            drift = (s[i] - 55.0) / 55.0 * 0.02
            px.append(px[-1] * (1 + drift + float(rng.normal(0, 0.007))))
        close = np.array(px[1:], dtype=float)
        df = pd.DataFrame({
            "date": dates,
            "open": close * (1 + rng.normal(0, 0.003, len(dates))),
            "close": close,
            "high": close * (1 + np.abs(rng.normal(0, 0.006, len(dates)))),
            "low": close * (1 - np.abs(rng.normal(0, 0.006, len(dates)))),
            "volume": rng.uniform(1e6, 5e6, len(dates)),
            "amount": rng.uniform(1e9, 9e9, len(dates)),
            "pct": pd.Series(close).pct_change().fillna(0).to_numpy() * 100,
            "turnover": np.abs(rng.normal(2.0, 0.6, len(dates))),
        })
        df.to_parquet(cache / f"{b['code']}.parquet", index=False)

    # 历史评分快照（留出末尾 warmup_gap 天，保证有未来收益可算）
    n_snap = max(len(dates) - warmup_gap, 1)
    for i in range(n_snap):
        d = dates[i]
        recs = []
        for b in boards:
            sc = round(float(score_hist[b["code"]][i]), 1)
            label, hint = grade(sc)
            recs.append({"code": b["code"], "name": b["name"], "score": sc,
                         "label": label, "action_hint": hint,
                         "factors": {k: round(float(rng.uniform(0.2, 0.9)), 3) for k in FACTORS},
                         "penalty": 0.0})
        fp = out / "archive" / d.strftime("%Y") / d.strftime("%m")
        fp.mkdir(parents=True, exist_ok=True)
        (fp / f"{d.strftime('%Y%m%d')}_1530.json").write_text(
            json.dumps({"updated": d.strftime("%Y-%m-%dT15:30:00"),
                        "intraday": False, "macro_score": 55.0, "boards": recs,
                        # demo 标记必须保留：backtest.load_snapshots 会据此
                        # 剔除合成快照，防止仿真数据污染真实回测结论
                        "demo": True},
                       ensure_ascii=False), encoding="utf-8")

    print(f"[simulate] K线缓存 {len(boards)} 个板块 × {len(dates)} 交易日")
    print(f"[simulate] 历史评分快照 {n_snap} 期 -> {out / 'archive'}")
    print("[simulate] 可运行: python pipeline/backtest.py 验证全链路")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "web" / "data"))
    ap.add_argument("--seed", type=int, default=20260911)
    ap.add_argument("--simulate", type=int, default=0, metavar="DAYS",
                    help="额外生成 N 天仿真K线缓存与历史快照，用于离线回测验证")
    ap.add_argument("--rebuild-map", action="store_true",
                    help="同时生成合成的个股-板块映射缓存")
    args = ap.parse_args()

    boards, blacklist = load_boards()
    rng = np.random.default_rng(args.seed)
    out = pathlib.Path(args.out)
    (out / "sectors").mkdir(parents=True, exist_ok=True)

    now = dt.datetime.now()
    scored = []
    for b in boards:
        f = {k: float(rng.uniform(0.15, 0.9)) for k in FACTORS}
        base = 100 * sum(W[k] * f[k] for k in FACTORS)
        pen = float(rng.uniform(0, 12))
        black = any(k in b["name"] or k in b.get("keywords", []) for k in blacklist)
        s = base - pen
        if black:
            s = min(s, 45.0)
        s = round(max(0.0, min(100.0, s)), 1)
        label, hint = grade(s)

        news = []
        for i in range(int(rng.integers(2, 6))):
            t, sent = DEMO_NEWS[int(rng.integers(0, len(DEMO_NEWS)))]
            news.append({
                "t": (now - dt.timedelta(hours=int(rng.integers(1, 40)))).strftime("%m-%d %H:%M"),
                "title": t,
                "sent": sent,
            })
        news.sort(key=lambda x: x["t"], reverse=True)

        # 合成区间位置：结构必须与 swing.swing_stats 的真实产物完全一致，
        # 否则「回撤 / 涨幅」视图在演示模式下会渲染成空白
        sw_windows = {}
        for n, lb in ((20, "20 个交易日"), (60, "60 个交易日"), (250, "近一年")):
            hi = float(rng.uniform(1200, 4000))
            lo = hi * float(rng.uniform(0.55, 0.82))
            cur = lo + (hi - lo) * float(rng.uniform(0.08, 0.92))
            sw_windows[str(n)] = {
                "label": lb, "bars": n,
                "high": round(hi, 2), "low": round(lo, 2),
                "high_date": (now - dt.timedelta(days=int(rng.integers(3, n)))).strftime("%Y-%m-%d"),
                "low_date": (now - dt.timedelta(days=int(rng.integers(3, n)))).strftime("%Y-%m-%d"),
                "days_since_high": int(rng.integers(1, n)),
                "days_since_low": int(rng.integers(1, n)),
                "drawdown": round(cur / hi - 1, 4),
                "rebound": round(cur / lo - 1, 4),
                "position": round((cur - lo) / (hi - lo), 3),
                "at_high": False, "at_low": False,
            }
        sw = {"close": round(cur, 2), "date": now.strftime("%Y-%m-%d"),
              "windows": sw_windows}

        rec = {
            "code": b["code"], "name": b["name"], "score": s,
            "base": round(base, 1), "label": label, "action_hint": hint,
            "blacklisted": black,
            "clearing_stage": b.get("clearing_stage", "未出清"),
            "factors": {k: round(v, 3) for k, v in f.items()},
            "penalty": round(pen, 2),
            "penalty_detail": {
                "release_ratio": round(float(rng.uniform(0, 0.04)), 5),
                "release_mv_yi": round(float(rng.uniform(0, 300)), 1),
                "reduction_cnt": int(rng.integers(0, 4)),
            },
            "valuation_source": "price_proxy",
            "swing": sw,
            "intraday": now.hour < 15,
            "updated": now.strftime("%Y-%m-%d %H:%M:%S"),
            "news": news,
        }
        scored.append(rec)
        (out / "sectors" / f"{b['code']}.json").write_text(
            json.dumps(rec, ensure_ascii=False), encoding="utf-8")

    m = round(float(rng.uniform(45, 78)), 1)
    (out / "meta.json").write_text(json.dumps({
        "updated": now.strftime("%Y-%m-%d %H:%M:%S"),
        "intraday": now.hour < 15,
        "macro_score": m,
        "macro_zone": "进攻" if m >= 70 else ("中性" if m >= 40 else "防守"),
        "count": len(scored),
        "demo": True,
        "macro_detail": {
            "liquidity": {"score": round(float(rng.uniform(0.35, 0.8)), 3),
                          "margin_growth": round(float(rng.uniform(-0.01, 0.03)), 4),
                          "lpr_1y": 2.9,
                          "social_financing": round(float(rng.uniform(0.3, 0.8)), 3)},
            "volume_price": {"score": round(float(rng.uniform(0.35, 0.8)), 3),
                             "pattern": round(float(rng.uniform(0.3, 0.8)), 3),
                             "breadth": round(float(rng.uniform(0.3, 0.7)), 3)},
            "policy": {"score": round(float(rng.uniform(0.3, 0.8)), 3),
                       "hits": int(rng.integers(1, 8)),
                       "weight": round(float(rng.uniform(2, 10)), 1)},
            "sentiment": {"score": round(float(rng.uniform(0.25, 0.8)), 3),
                          "limit_up": int(rng.integers(20, 90)),
                          "limit_down": int(rng.integers(0, 20)),
                          "inverted": True},
        },
    }, ensure_ascii=False), encoding="utf-8")

    scored.sort(key=lambda x: -x["score"])
    (out / "index.json").write_text(json.dumps([{
        "code": r["code"], "name": r["name"], "score": r["score"],
        "label": r["label"], "action_hint": r["action_hint"],
        "parent": r.get("parent", ""),
        # 与 main.py 一致：带上三窗口精简版，前端才能切 20/60/250 日
        "swing": {k: {"dd": w["drawdown"], "rb": w["rebound"],
                      "pos": w["position"], "dsh": w["days_since_high"]}
                  for k, w in (r.get("swing") or {}).get("windows", {}).items()},
    } for r in scored], ensure_ascii=False), encoding="utf-8")

    # 合成大盘关键位：结构与 levels.build 的真实产物一致，让演示界面能完整渲染
    def _demo_period(key: str, label: str, bars: int, close: float) -> dict:
        return {
            "key": key, "label": label, "bars": bars,
            "from": "2024-01-02", "to": now.strftime("%Y-%m-%d"),
            "range_high": round(close * 1.09, 2), "range_low": round(close * 0.88, 2),
            "position": 0.62, "position_text": "偏上",
            "resistance": [{"price": round(close * 1.03, 2), "gap_pct": 3.0,
                            "date": "2026-06-01", "near": False}],
            "support": [{"price": round(close * 0.98, 2), "gap_pct": -2.0,
                         "date": "2026-07-01", "near": False}],
            "at_range_high": False, "at_range_low": False,
        }

    close = 3000.0
    periods = [_demo_period("week", "周线", 104, close),
               _demo_period("month", "月线", 60, close),
               _demo_period("year", "年线", 10, close)]
    (out / "levels.json").write_text(json.dumps({
        "updated": now.strftime("%Y-%m-%d %H:%M:%S"),
        "indices": [{
            "code": "sh000001", "name": "上证指数",
            "date": now.strftime("%Y-%m-%d"), "close": close, "pct": 0.5,
            "periods": periods,
            "summary": {
                "nearest_resistance": {"period": "周线", **periods[0]["resistance"][0]},
                "nearest_support": {"period": "周线", **periods[0]["support"][0]},
                "text": f"上证指数 {close:.2f}：（这行是合成演示数据，无实际含义）",
                "notes": [],
            },
        }],
    }, ensure_ascii=False), encoding="utf-8")

    print(f"[demo] 已生成 {len(scored)} 个板块的**合成数据** -> {out}")
    print("[demo] ⚠️ 这是假数据，仅用于界面预览，请勿据此做任何投资判断")

    if args.rebuild_map:
        # 合成一份个股-板块映射，供筹码维度离线调试
        rows = []
        for bi, b in enumerate(boards):
            for k in range(6):
                rows.append({"stock_code": f"{600000 + bi * 10 + k:06d}",
                             "stock_name": f"{b['name']}{k}",
                             "board_code": b["code"], "board_name": b["name"]})
        cache = out / "cache"
        cache.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(rows).to_parquet(cache / "stock_board_map.parquet", index=False)
        print(f"[demo] 合成映射表 {len(rows)} 条 -> {cache / 'stock_board_map.parquet'}")

    if args.simulate > 0:
        simulate(out, boards, days=args.simulate, seed=args.seed)

    return 0


if __name__ == "__main__":
    sys.exit(main())
