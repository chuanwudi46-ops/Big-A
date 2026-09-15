#!/usr/bin/env python3
"""炒股工作台 · 数据管道入口

用法:
    python pipeline/main.py --stage warmup   # 13:40 预热：历史K线 / 映射表 / 宏观原始数据 / 新闻
    python pipeline/main.py --stage score    # 14:00 主评分：盘中快照 -> 12维 -> 发布
    python pipeline/main.py --stage close    # 15:30 收盘复核：重算并归档
    python pipeline/main.py --stage score --force      # 忽略交易日判断
    python pipeline/main.py --stage warmup --rebuild-map  # 强制重建个股-板块映射

产物统一写入 web/data/，随 GitHub Pages 一起发布。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import sys

import pandas as pd
import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import chips                 # noqa: E402
import factors as F          # noqa: E402
import macro as M            # noqa: E402
import score as S            # noqa: E402
import sources               # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]
CFG = pathlib.Path(__file__).resolve().parent / "config"
DATA = ROOT / "web" / "data"
CACHE = DATA / "cache"


# --------------------------------------------------------------------- utils
def load_cfg() -> tuple[dict, dict]:
    w = yaml.safe_load((CFG / "weights.yaml").read_text(encoding="utf-8"))
    meta_fp = CFG / "sector_meta.json"
    if not meta_fp.exists():
        # 首次运行（含 CI 首次构建）自动生成，免去「必须先手动跑一次」的前置步骤
        print(f"[init] 未找到 {meta_fp.name}，自动拉取行业板块清单 …")
        try:
            import sync_boards                    # 同目录模块
            meta_fp = sync_boards.ensure_sector_meta()
        except Exception as e:                    # noqa: BLE001
            raise SystemExit(
                f"[error] 自动生成 {meta_fp.name} 失败：{e}\n"
                "        请检查网络后重试，或手动运行: python pipeline/sync_boards.py"
            )
    meta = json.loads(meta_fp.read_text(encoding="utf-8"))
    return w, meta


def _dump(fp: pathlib.Path, obj) -> None:
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")


def _read_parquet(name: str, required: bool = False) -> pd.DataFrame:
    fp = DATA / name
    if not fp.exists():
        if required:
            print(f"[warn] 缺少 {fp}，建议先运行 --stage warmup")
        return pd.DataFrame()
    try:
        return pd.read_parquet(fp)
    except Exception as e:  # noqa: BLE001
        print(f"[warn] read {name}: {e}")
        return pd.DataFrame()


def _write_parquet(name: str, df: pd.DataFrame) -> None:
    if df is None or df.empty:
        return
    fp = DATA / name
    fp.parent.mkdir(parents=True, exist_ok=True)
    try:
        df.to_parquet(fp, index=False)
        print(f"[warmup] {name}: {len(df)} 行")
    except Exception as e:  # noqa: BLE001
        print(f"[warn] write {name}: {e}")


# ---------------------------------------------------------------- warmup
def stage_warmup(w: dict, meta: dict, rebuild_map: bool = False) -> None:
    """预热：抓历史 K 线（增量）、板块映射、宏观原始数据、新闻、解禁/减持"""
    CACHE.mkdir(parents=True, exist_ok=True)
    end = dt.date.today().strftime("%Y%m%d")
    start = (dt.date.today() - dt.timedelta(days=560)).strftime("%Y%m%d")

    # ---- 板块历史 K 线（增量）
    ok = 0
    for b in meta["boards"]:
        fp = CACHE / f"{b['code']}.parquet"
        try:
            if fp.exists():
                old = pd.read_parquet(fp)
                last = pd.to_datetime(old["date"]).max()
                beg = (last + pd.Timedelta(days=1)).strftime("%Y%m%d")
                if beg > end:
                    ok += 1
                    continue
                new = sources.board_hist(b["name"], beg, end, b["code"])
                df = pd.concat([old, new], ignore_index=True).drop_duplicates("date")
            else:
                df = sources.board_hist(b["name"], start, end, b["code"])
            df.to_parquet(fp, index=False)
            ok += 1
        except Exception as e:  # noqa: BLE001
            print(f"[warn] hist {b['name']}: {e}")
    print(f"[warmup] boards {ok}/{len(meta['boards'])}")

    # ---- 新闻
    _write_parquet("news.parquet", sources.fetch_news())

    # ---- 筹码原始数据
    _write_parquet("release.parquet", sources.release_calendar(
        end, (dt.date.today() + dt.timedelta(days=180)).strftime("%Y%m%d")))
    _write_parquet("share_change.parquet", sources.share_change())

    # ---- 宏观原始数据
    _write_parquet("margin.parquet", sources.margin_balance())
    _write_parquet("lpr.parquet", sources.lpr())
    _write_parquet("sf.parquet", sources.social_financing())

    # ---- 基准指数（沪深300 / 上证，用于量价与情绪）
    try:
        idx = sources.index_daily("sh000300")
        _write_parquet("index_hs300.parquet", idx)
    except Exception as e:  # noqa: BLE001
        print(f"[warn] hs300: {e}")
    try:
        sh = sources.index_daily("sh000001")
        _write_parquet("index_sh.parquet", sh)
    except Exception as e:  # noqa: BLE001
        print(f"[warn] sh000001: {e}")

    # ---- 个股 → 板块 映射（筹码归属基础）
    chips.build_map(meta["boards"], force=rebuild_map)

    # ---- 交易日历
    M.trade_calendar(refresh=True)


# ----------------------------------------------------------------- score
def _load_macro_inputs() -> dict:
    return {
        "margin": _read_parquet("margin.parquet"),
        "lpr": _read_parquet("lpr.parquet"),
        "sf": _read_parquet("sf.parquet"),
        "idx": _read_parquet("index_hs300.parquet"),
        "news": _read_parquet("news.parquet"),
    }


def stage_score(w: dict, meta: dict) -> None:
    now = dt.datetime.now()
    intraday = bool(w.get("intraday", {}).get("mark", True)) and now.hour < 15

    try:
        snap = sources.board_snapshot()
    except Exception as e:  # noqa: BLE001
        raise SystemExit(f"[error] 板块快照获取失败，保留上一版产物: {e}")
    sidx = {str(r["code"]): r for _, r in snap.iterrows()}
    mktcap = {str(r["code"]): r.get("mktcap") for _, r in snap.iterrows()}

    mi = _load_macro_inputs()
    news = mi["news"]
    rel = _read_parquet("release.parquet")
    shr = _read_parquet("share_change.parquet")
    bmap = chips.load_map()

    # ---- 基准：沪深300 近 20 日
    bench_r20 = None
    try:
        idx = mi["idx"] if not mi["idx"].empty else sources.index_daily("sh000300")
        bench_r20 = float(idx["close"].iloc[-1] / idx["close"].iloc[-21] - 1)
    except Exception as e:  # noqa: BLE001
        print(f"[warn] benchmark: {e}")
        idx = pd.DataFrame()

    # ---- 筹码压力（按板块预聚合一次）
    rel_raw = chips.release_pressure(rel, bmap, w["penalty"].get("horizon_days", 30))
    red_cnt = chips.reduction_counts(shr, bmap, days=30)

    policy_words = [x for v in meta.get("policy_signals", {}).values() for x in v]
    price_words = meta.get("price_signals", [])
    blacklist = meta.get("blacklist", [])
    blk_cap = float(w.get("blacklist", {}).get("cap", 45.0))
    blk_on = bool(w.get("blacklist", {}).get("enabled", True))
    pen_cfg = w["penalty"]

    # ---- 市场广度
    up = float(pd.to_numeric(snap.get("up"), errors="coerce").fillna(0).sum()) \
        if "up" in snap.columns else 0.0
    dn = float(pd.to_numeric(snap.get("down"), errors="coerce").fillna(0).sum()) \
        if "down" in snap.columns else 0.0
    breadth = 0.5 if up + dn == 0 else up / (up + dn)

    scored: list[dict] = []
    for b in meta["boards"]:
        fp = CACHE / f"{b['code']}.parquet"
        if not fp.exists():
            print(f"[skip] {b['name']}: 无缓存K线，请先跑 warmup")
            continue
        df = pd.read_parquet(fp)
        if len(df) < 61:
            print(f"[skip] {b['name']}: K线不足 61 根")
            continue

        row = sidx.get(str(b["code"]), {})
        news_norm, news_hits = F.board_news(news, b.get("keywords", []), now)
        val_score, val_src = F.valuation(df)

        f = {
            "momentum": F.momentum(df, bench_r20),
            "trend": F.trend(df),
            "volume_price": F.volume_price(df),
            "turnover": F.turnover(df),
            "fund_flow": F.fund_flow(row.get("main_inflow_pct")),
            "valuation": val_score,
            "cycle": F.cycle(df, F.count_hits(news, price_words)),
            "clearing": F.clearing(b.get("clearing_score", 0.5)),
            "policy": F.policy(F.count_hits(news, policy_words)),
            "news": news_norm,
            "resonance": F.resonance(sources.board_cons(b["name"])),
        }

        base = S.combine(f, w["weights"])

        # ---- 第 12 维：筹码供给罚分（精确归属）
        # release_ratio 返回原始比例（如 2.5% = 0.025），归一化在 chip_supply 内完成
        rr, rel_detail = chips.release_ratio(b["code"], mktcap.get(str(b["code"])), rel_raw)
        rcnt = int(red_cnt.get(str(b["code"]), 0))
        penalty = F.chip_supply(rr, rcnt,
                                pen_cfg["release_weight"],
                                pen_cfg["reduction_weight"],
                                pen_cfg["chip_supply_max"],
                                pen_cfg.get("release_threshold", 0.05),
                                pen_cfg.get("reduction_threshold", 5.0))

        black = any((k in b["name"]) or (k in b.get("keywords", [])) for k in blacklist)
        final = S.finalize(base, penalty, black, blk_cap, blk_on)
        label, hint = S.grade(final, w["grade"])

        rec = {
            "code": b["code"],
            "name": b["name"],
            "score": final,
            "base": round(base, 1),
            "label": label,
            "action_hint": hint,
            "blacklisted": black,
            "clearing_stage": b.get("clearing_stage", "未出清"),
            "factors": {k: round(v, 3) for k, v in f.items()},
            "penalty": round(penalty, 2),
            "penalty_detail": {"release_ratio": rel_detail.get("ratio"),
                               "release_mv_yi": rel_detail.get("release_mv_yi"),
                               "reduction_cnt": rcnt},
            "valuation_source": val_src,
            "intraday": intraday,
            "updated": now.strftime("%Y-%m-%d %H:%M:%S"),
            "news": news_hits[:20],
        }
        scored.append(rec)
        _dump(DATA / "sectors" / f"{b['code']}.json", rec)

    if not scored:
        raise SystemExit("[error] 无任何板块完成评分，保留上一版产物")

    # ---- 宏观分四要素
    liq, liq_d = M.liquidity(mi["margin"], mi["lpr"], mi["sf"])
    vp, vp_d = M.volume_price(idx, breadth)
    pol, pol_d = M.policy_strength(news, meta.get("policy_signals", {}))
    zt = dtz = None
    try:
        zt, dtz = sources.limit_up_pool(M.last_trading_day().strftime("%Y%m%d"))
    except Exception as e:  # noqa: BLE001
        print(f"[warn] limit pool: {e}")
    sent, sent_d = M.sentiment(idx, zt, dtz, mi["margin"])

    mw = w["macro"]["weights"]
    m = S.macro_score(liq, vp, pol, sent, mw, w["macro"]["sentiment_inverse"])
    ga, gn = w["macro"]["gate_attack"], w["macro"]["gate_neutral"]

    _dump(DATA / "meta.json", {
        "updated": now.strftime("%Y-%m-%d %H:%M:%S"),
        "intraday": intraday,
        "macro_score": m,
        "macro_zone": S.macro_zone(m, ga, gn),
        "count": len(scored),
        "bench_r20": None if bench_r20 is None else round(bench_r20, 4),
        "macro_detail": {
            "liquidity": {"score": round(liq, 3), **liq_d},
            "volume_price": {"score": round(vp, 3), **vp_d},
            "policy": {"score": round(pol, 3), **pol_d},
            "sentiment": {"score": round(sent, 3), **sent_d,
                          "inverted": bool(w["macro"]["sentiment_inverse"])},
        },
    })

    scored.sort(key=lambda x: -x["score"])
    _dump(DATA / "index.json", [{
        "code": r["code"], "name": r["name"], "score": r["score"],
        "label": r["label"],
        "action_hint": S.action_matrix(r["score"], m, ga, gn),
    } for r in scored])

    arc = DATA / "archive" / now.strftime("%Y/%m")
    _dump(arc / f"{now.strftime('%Y%m%d_%H%M')}.json", {
        "updated": now.isoformat(timespec="seconds"),
        "intraday": intraday,
        "macro_score": m,
        "macro_detail": {"liquidity": round(liq, 3), "volume_price": round(vp, 3),
                         "policy": round(pol, 3), "sentiment": round(sent, 3)},
        "boards": scored,
        # 真实数据显式标记，与 make_demo.py 的 demo=true 对应；
        # backtest.load_snapshots 据此剔除合成快照
        "demo": False,
    })
    print(f"[score] {len(scored)} boards | macro={m} ({S.macro_zone(m, ga, gn)}) "
          f"| 流动性{liq:.2f} 量价{vp:.2f} 政策{pol:.2f} 情绪{sent:.2f} | intraday={intraday}")


# ----------------------------------------------------------------- close
def stage_close(w: dict, meta: dict, rebuild_map: bool = False) -> None:
    """收盘复核：先补齐当日收盘 K 线，再用收盘价重算归档"""
    stage_warmup(w, meta, rebuild_map=rebuild_map)
    stage_score(w, meta)


# ------------------------------------------------------------------ main
STAGES = {"warmup": stage_warmup, "score": stage_score, "close": stage_close}


def main() -> int:
    ap = argparse.ArgumentParser(description="炒股工作台数据管道")
    ap.add_argument("--stage", required=True, choices=list(STAGES))
    ap.add_argument("--force", action="store_true", help="忽略交易日判断")
    ap.add_argument("--rebuild-map", action="store_true",
                    help="强制重建个股-板块映射表")
    args = ap.parse_args()

    if not args.force and not M.is_trading_day():
        print("非交易日（依据交易日历），跳过")
        return 0

    w, meta = load_cfg()
    if args.stage in ("warmup", "close"):
        STAGES[args.stage](w, meta, rebuild_map=args.rebuild_map)
    else:
        STAGES[args.stage](w, meta)
    return 0


if __name__ == "__main__":
    sys.exit(main())
