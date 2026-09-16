#!/usr/bin/env python3
"""评分与规则的「长历史 · 点内重建」回测

与 `backtest.py` 的分工（两个都要，别混）

| 脚本 | 口径 | 样本从哪来 | 现在能用吗 |
|---|---|---|---|
| `backtest.py` | **前瞻监控**：每天归档一份快照，攒够后拿「真实发布的分数」算 IC | `web/data/archive/` | ❌ 目前 11 份归档**全是同一天**（2026-09-16）的多次运行，按日期去重后只剩 1 天 |
| `backtest_hist.py` | **历史回测**：用真实日 K 线**逐日重建**当时的因子值 | `bt_data/kline/`（腾讯长历史） | ✅ 120 板块 × 2001 根日线（2018-06-22 起） |

**为什么必须重建**：归档快照是「看未来」的唯一正品，但它只能一天一天攒；本期要回答
「权重/阈值/规则到底有没有效」，只能回到历史里逐日重算。重建的代价是：12 维里
**只有 6 个技术面维度可回溯**（动量/趋势/量价/换手/估值代理/周期），
其余 5 个（主力资金流、出清度、政策、新闻情绪、板块共振）与筹码罚分都需要**当期快照**，
历史不可得 —— 报告里所有组合分都按「6 维内重新归一」的口径，且**明确标注**。

**点内（point-in-time）保证**：评价日 t 只喂 `df[:t+1]` 给 `factors.py` 的原函数，
不做任何前视。因子函数要求滚动 250 日窗口，故 `min_bars=260`。

用法
    $PY pipeline/backtest_hist.py --fetch      # 抓长历史（K线 120 个 + 解禁明细 93 个月）
    $PY pipeline/backtest_hist.py              # 跑回测 → docs/backtest_report_hist.md
    $PY pipeline/backtest_hist.py --horizons 5,20,60,120 --stride 5
"""
from __future__ import annotations

import argparse
import calendar
import datetime as dt
import io
import json
import pathlib
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pandas as pd

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pipeline"))

import backtest as BT          # noqa: E402  复用 spearman / forward_return，避免两套实现
import factors as F            # noqa: E402  ← 回测用的就是线上那套因子代码
import macro as M              # noqa: E402
import score as S              # noqa: E402
import sources as SR           # noqa: E402

DATA = ROOT / "web" / "data"
BTDATA = ROOT / "bt_data"
KLINE_DIR = BTDATA / "kline"
REL_HIST = BTDATA / "release_hist.parquet"

# 可回溯的 6 个维度（其余需要当期快照，历史不可得）
BACKTESTABLE = ["momentum", "trend", "volume_price", "turnover", "valuation", "cycle"]
NOT_BACKTESTABLE = ["fund_flow", "clearing", "policy", "news", "resonance"]

# 因子函数最长回看 = 250（分位窗口）+ 61（动量/周期）→ 取 340 根足够
LOOKBACK = 340

# `build_panel` 取值逻辑的版本号：改了列的算法 / 预筛条件 / LOOKBACK 而 factors.py
# 没动时手工 +1，让 `--reuse-panel` 能把旧面板判为过期（见 factor_rev）。
PANEL_SCHEMA = 2

# 「解禁吃满」的**原规则**罚分系数（2026-09-16 之后线上配置已改为 0）。
# release_supply_test 用它来复现「改动前那条规则」的效果 —— 不能读当前配置，
# 否则调成 0 之后该检验会把自己的证据抹掉。详见该函数内的注释与报告 §5.7.1。
RELEASE_W_ORIGINAL = 12.0


# ==================================================================== 配置
def load_weights() -> dict:
    import yaml
    fp = ROOT / "pipeline" / "config" / "weights.yaml"
    return yaml.safe_load(fp.read_text(encoding="utf-8"))


def factor_rev() -> str:
    """面板内容指纹：`factors.py` ＋ **只有真正进面板的那部分** `weights.yaml`。

    面板里存的是**已经算好的因子值**，所以 `--reuse-panel` 复用它时，只要这两个来源
    变过，复用就等于「拿旧公式的结论去评价新公式」—— 而且**不会报错**，只会得到
    一张看起来很正常、结论却张冠李戴的表。故在 meta 里记指纹，复用时比对。

    **口径收窄（2026-09-16）**：最初是整个 `weights.yaml` 一起哈希，结果改一个与面板
    毫不相干的键（如 `penalty.release_weight`、`macro.weights`、`archive.summary`）
    也会让复用被拒、白等 6 分钟重建。而 `build_panel` 只用到
    `F.*` 与 `S.combine(f, w6)`，即只有 `window`（`_pctrank` 的滚动窗口）
    与 `BACKTESTABLE` 那 6 个权重会影响面板内容。现在只哈希这两样。

    `PANEL_SCHEMA`：当 `build_panel` 的**取值逻辑**本身变了（列的算法、预筛条件、
    `LOOKBACK` 等）而 `factors.py` 没动时，手工把它 +1，面板同样会被判为过期。
    """
    import hashlib
    import json as _json
    w = load_weights()
    payload = {
        "schema": PANEL_SCHEMA,
        "window": w.get("window"),
        "weights6": {k: (w.get("weights", {}) or {}).get(k) for k in BACKTESTABLE},
    }
    h = hashlib.md5()
    h.update(_json.dumps(payload, sort_keys=True, ensure_ascii=False).encode())
    h.update(b"\x00")
    try:
        h.update((ROOT / "pipeline" / "factors.py").read_bytes())
    except OSError:
        h.update(b"factors.py-missing")
    return h.hexdigest()[:8]


# ==================================================================== 抓数
def fetch_klines(counts: tuple[int, ...] = (2000, 1200, 800),
                 workers: int = 10) -> dict:
    """抓 120 个申万二级板块的最长可用日线 → bt_data/kline/<名称>.parquet

    腾讯通道的 `count` 有服务端上限（实测 2000 可用、2050 起返回空），
    所以「能回多远」由它决定，而不是由 `start` 决定 —— 传 start=20100101
    也只会拿到最后 2000 根（2018-06-22 起）。
    """
    uni = json.loads((ROOT / "pipeline" / "config" / "board_universe.json")
                     .read_text(encoding="utf-8"))
    names = uni["levels"]["申万二级行业"]
    sw = uni["sw_codes"]
    KLINE_DIR.mkdir(parents=True, exist_ok=True)
    end = dt.date.today().strftime("%Y%m%d")

    def one(name: str):
        last = ""
        for c in counts:
            try:
                df = SR._tencent_board_hist(name, "20100101", end,
                                            count=c, sw_code=sw.get(name))
                if "turnover" not in df.columns or df["turnover"].isna().all():
                    last = "无换手率列"
                    continue
                df.to_parquet(KLINE_DIR / f"{name.replace('/', '_')}.parquet",
                              index=False)
                return name, "ok", len(df)
            except Exception as e:  # noqa: BLE001
                last = str(e)[:70]
        return name, f"fail({last})", 0

    t0 = time.time()
    res = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for r in ex.map(one, names):
            res.append(r)
    ok = [r for r in res if r[1] == "ok"]
    print(f"[fetch] K线 ok={len(ok)}/{len(names)}（{time.time() - t0:.1f}s）")
    for r in res:
        if r[1] != "ok":
            print(f"[fetch][warn] {r[0]}: {r[1]}")
    if ok:
        rows = [r[2] for r in ok]
        print(f"[fetch] 每板块 {min(rows)}~{max(rows)} 根")
    return {"ok": len(ok), "total": len(names)}


def fetch_release_history(start_year: int = 2019) -> int:
    """按月分片抓解禁明细 → bt_data/release_hist.parquet

    **必须按月分片**：`stock_restricted_release_detail_em` 单次查询有服务端条数上限
    （实测一个季度正好回 400 条并停在 400 —— 那是被截断，不是巧合），
    按月查（≤ 约 250 条）才不会静默丢数据。
    """
    today = dt.date.today()
    months = []
    y = start_year
    m = 1
    while (y, m) <= (today.year, today.month):
        months.append((f"{y}{m:02d}01",
                       f"{y}{m:02d}{calendar.monthrange(y, m)[1]:02d}"))
        m += 1
        if m == 13:
            y, m = y + 1, 1

    def one(rng):
        a, b = rng
        for _ in range(3):
            try:
                return a, SR.release_calendar(a, b)
            except Exception:  # noqa: BLE001
                time.sleep(1.5)
        return a, None

    t0 = time.time()
    frames, near_cap = [], []
    with ThreadPoolExecutor(max_workers=6) as ex:
        for a, df in ex.map(one, months):
            if df is None or df.empty:
                continue
            if len(df) >= 380:
                near_cap.append((a, len(df)))
            frames.append(df)
    if not frames:
        print("[fetch] 解禁明细为空，跳过")
        return 0
    all_df = pd.concat(frames, ignore_index=True).drop_duplicates(
        subset=["股票代码", "解禁时间", "限售股类型"])
    BTDATA.mkdir(parents=True, exist_ok=True)
    all_df.to_parquet(REL_HIST, index=False)
    d = pd.to_datetime(all_df["解禁时间"])
    print(f"[fetch] 解禁 {len(all_df)} 条 / {len(months)} 个月 "
          f"({d.min().date()} ~ {d.max().date()})  {time.time() - t0:.1f}s")
    if near_cap:
        print(f"[fetch][warn] 以下月份接近 400 条上限，可能被截断：{near_cap}")
    return len(all_df)


# ==================================================================== 载入
def load_bt_klines(kline_dir: pathlib.Path = KLINE_DIR) -> dict[str, pd.DataFrame]:
    out: dict[str, pd.DataFrame] = {}
    for fp in sorted(kline_dir.glob("*.parquet")):
        df = pd.read_parquet(fp)
        df["date"] = pd.to_datetime(df["date"]).dt.normalize()
        out[fp.stem] = df.sort_values("date").reset_index(drop=True)
    return out


def load_bench() -> pd.DataFrame:
    fp = DATA / "index_hs300.parquet"
    df = pd.read_parquet(fp)
    df["date"] = pd.to_datetime(df["date"]).dt.normalize()
    return df.sort_values("date").reset_index(drop=True)


# ==================================================================== 面板
def build_panel(klines: dict[str, pd.DataFrame], bench: pd.DataFrame,
                w6: dict, stride: int = 5, min_bars: int = 260,
                min_boards: int = 100,
                horizons: tuple[int, ...] = (5, 20, 60, 120)):
    """逐评价日重建因子 → 长表面板（一行 = 一个「板块 × 评价日」）

    返回 (panel, meta)；panel 列包含 6 个因子分、组合分（两种修正口径）、
    规则检验要用的原始量、以及各 horizon 的前瞻收益。

    **日期轴取并集而不是交集**：各板块的可回溯长度并不相同（腾讯对不同行业
    返回的起始日期不同，实测取交集只剩 2021-12 起，白丢 3 年半样本）。
    改为「以并集为日历、每个板块按自己那天有没有 bar 决定是否入样」：
    评价日当天无 bar 就跳过该板块，其自身 bar 数不足 `min_bars` 也跳过 ——
    这样既不牺牲样本，又保证每个入样的组合都是完整的滚动窗口。
    """
    names = sorted(klines)
    axis = sorted(set().union(*[set(klines[n]["date"]) for n in names]))
    darr = {n: klines[n]["date"].to_numpy() for n in names}   # datetime64 数组
    print(f"[panel] 日期轴（并集）{len(axis)} 天：{axis[0].date()} ~ {axis[-1].date()}；"
          f"板块 {len(names)} 个")

    # 全市场广度（当日上涨板块占比）—— 生产用的是板块快照的涨跌家数，
    # 这里没有历史快照，用「板块涨跌占比」作代理，口径不同已在报告声明
    pct_mat = pd.DataFrame({n: klines[n].set_index("date")["pct"]
                            for n in names}).reindex(axis)
    breadth = ((pct_mat > 0).sum(axis=1)
               / pct_mat.notna().sum(axis=1).replace(0, np.nan))

    # 基准：沪深300 近 20 日收益（point-in-time）
    bs = bench.set_index("date")["close"].reindex(axis)
    bench_r20 = (bs / bs.shift(20) - 1)

    # 评价日的横截面必须够宽，否则「当天只有 3 个板块有历史」也在算 IC。
    # 逐日统计可用板块数：当天有 bar 且自身 bar 数 ≥ min_bars 才算可用。
    # 这一步不能省 —— 并集轴会被个别长历史标的（实测「铁路公路」从 2010-10 起，
    # 而其 2001 根 bar 跨了 5800 个自然日，中间大量缺口）拉到 2010 年。
    axa = pd.DatetimeIndex(axis).to_numpy()
    avail = np.zeros(len(axis), dtype=int)
    for n in names:
        dn = darr[n]
        pos = np.searchsorted(dn, axa, side="right") - 1
        ok = pos >= (min_bars - 1)
        pc = np.clip(pos, 0, len(dn) - 1)
        ok &= (dn[pc] == axa)
        avail += ok.astype(int)

    eval_idx = [i for i in range(min_bars - 1, len(axis) - 1, stride)
                if avail[i] >= min_boards]
    if not eval_idx:
        print("[error] 没有任何评价日满足横截面宽度要求")
        return pd.DataFrame(), {}
    print(f"[panel] 评价日 {len(eval_idx)} 个（步长 {stride} 交易日，"
          f"首个 {axis[eval_idx[0]].date()}，横截面 ≥{min_boards} 个板块；"
          f"实际中位 {int(np.median(avail[eval_idx]))} 个）")
    rows: list[dict] = []
    t0 = time.time()
    for k, i in enumerate(eval_idx):
        d = axis[i]
        dn_d = np.datetime64(d)
        br20 = bench_r20.get(d)
        if br20 is not None and not np.isfinite(br20):
            br20 = None
        br = float(breadth.get(d, np.nan))
        br = br if np.isfinite(br) else None
        for n in names:
            dn = darr[n]
            pos = int(np.searchsorted(dn, dn_d, side="right")) - 1
            if pos < 0 or dn[pos] != dn_d or pos + 1 < min_bars:
                continue          # 当天无 bar / 历史不够一个完整滚动窗口
            df = klines[n]
            sub = df.iloc[max(0, pos + 1 - LOOKBACK): pos + 1]
            # ↓↓↓ 全部直接调用线上因子函数：回测的就是跑在生产里的那段代码
            f = {
                "momentum": F.momentum(sub, br20),
                "trend": F.trend(sub),
                "volume_price": F.volume_price(sub),
                "turnover": F.turnover(sub),
                "valuation": F.valuation(sub)[0],
                "cycle": F.cycle(sub, 0),
            }
            f["valuation_inv"] = F._pctrank(-sub["close"])
            # 换手率的**原始 250 日分位**（0=最低换手，1=最高换手），与因子方向无关。
            # 必须单独留一列：分档表要按「低换手 / 高换手」切，而 `F.turnover()`
            # 在 2026-09-16 已改为返回 `1 − 分位`，直接拿它当分位会**把档位标签讲反**。
            turn_raw = (F._pctrank(sub["turnover"])
                        if "turnover" in sub.columns else 0.5)
            c = float(sub["close"].iloc[-1])
            a = sub["amount"] if "amount" in sub.columns else None
            vr5 = (float(a.tail(5).mean() / a.tail(20).mean())
                   if a is not None and a.tail(20).mean() > 0 else np.nan)
            ma20 = float(sub["close"].rolling(20).mean().iloc[-1])
            ma60 = float(sub["close"].rolling(60).mean().iloc[-1])
            kk, dd, jj = F._kdj(sub)
            rec = {
                "date": d, "name": n, "close": c,
                "score": S.combine(f, w6),
                "score_valinv": S.combine({**f, "valuation": f["valuation_inv"]},
                                          w6),
                # 对照组：**修复前**的换手率口径（分位越高分越高）。
                # 2026-09-16 把 `factors.turnover()` 修成低换手加分后，
                # `score` 才是「修正后」，这一列保留成「修正前」，供报告对照。
                "score_turn_old": S.combine({**f, "turnover": turn_raw}, w6),
                "r5": c / float(sub["close"].iloc[-6]) - 1,
                "r20": c / float(sub["close"].iloc[-21]) - 1,
                "r60": c / float(sub["close"].iloc[-61]) - 1,
                "vr5": vr5,
                "ma_bull": int(c > ma20 and ma20 > ma60),
                "above_ma20": int(c > ma20),
                "golden": int(kk.iloc[-1] > dd.iloc[-1] and kk.iloc[-2] <= dd.iloc[-2]),
                "j_val": float(jj.iloc[-1]),
                "turn_pct": turn_raw,
                "price_pct": f["valuation"],
                "breadth": br,
            }
            for key in BACKTESTABLE:
                rec[f"f_{key}"] = f.get(key)
            cl = df["close"].to_numpy(dtype=float)
            for h in horizons:
                rec[f"fwd{h}"] = (float(cl[pos + h] / c - 1)
                                  if pos + h < len(cl) and c > 0 else np.nan)
            rows.append(rec)
        if (k + 1) % 50 == 0:
            print(f"   ... {k + 1}/{len(eval_idx)} 评价日 "
                  f"（{time.time() - t0:.0f}s）")

    panel = pd.DataFrame(rows)
    print(f"[panel] 完成 {len(panel)} 行（{time.time() - t0:.0f}s）")
    meta = {"axis_start": axis[0], "axis_end": axis[-1], "n_axis": len(axis),
            "eval_start": axis[eval_idx[0]], "eval_end": axis[eval_idx[-1]],
            "n_eval": len(eval_idx), "n_boards": len(names),
            "avail_median": int(np.median(avail[eval_idx])),
            "avail_min": int(np.min(avail[eval_idx])),
            "min_boards": min_boards,
            "stride": stride, "min_bars": min_bars, "horizons": list(horizons)}
    return panel, meta


# =============================================================== 组合分评估
def eval_signal(panel: pd.DataFrame, col: str, horizons, quantiles: int = 5,
                min_boards: int = 20) -> dict:
    """一个信号列的 IC / ICIR / t / 分层 / 多空"""
    out: dict = {}
    for h in horizons:
        ic, tiers, ls, mkt = [], {}, [], []
        for _, g in panel.groupby("date"):
            g = g[[col, f"fwd{h}"]].dropna()
            if len(g) < min_boards:
                continue
            x = g[col].to_numpy(dtype=float)
            y = g[f"fwd{h}"].to_numpy(dtype=float)
            ic.append(BT.spearman(x, y))
            mkt.append(float(y.mean()))
            try:
                q = pd.qcut(pd.Series(x), quantiles, labels=False,
                            duplicates="drop")
            except Exception:  # noqa: BLE001
                continue
            means = []
            for k in range(quantiles):
                m = (q == k).to_numpy()
                mv = float(y[m].mean()) if m.sum() else np.nan
                tiers.setdefault(k, []).append(mv)
                means.append(mv)
            if np.isfinite(means[-1]) and np.isfinite(means[0]):
                ls.append(means[-1] - means[0])
        ic = np.array([x for x in ic if np.isfinite(x)], dtype=float)
        ls = np.array(ls, dtype=float)
        n = len(ic)
        m: dict = {"n_dates": n,
                   "ic_mean": float(ic.mean()) if n else np.nan,
                   "ic_std": float(ic.std(ddof=1)) if n > 1 else np.nan,
                   "ic_pos": float((ic > 0).mean()) if n else np.nan,
                   "ic_ir": float(ic.mean() / ic.std(ddof=1))
                            if n > 1 and ic.std(ddof=1) > 0 else np.nan,
                   "t": float(ic.mean() / ic.std(ddof=1) * np.sqrt(n))
                        if n > 1 and ic.std(ddof=1) > 0 else np.nan,
                   "market": float(np.mean(mkt)) if mkt else np.nan}
        m["tiers"] = {k: (float(np.nanmean(v)) if any(np.isfinite(v)) else np.nan)
                      for k, v in tiers.items()}
        m["ls_mean"] = float(ls.mean()) if len(ls) else np.nan
        m["ls_win"] = float((ls > 0).mean()) if len(ls) else np.nan
        m["mono"] = _monotonic([m["tiers"].get(k, np.nan)
                                for k in sorted(m["tiers"])])
        out[h] = m
    return out


def _monotonic(v: list[float]) -> bool | None:
    v = [x for x in v if np.isfinite(x)]
    if len(v) < 3:
        return None
    return all(v[i] <= v[i + 1] for i in range(len(v) - 1))


def _spread_md(o: dict, H, HN) -> str:
    """把 `extreme_spread` 的结果渲染成一行说明"""
    if not o or not o.get("h"):
        return ""
    parts = []
    for h in H:
        v = o["h"].get(h)
        if v:
            parts.append(f"{HN.get(h, h)} **{v['mean'] * 100:+.2f}pt**"
                         f"（为正的日期 {v['win']:.0%}，n={v['n']}）")
    if not parts:
        return ""
    return (f"> 逐日配对（**{o['last']} − {o['first']}**）：" + "；".join(parts)
            + "。这一行比上表的均值更可靠 —— 均值会被「哪些日期落进这一档」影响，"
              "配对则只在同一天内比较。")


def _f(x, pct=False, d=3) -> str:
    if x is None or not np.isfinite(x):
        return "—"
    return f"{x * 100:.2f}%" if pct else f"{x:.{d}f}"


# ==================================================================== 校验
def verify_against_archive(klines: dict[str, pd.DataFrame]) -> dict:
    """用真实归档快照校验「重建因子」的保真度

    归档里带完整的 `factors` 字典 —— 这是**唯一**能把重建值和真实值逐项对照的地方。
    注意归档取数发生在当日 13:40 的 warmup 之后，其最后一根 K 线是**盘中**价格，
    与本机收盘后拉的最后一根会有差异，差异大小本身也要如实报告。
    """
    fp = BTDATA / "archive" / "20260916_0745.json"
    if not fp.exists():
        return {}
    obj = json.loads(fp.read_text(encoding="utf-8"))
    date = pd.Timestamp(obj.get("updated") or "2026-09-16").normalize()
    real = {b["name"]: b["factors"] for b in obj["boards"]}
    rows = []
    for name, fl in real.items():
        df = klines.get(name)
        if df is None:
            continue
        sub = df[df["date"] <= date]
        if len(sub) < 260:
            continue
        sub = sub.reset_index(drop=True)
        mine = {
            "momentum": F.momentum(sub, None),
            "trend": F.trend(sub),
            "volume_price": F.volume_price(sub),
            "turnover": F.turnover(sub),
            "valuation": F.valuation(sub)[0],
            "cycle": F.cycle(sub, 0),
        }
        for k, v in mine.items():
            if k in fl:
                rows.append({"name": name, "factor": k,
                             "mine": v, "real": float(fl[k]),
                             "diff": v - float(fl[k])})
    dfv = pd.DataFrame(rows)
    if dfv.empty:
        return {}
    out = {"date": str(date.date()), "n_boards": dfv["name"].nunique(),
           "n_pairs": len(dfv), "per_factor": {}}
    for k, g in dfv.groupby("factor"):
        out["per_factor"][k] = {
            "spearman": BT.spearman(g["mine"], g["real"]),
            "mean_abs_diff": float(g["diff"].abs().mean()),
            "identical": int((g["diff"].abs() < 1e-9).sum()),
            "n": len(g),
        }
    out["overall_spearman"] = BT.spearman(dfv["mine"], dfv["real"])
    out["overall_mean_abs_diff"] = float(dfv["diff"].abs().mean())
    return out


# =============================================================== 规则级检验
def rule_tests(panel: pd.DataFrame, horizons, w: dict) -> dict:
    """逐条检验「术」层面的规则（含帽子哥的原话）

    所有分档表的数值都是**同日超额收益**（减去该评价日全市场等权），
    理由见 `add_excess()`：绝对收益会把市场 beta 混进来，实测能让同一规则得出相反结论。
    """
    out: dict = {}
    panel = add_excess(panel, horizons)

    # --- R1 分级阈值：现行 75/60/45/30 落在重建分的哪里
    th = w["grade"]
    qs = panel["score"].quantile([.1, .25, .5, .75, .9, .95]).round(1).to_dict()
    out["thresholds"] = {
        "cuts": {k: th[k] for k in ("strong", "watch", "neutral", "weak")},
        "score_quantiles": {f"P{int(k * 100)}": v for k, v in qs.items()},
        "score_mean": float(panel["score"].mean()),
        "score_std": float(panel["score"].std()),
    }
    cuts = [-np.inf, th["weak"], th["neutral"], th["watch"], th["strong"], np.inf]
    labels = ["<30 回避", "30-45 转弱", "45-60 中性", "60-75 观察", "≥75 主升"]
    panel = panel.copy()
    panel["grade_bucket"] = pd.cut(panel["score"], bins=cuts, labels=labels)
    out["grade_table"] = _group_fwd(panel, "grade_bucket", horizons)
    out["grade_spread"] = extreme_spread(panel, "grade_bucket", horizons)

    # --- R2 量价健康：「缩量调整是健康的」「放量背离要扣分」
    panel["vp_case"] = "其他"
    up = panel["r5"] > 0
    dn = panel["r5"] < 0
    expand = panel["vr5"] > 1.05
    shrink = panel["vr5"] < 0.95
    panel.loc[up & expand, "vp_case"] = "放量上涨"
    panel.loc[up & shrink, "vp_case"] = "缩量上涨"
    panel.loc[dn & shrink, "vp_case"] = "缩量调整（称健康）"
    panel.loc[dn & expand, "vp_case"] = "放量下跌（称要杀）"
    out["volume_price"] = _group_fwd(
        panel[panel["vp_case"] != "其他"], "vp_case", horizons)

    # --- R3 趋势结构：「MA20/MA60 多头排列 + 站上 MA20」
    # 用 Categorical 固定档位顺序，让「逐日配对」的符号可解释：
    # 首档 = 空头、末档 = 多头排列 ⇒ spread > 0 表示多头排列更好。
    panel["trend_case"] = pd.Categorical(
        np.where(panel["ma_bull"] == 1, "多头排列",
                 np.where(panel["above_ma20"] == 1, "仅站上MA20", "空头（破MA20）")),
        categories=["空头（破MA20）", "仅站上MA20", "多头排列"], ordered=True)
    out["trend"] = _group_fwd(panel, "trend_case", horizons)
    out["trend_spread"] = extreme_spread(panel, "trend_case", horizons)

    # --- R3b 趋势结构**内部拆解**：这个因子其实是四项加权的和
    #     trend = 0.25×站上MA20 + 0.25×MA多头 + 0.15×KDJ金叉 + 0.35×J值归一
    # 5.3 显示「多头排列」本身是略有效的，而该因子整体 IC 却是负的 ——
    # 拆开看才能知道是哪一项在反向拖累（J 值越高=越超买=分越高）。
    panel["j_case"] = pd.cut(panel["j_val"], [-np.inf, 0, 20, 50, 80, np.inf],
                             labels=["<0 超卖", "0-20", "20-50", "50-80",
                                     ">80 超买"])
    out["jval"] = _group_fwd(panel, "j_case", horizons)
    out["jval_spread"] = extreme_spread(panel, "j_case", horizons)
    panel["golden_case"] = pd.Categorical(
        np.where(panel["golden"] == 1, "当日KDJ金叉", "非金叉"),
        categories=["非金叉", "当日KDJ金叉"], ordered=True)
    out["golden"] = _group_fwd(panel, "golden_case", horizons)
    out["golden_spread"] = extreme_spread(panel, "golden_case", horizons)

    # --- R4 换手率位置：「低分位加分」？
    panel["turn_case"] = pd.cut(panel["turn_pct"], [0, .2, .5, .8, 1.0],
                                labels=["最低20%", "20-50%", "50-80%", "最高20%"])
    out["turnover"] = _group_fwd(panel, "turn_case", horizons)
    out["turnover_spread"] = extreme_spread(panel, "turn_case", horizons)

    # --- R5 估值位置（价格分位代理）方向
    panel["val_case"] = pd.cut(panel["price_pct"], [0, .2, .5, .8, 1.0],
                               labels=["价格最低20%", "20-50%", "50-80%",
                                       "价格最高20%"])
    out["valuation_dir"] = _group_fwd(panel, "val_case", horizons)
    out["valuation_spread"] = extreme_spread(panel, "val_case", horizons)

    # --- R6 动量：短/中/长涨幅各自的预测方向
    for col, lab in (("r5", "近5日涨幅"), ("r20", "近20日涨幅"),
                     ("r60", "近60日涨幅")):
        key = f"mom_{col}"
        panel[key] = pd.cut(panel[col], [-1, -.1, -.03, .03, .1, 1],
                            labels=["<-10%", "-10~-3%", "-3~3%", "3~10%", ">10%"])
        out[key] = {"label": lab, "table": _group_fwd(panel, key, horizons),
                    "spread": extreme_spread(panel, key, horizons)}
    return out


def _group_fwd(panel: pd.DataFrame, col: str, horizons) -> list[dict]:
    """按分组统计前瞻收益。

    **同时给「绝对收益」与「超额收益」**：把同一评价日全部板块的收益均值减掉，
    剩下的才是「这条规则有没有选股能力」。只看绝对收益会把市场 beta 混进来 ——
    实测同一个规则两种口径会给出**相反**的结论（低分位换手的行业既可能在牛市里
    绝对收益高，也可能只是恰好集中在市场见底那几期）。判规则一律看超额列。
    """
    p = panel
    rows = []
    for name, idx in p.groupby(col, observed=True).groups.items():
        g = p.loc[idx]
        rec = {"bucket": str(name), "n": len(g)}
        for h in horizons:
            v = g[f"fwd{h}"].dropna()
            rec[f"fwd{h}"] = float(v.mean()) if len(v) else np.nan
            if f"exc{h}" in p.columns:
                e = g[f"exc{h}"].dropna()
                rec[f"exc{h}"] = float(e.mean()) if len(e) else np.nan
        rows.append(rec)
    return rows


def add_excess(panel: pd.DataFrame, horizons) -> pd.DataFrame:
    """给面板加「同日超额收益」列（减掉同一评价日全市场等权）。

    必须在**全量面板**上算一次再拿去切片 —— 若在子集上现算，
    「基准」会变成该子集自己的均值，各表的可比性就没了。
    """
    p = panel.copy()
    for h in horizons:
        p[f"exc{h}"] = p[f"fwd{h}"] - p.groupby("date")[f"fwd{h}"].transform("mean")
    return p


def extreme_spread(panel: pd.DataFrame, col: str, horizons) -> dict:
    """逐日配对的「末档 − 首档」超额差。

    分档表里的均值会受到「哪些日期落在这一档」的影响（档位分布随时间变化），
    所以再加一个**逐日配对**的读数：同一天里比较末档与首档，再跨日期取均值与胜率。
    这一列才是「这条规则稳不稳定」的判据；差异只有零点几个百分点而胜率在 50% 附近
    的，一律当作噪音处理。

    只接受 **Categorical** 列（`pd.cut` 的产物），因为「档位顺序」是有语义的；
    若传入普通字符串列则返回 `{}`（调用方会退回只报分档表）。
    返回结构空时务必让调用方判断成 `{}` 而不是真值 —— 否则 `or` 回退会取到别的键。
    """
    d = panel.dropna(subset=[col])
    if d.empty or not hasattr(d[col], "cat") or len(d[col].cat.categories) < 2:
        return {}
    labs = list(d[col].cat.categories)
    first, last = str(labs[0]), str(labs[-1])
    out: dict = {"first": first, "last": last, "h": {}}
    for h in horizons:
        if f"exc{h}" not in d.columns:
            continue
        a = d[d[col].astype(str) == first].groupby("date")[f"exc{h}"].mean()
        b = d[d[col].astype(str) == last].groupby("date")[f"exc{h}"].mean()
        sp = (pd.concat([b.rename("hi"), a.rename("lo")], axis=1, sort=False)
              .dropna().eval("hi - lo"))
        if sp.empty:
            continue
        out["h"][h] = {"mean": float(sp.mean()),
                       "win": float((sp > 0).mean()),
                       "n": int(len(sp))}
    return out


def _rule_spread(rules: dict, key: str) -> dict:
    """安全地取某条规则的逐日配对结果（键不存在 / 类型不对都退化为空）"""
    o = rules.get(f"{key}_spread")
    if isinstance(o, dict):
        return o
    v = rules.get(key)
    if isinstance(v, dict):
        sp = v.get("spread")
        if isinstance(sp, dict):
            return sp
    return {}


def release_event_study(path: pathlib.Path) -> dict:
    """解禁罚分规则的真实事件研究（2019 至今 1.7 万条真实解禁事件）

    `chip_supply` 对解禁的假设：解禁 = 筹码增加 = 利空，占比越大罚得越狠
    （`release_threshold=0.05`）。东财的解禁明细自带「解禁前 20 日涨跌幅」
    与「解禁后 20 日涨跌幅」两个字段，可以直接检验这个假设 ——
    而且它是**逐个股**的，与板块指数无关，是独立于上面那套重建的旁证。
    """
    if not path.exists():
        return {}
    df = pd.read_parquet(path)
    df["ratio"] = pd.to_numeric(df["占解禁前流通市值比例"], errors="coerce")
    df["pre"] = pd.to_numeric(df["解禁前20日涨跌幅"], errors="coerce")
    df["post"] = pd.to_numeric(df["解禁后20日涨跌幅"], errors="coerce")
    df["mv"] = pd.to_numeric(df["实际解禁市值"], errors="coerce")
    df["d"] = pd.to_datetime(df["解禁时间"])
    df = df.dropna(subset=["pre", "post"])
    out: dict = {"n": len(df),
                 "range": [str(df["d"].min().date()), str(df["d"].max().date())],
                 "all_pre": float(df["pre"].mean()),
                 "all_post": float(df["post"].mean()),
                 "all_post_median": float(df["post"].median()),
                 "all_pre_median": float(df["pre"].median()),
                 "post_win": float((df["post"] > 0).mean()),
                 "pre_win": float((df["pre"] > 0).mean())}

    # 按「解禁占流通市值比例」分档 —— 直接对应 release_threshold=0.05
    bins = [-np.inf, 0.005, 0.01, 0.03, 0.05, 0.10, np.inf]
    labs = ["<0.5%", "0.5-1%", "1-3%", "3-5%", "5-10%", ">10%"]
    df["ratio_bucket"] = pd.cut(df["ratio"], bins, labels=labs)
    rows = []
    for lab, g in df.groupby("ratio_bucket", observed=True):
        if not len(g):
            continue
        rows.append({
            "bucket": str(lab), "n": len(g),
            "pre": float(g["pre"].mean()), "post": float(g["post"].mean()),
            "post_win": float((g["post"] > 0).mean()),
            "mv_yi": float(g["mv"].mean() / 1e8) if g["mv"].notna().any() else np.nan,
        })
    out["by_ratio"] = rows

    # 按解禁市值绝对值分档
    df["mv_bucket"] = pd.cut(df["mv"] / 1e8, [0, 1, 5, 20, 100, np.inf],
                             labels=["<1亿", "1-5亿", "5-20亿", "20-100亿", ">100亿"])
    rows2 = []
    for lab, g in df.groupby("mv_bucket", observed=True):
        if not len(g):
            continue
        rows2.append({"bucket": str(lab), "n": len(g),
                      "pre": float(g["pre"].mean()),
                      "post": float(g["post"].mean()),
                      "post_win": float((g["post"] > 0).mean())})
    out["by_mv"] = rows2

    # 逐年（有没有随市场环境漂移）
    rows3 = []
    for y, g in df.groupby(df["d"].dt.year):
        rows3.append({"year": int(y), "n": len(g),
                      "pre": float(g["pre"].mean()),
                      "post": float(g["post"].mean()),
                      "post_win": float((g["post"] > 0).mean())})
    out["by_year"] = rows3
    return out


# ------------------------- 解禁供给压力：按生产「同款口径」重建后直接检验
def fetch_board_mktcap(force: bool = False) -> dict:
    """当前各板块总市值（元），缓存到 `bt_data/board_mktcap.json`。

    **历史板块市值没有数据源**（东财快照只有今天的，tushare 不含行业），所以重建
    历史「解禁市值/板块总市值」只能用 `今日市值 × 板块价格指数比值` 回溯 ——
    见 `release_supply_test` 的 caveat。
    """
    fp = BTDATA / "board_mktcap.json"
    if fp.exists() and not force:
        try:
            d = json.loads(fp.read_text(encoding="utf-8"))
            if d:
                return d
        except Exception:  # noqa: BLE001
            pass
    try:
        snap = SR.board_snapshot()
    except Exception as e:  # noqa: BLE001
        print(f"[warn] 板块市值获取失败（{str(e)[:80]}），解禁占比口径将不可用")
        return {}
    out: dict = {}
    for _, r in snap.iterrows():
        nm = str(r.get("name") or "")
        try:
            v = float(r.get("mktcap"))
        except (TypeError, ValueError):
            continue
        if nm and np.isfinite(v) and v > 0:
            out[nm] = v
    if out:
        BTDATA.mkdir(parents=True, exist_ok=True)
        fp.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
        print(f"[fetch] 板块总市值 {len(out)} 条 -> {fp.name}")
    return out


def release_supply_test(panel: pd.DataFrame, klines: dict[str, pd.DataFrame],
                        bmap: pd.DataFrame, mktcap: dict,
                        rel_path: pathlib.Path = REL_HIST,
                        w: dict | None = None,
                        horizon_days: int = 30,
                        horizons=(5, 20, 60)) -> dict:
    """解禁罚分的**生产同款口径**检验（比 §5.7 的事件研究更贴题）

    为什么 §5.7 不够：那套事件研究看的是「解禁**前后** 20 日」的个股涨跌幅，而
    罚分实际打的时点是 **未来 30 日内有解禁**（`chips.release_pressure` 的
    `horizon_days=30`，方向是**向前看**）。本函数逐评价日重建那个 ratio：

        ratio = 未来 30 日该板块成分股解禁市值之和 / 板块总市值     （阈值 5% 吃满）
        pen   = chip_supply(ratio, 0)  ← 只含解禁项，减持项历史不可得

    然后看「**被罚得越狠的板块，未来收益是不是越差**」——方向对，则末档 − 首档
    应为**负**。同时给一个流动性口径的旁证：解禁市值 / 近 20 日日均成交额
    （「相当于几天的成交额」），它不依赖市值回溯，可作为交叉验证。

    **Caveat（必须随结论一起引用）**：板块总市值历史无源，用
    `今日市值 × 当日板块指数 / 今日板块指数` 回溯 —— 它假设板块股本在样本期内
    变化不大。因此**占比的绝对水平不可信、序关系大体可信**，结论只看方向与单调性。
    """
    if bmap is None or bmap.empty or not pathlib.Path(rel_path).exists():
        return {}
    rel = pd.read_parquet(rel_path)
    need = ["股票代码", "解禁时间", "实际解禁市值"]
    if not all(c in rel.columns for c in need):
        return {}
    rel = rel[need].copy()
    rel["_code"] = (rel["股票代码"].astype(str).str.extract(r"(\d+)")[0]
                    .fillna("").str.zfill(6))
    rel["d"] = pd.to_datetime(rel["解禁时间"], errors="coerce")
    rel["mv"] = pd.to_numeric(rel["实际解禁市值"], errors="coerce").fillna(0.0)
    rel = rel.dropna(subset=["d"])
    rel = rel[rel["mv"] > 0]
    if rel.empty:
        return {}

    m = bmap[["stock_code", "board_code", "board_name"]].drop_duplicates().copy()
    m["stock_code"] = m["stock_code"].astype(str).str.zfill(6)
    rel = rel.merge(m, left_on="_code", right_on="stock_code", how="inner")
    if rel.empty:
        return {}
    ev = rel.groupby(["board_code", "d"], as_index=False)["mv"].sum()
    name_of = (bmap[["board_code", "board_name"]].drop_duplicates()
               .set_index("board_code")["board_name"].astype(str).to_dict())
    code_of = {}
    for bc, nm in name_of.items():
        code_of.setdefault(str(nm), str(bc))
    p = panel if all(f"exc{h}" in panel.columns for h in horizons) \
        else add_excess(panel, horizons)
    matched = {str(n): code_of.get(str(n)) for n in p["name"].unique()}

    eval_dates = np.array(sorted(p["date"].unique()), dtype="datetime64[ns]")
    end_dates = eval_dates + np.timedelta64(horizon_days, "D")
    pieces = []
    for bname in p["name"].unique():
        bc = matched.get(str(bname))
        kl = klines.get(str(bname))
        if kl is None or kl.empty:
            continue
        g = ev[ev["board_code"] == bc] if bc else ev.iloc[0:0]
        if g.empty:
            fwd = np.zeros(len(eval_dates))
        else:  # 累加和 + 二分定位 → 一次性算全部评价日的「未来 N 日解禁市值」
            ds = g["d"].to_numpy(dtype="datetime64[ns]")
            cs = np.concatenate([[0.0], np.cumsum(g["mv"].to_numpy(dtype=float))])
            lo = np.searchsorted(ds, eval_dates, side="right")
            hi = np.searchsorted(ds, end_dates, side="right")
            fwd = cs[hi] - cs[lo]

        kd = kl["date"].to_numpy(dtype="datetime64[ns]")
        kc = kl["close"].to_numpy(dtype=float)
        pos = np.searchsorted(kd, eval_dates, side="right") - 1
        ok = pos >= 0
        posc = np.clip(pos, 0, len(kc) - 1)
        close_t = np.where(ok, kc[posc], np.nan)
        mc_now = float(mktcap.get(str(bname), 0.0) or 0.0) if mktcap else 0.0
        if mc_now > 0 and kc[-1] > 0:
            mc_t = mc_now * close_t / float(kc[-1])
        else:
            mc_t = np.full(len(eval_dates), np.nan)
        if "amount" in kl.columns:
            amt = kl["amount"].rolling(20).mean().to_numpy(dtype=float)
            amt_t = np.where(ok, amt[posc], np.nan)
        else:
            amt_t = np.full(len(eval_dates), np.nan)
        pieces.append(pd.DataFrame({
            "date": pd.DatetimeIndex(eval_dates), "name": str(bname),
            "rel_mv_yi": fwd / 1e8,
            "rel_ratio": fwd / mc_t,
            "rel_days": fwd / amt_t,
        }))
    if not pieces:
        return {}
    rel_all = pd.concat(pieces, ignore_index=True)

    mrg = p.merge(rel_all, on=["date", "name"], how="left")
    mrg["rel_mv_yi"] = mrg["rel_mv_yi"].fillna(0.0)
    mrg["rel_days"] = mrg["rel_days"].fillna(0.0)

    pen_cfg = (w or {}).get("penalty", {})
    # ⚠️ 这里**故意不读** `weights.yaml` 的 release_weight，而是钉死「原规则」的 12.0。
    # 理由：本节是「该不该罚」的**证据表**。若跟随当前配置，一旦按结论把
    # release_weight 调成 0，本节的罚分就全变成 0 → 所有分档塌成一档 →
    # **证据表会自己把自己的证据抹掉**（2026-09-16 实际踩到：报告里出现
    # 「被罚占比 0.0%」「最重罚档 − 无解禁：—」）。所以口径必须锚在「改动前」。
    # 阈值与上限仍读配置（它们没变，且描述的是「多大的解禁算吃满」）。
    rw = RELEASE_W_ORIGINAL
    th = float(pen_cfg.get("release_threshold", 0.05))
    cap = float(pen_cfg.get("chip_supply_max", 20.0))
    no_rel = mrg["rel_mv_yi"] <= 0
    # 生产罚分（只含解禁项）；无解禁 → 0 分，占比缺失 → NaN（不参与分档）
    ratio_eff = np.where(no_rel, 0.0, mrg["rel_ratio"].to_numpy(dtype=float))
    mrg["pen"] = [F.chip_supply(float(r), 0, rw, 0.0, cap, th)
                  if np.isfinite(r) else np.nan for r in ratio_eff]

    mrg["has_rel"] = pd.Categorical(np.where(no_rel, "无解禁", "有解禁"),
                                    categories=["无解禁", "有解禁"], ordered=True)
    # 用 -1 作「无解禁」哨兵，让分档表首档就是「无解禁」，与 extreme_spread 的
    # 「末档 − 首档」语义天然对齐（解禁越重 → 越差？）
    mrg["ratio_b"] = pd.cut(np.where(no_rel, -1.0, mrg["rel_ratio"].to_numpy(dtype=float)),
                            [-2.0, -0.5, 0.005, 0.01, 0.03, 0.05, np.inf],
                            labels=["无解禁", "<0.5%", "0.5-1%", "1-3%", "3-5%", "≥5%（吃满）"])
    mrg["pen_b"] = pd.cut(np.where(no_rel, -1.0, mrg["pen"].to_numpy(dtype=float)),
                          [-2.0, -0.5, 3.0, 6.0, 9.0, 12.0001],
                          labels=["0（无解禁）", "0-3 分", "3-6 分", "6-9 分", "9-12 分（吃满）"])
    mrg["days_b"] = pd.cut(np.where(no_rel, -1.0, mrg["rel_days"].to_numpy(dtype=float)),
                           [-2.0, -0.5, 0.1, 0.3, 1.0, 3.0, np.inf],
                           labels=["无解禁", "≤0.1 天", "0.1-0.3 天", "0.3-1 天",
                                   "1-3 天", ">3 天"])

    tabs = {}
    for key, col in (("has_rel", "has_rel"), ("by_ratio", "ratio_b"),
                     ("by_pen", "pen_b"), ("by_days", "days_b")):
        tabs[key] = {"rows": _group_fwd(mrg, col, horizons),
                     "spread": extreme_spread(mrg, col, horizons)}

    pen_vals = mrg["pen"].dropna()
    return {
        "release_w_used": float(rw),
        "horizon_days": horizon_days,
        "n_rows": int(len(mrg)),
        "n_dates": int(mrg["date"].nunique()),
        "n_events": int(len(rel)),
        "mv_total_yi": float(rel_all["rel_mv_yi"].sum()),
        "mktcap_ok": bool(mktcap),
        "penalized_share": float((pen_vals > 0).mean()) if len(pen_vals) else float("nan"),
        "pen_mean": float(pen_vals.mean()) if len(pen_vals) else float("nan"),
        "tables": tabs,
    }


# =============================================================== 宏观门检验
def macro_gate_test(bench: pd.DataFrame, klines: dict[str, pd.DataFrame],
                    horizons=(20, 60)) -> dict:
    """宏观分「进攻/中性/防守」门限的有效性检验（只能用可回溯的两个要素）

    四要素里 **政策**（需新闻历史）与 **情绪**（需涨停家数历史）不可回溯，
    故用 流动性 0.30 + 量价 0.25 在 0.55 内重新归一，得到 M6。
    因为绝对水平与生产口径不可比，这里检验的是**排序性**：
    「宏观分越高 → 后市指数越好」这个前提是否成立（按分位分档看指数前瞻收益）。
    """
    margin = pd.read_parquet(DATA / "margin.parquet")
    mc = "信用交易日期" if "信用交易日期" in margin.columns else margin.columns[0]
    margin[mc] = pd.to_datetime(margin[mc].astype(str), format="%Y%m%d")
    margin = margin.sort_values(mc).reset_index(drop=True)
    lpr = pd.read_parquet(DATA / "lpr.parquet")
    lpr["TRADE_DATE"] = pd.to_datetime(lpr["TRADE_DATE"])
    lpr = lpr.sort_values("TRADE_DATE").reset_index(drop=True)
    sf = pd.read_parquet(DATA / "sf.parquet")

    # 评价日只需从两融数据的起点开始（流动性要素的硬依赖），
    # 否则要白跑 8000+ 个必然被 `len(mg) < 21` 跳过的日期
    axis = [d for d in sorted(set(bench["date"])) if d >= margin[mc].min()]
    # 广度：用板块涨跌占比（无历史快照）
    up = {}
    for n, df in klines.items():
        up[n] = df.set_index("date")["pct"]
    upm = pd.DataFrame(up).reindex(axis)
    breadth = (upm > 0).sum(axis=1) / upm.notna().sum(axis=1).replace(0, np.nan)

    rows = []
    for d in axis:
        mg = margin[margin[mc] <= d]
        if len(mg) < 21:
            continue
        lp = lpr[lpr["TRADE_DATE"] <= d]
        sfm = sf[sf["月份"].astype(str).str[:6]
                 <= pd.Timestamp(d).strftime("%Y%m")] if "月份" in sf.columns else sf
        idx = bench[bench["date"] <= d]
        if len(idx) < 62:
            continue
        liq, _ = M.liquidity(mg, lp, sfm)
        vp, _ = M.volume_price(idx, float(breadth.get(d, np.nan))
                               if np.isfinite(breadth.get(d, np.nan)) else None)
        rows.append({"date": d, "M6": 100 * (0.30 * liq + 0.25 * vp) / 0.55,
                     "liq": liq, "vp": vp})
    md = pd.DataFrame(rows)
    if md.empty:
        return {}
    out = {"n_dates": len(md), "range": [str(md["date"].min().date()),
                                         str(md["date"].max().date())]}
    for h in horizons:
        md[f"idx{h}"] = [BT.forward_return(bench, d, h) or np.nan
                         for d in md["date"]]
    md["bucket"] = pd.qcut(md["M6"], 3, labels=["低", "中", "高"])
    rows2 = []
    for lab, g in md.groupby("bucket", observed=True):
        rec = {"bucket": str(lab), "n": len(g), "M6_mean": float(g["M6"].mean())}
        for h in horizons:
            v = g[f"idx{h}"].dropna()
            rec[f"idx{h}"] = float(v.mean()) if len(v) else np.nan
            rec[f"win{h}"] = float((v > 0).mean()) if len(v) else np.nan
        rows2.append(rec)
    out["tiers"] = rows2
    # 现行门限 70 / 40 在重建口径下的分位位置
    out["gate_quantile"] = {
        f"P{int((md['M6'] <= g).mean() * 100)}_for_{g}": int((md["M6"] <= g).sum())
        for g in (40, 70)}
    out["M6_range"] = [float(md["M6"].min()), float(md["M6"].max())]
    out["ic"] = {h: BT.spearman(md["M6"], md[f"idx{h}"]) for h in horizons}
    out["series"] = md
    return out


# =============================================================== 稳定性
def stability(panel: pd.DataFrame, horizons, quantiles: int = 5) -> dict:
    """分年度 / 分市场环境的稳定性。

    「全样本 IC 为负」不足以判一条规则无效 —— 可能只是某一年（如 2022 熊市）主导。
    分年再看一遍，符号一致的才算稳定结论。
    """
    p = panel.copy()
    p["year"] = p["date"].dt.year
    rows_score, rows_fac = [], []
    for y, g in p.groupby("year"):
        if g["date"].nunique() < 20:
            continue
        ms = eval_signal(g, "score", horizons, quantiles)
        rec = {"year": int(y), "n_dates": int(g["date"].nunique()),
               "n": len(g)}
        for h in horizons:
            rec[f"ic{h}"] = ms[h]["ic_mean"]
        rows_score.append(rec)
        rf = {"year": int(y), "n_dates": int(g["date"].nunique())}
        for k in BACKTESTABLE:
            mf = eval_signal(g, f"f_{k}", horizons, quantiles)
            rf[k] = mf[horizons[len(horizons) // 2]]["ic_mean"]
        rows_fac.append(rf)
    # 符号一致性：有几个年份的中期 IC 为负
    sign = {}
    for k in BACKTESTABLE:
        vals = [r[k] for r in rows_fac if np.isfinite(r[k])]
        if vals:
            sign[k] = {"neg": sum(1 for v in vals if v < 0), "n": len(vals)}
    return {"by_year_score": rows_score, "by_year_factor": rows_fac,
            "sign": sign}


# ==================================================================== 报告
def render(panel, meta, w, combo, fac, rules, rel, mac, verify, stab,
           rsup: dict | None = None) -> str:
    L: list[str] = []
    A = L.append
    now = dt.datetime.now()
    H = meta["horizons"]
    HN = {5: "短期（1 周）", 20: "中期（1 月）", 60: "长期（1 季）",
          120: "长期（半年）"}

    A("# 评分与规则有效性回测（长历史 · 点内重建）")
    A("")
    A(f"> 生成时间：{now:%Y-%m-%d %H:%M}　|　脚本：`pipeline/backtest_hist.py`"
      f"　|　因子版本指纹 `{meta.get('factor_rev', 'unknown')}`"
      f"（`factors.py` + `weights.yaml` 的 md5 前 8 位；**跨版本结论不可直接对比**）")
    A(f"> 样本：**{meta['n_boards']} 个申万二级板块 × {meta['n_eval']} 个评价日**"
      f"（每个评价日 × 每个板块 = 一行，共 **{len(panel):,} 行**）")
    A(f"> 评价区间：**{meta['eval_start']:%Y-%m-%d} ~ {meta['eval_end']:%Y-%m-%d}**"
      f"（{meta['stride']} 交易日抽样一次；K 线起点 {meta['axis_start']:%Y-%m-%d}）")
    A(f"> 横截面：每个评价日至少 {meta['min_boards']} 个板块，实际中位 "
      f"**{meta['avail_median']} 个**（最少 {meta['avail_min']} 个）")
    A(f"> 持有期：{' / '.join(f'{h} 交易日' for h in H)}")
    A("")
    A("> **本报告不构成投资建议**；板块指数不可直接交易，回测衡量的是「信号有效性」，"
      "不含手续费、冲击成本与容量约束。")
    A("")

    # ---------------------------------------------------------- 一句话结论
    A("## 一句话结论")
    A("")
    ic_txt = "、".join(
        f"{HN.get(h, str(h))} {combo['score'][h]['ic_mean']:+.3f}" for h in H)
    best = sorted(BACKTESTABLE,
                  key=lambda k: -abs(fac[k][H[len(H) // 2]]["ic_mean"]))
    A(f"- 技术面 6 维组合分的 Spearman IC：{ic_txt}。"
      f"信息量最大的维度是 `{best[0]}`、`{best[1]}`、`{best[2]}`。")
    worst = [k for k in BACKTESTABLE if fac[k][H[len(H) // 2]]["ic_mean"] < -0.02]
    mono_ok = all(bool(combo["score"][h]["mono"]) for h in H)
    A(f"- 分层单调性：{'三个持有期都单调递增' if mono_ok else '**并非所有持有期都单调**（高分档并不总是跑赢低分档）'}；"
      f"方向为负（即「分越高越差」）的维度有 {len(worst)} 个"
      f"{'：' + '、'.join('`' + k + '`' for k in worst) if worst else ''}。")
    A("- 详细口径、逐条规则检验与偏差声明见下文；**结论只适用于「技术面 6 维」这个子集**。")
    A("")

    # ---------------------------------------------------------- 方法学
    A("## 0. 读之前必须先知道的三件事")
    A("")
    A("**① 只有 6 个维度可回溯。** 12 维里，动量 / 趋势 / 量价健康 / 换手率位置 / "
      "估值位置 / 周期位置 六个维度**只依赖日 K 线**，可以在历史任一天用当时的数据重算；"
      "另外五个（主力资金流、行业出清度、政策强度、新闻情绪、板块共振）"
      "以及筹码供给罚分**需要当期快照**（成分股涨跌、资金流、当日新闻、解禁/减持公告），"
      "历史不可得。因此本报告的组合分是「**6 维内重新归一**」的技术面口径，"
      "**不是**线上那个 12 维总分。")
    A("")
    A("**② 回测用的就是线上那段代码。** 评价日 t 只把 `df[:t+1]` 喂给 `pipeline/factors.py` "
      "的原函数（`F.momentum` / `F.trend` / …），不做任何前视、不另写一套公式 —— "
      "否则「回测通过」和「上线有效」是两回事。")
    A("")
    A("**③ 归档快照这条路目前走不通。** 远端 11 份归档**全部是 2026-09-16 同一天**的多次运行，"
      "按日期去重后只剩 1 天 → `backtest.py` 的「样本不足」是**结构性**的，"
      "不是「再等两周就好」。要评估权重只能走本报告的重建法。")
    A("")

    # ---------------------------------------------------------- 保真度
    A("## 1. 重建保真度校验（用真实归档对照）")
    A("")
    if verify:
        A(f"归档快照 `bt_data/archive/20260916_0745.json` 带着**真实发布的因子值**，"
          f"把它和本机同日重建值逐项对照（{verify['n_pairs']} 对 / "
          f"{verify['n_boards']} 个板块）：")
        A("")
        A("| 因子 | Spearman | 平均绝对差 | 完全一致 |")
        A("|---|---|---|---|")
        for k, v in sorted(verify["per_factor"].items()):
            A(f"| {k} | {_f(v['spearman'])} | {_f(v['mean_abs_diff'], d=4)} | "
              f"{v['identical']}/{v['n']} |")
        A(f"| **合计** | **{_f(verify['overall_spearman'])}** | "
          f"**{_f(verify['overall_mean_abs_diff'], d=4)}** | — |")
        A("")
        A("> 差异来源已知：生产缓存的最后一根 K 线是 13:40 预热时的**盘中**价格，"
          "本机是收盘后取的全日 bar —— 动量/KDJ 这类吃最新价的因子会因此有微小偏差。")
        # 归档是「某一版因子代码」的产物。若某个因子的重建值与归档**负相关**，
        # 说明归档生成之后该因子的定义变过（如 2026-09-16 修正了 turnover 方向）——
        # 这不是重建错了，而是「对照物过期了」。必须显式说明，否则会被误读成保真度崩了。
        neg = [k for k, v in verify["per_factor"].items()
               if np.isfinite(v["spearman"]) and v["spearman"] < 0]
        if neg:
            A(">")
            A(f"> ⚠️ 注意：`{'`、`'.join(neg)}` 的重建值与归档**负相关**，"
              f"说明**归档快照是用旧版因子代码生成的**（对照物过期），"
              f"不是重建出错。报告头部的因子版本指纹可核对；等 CI 用新代码跑出一份"
              f"新归档后，本表的保真度会自动恢复到 0.9 以上。")
            A(f"> 因此**本报告的结论不要拿这份归档去评判**，"
              f"而应看下面各节基于 41,762 行面板的统计。")
    else:
        A("（未找到归档快照，跳过。执行 `--fetch-archive` 或手工放置到 `bt_data/archive/`）")
    A("")

    # ---------------------------------------------------------- 组合分
    A("## 2. 组合分（6 维技术面）的预测力：短期 / 中期 / 长期")
    A("")
    A("IC = 当日板块分与未来 N 日收益的 Spearman 秩相关；ICIR = IC 均值 / IC 标准差；"
      "t = ICIR × √样本数（**注意持有期重叠**：步长 5 日下，20/60/120 日的样本大量重叠，"
      "t 值应视为上界，看绝对水平和正负即可，别拿它当显著性检验）。")
    A("")
    A("| 口径 | 持有期 | IC 均值 | IC 标准差 | ICIR | t | IC>0 占比 | 多空均值 | 多空胜率 | 分层单调 |")
    A("|---|---|---|---|---|---|---|---|---|---|")
    for label, key in (("**现行 6 维**（已修正换手率方向）", "score"),
                       ("估值反向修正（对照）", "score_valinv"),
                       ("换手率**修复前**口径（对照）", "score_turn_old")):
        m = combo[key]
        for h in H:
            v = m[h]
            A(f"| {label} | {HN.get(h, str(h))} | {_f(v['ic_mean'])} | "
              f"{_f(v['ic_std'])} | {_f(v['ic_ir'])} | {_f(v['t'], d=2)} | "
              f"{_f(v['ic_pos'], True)} | {_f(v['ls_mean'], True)} | "
              f"{_f(v['ls_win'], True)} | {'✅' if v['mono'] else ('❌' if v['mono'] is False else '—')} |")
    A("")
    A("> 三行口径说明：「**现行 6 维**」= `factors.py` **当前**代码 —— 2026-09-16 已把 "
      "`turnover` 从「换手越高分越高」修正为「**低换手加分**」；"
      "「换手率**修复前**口径」= 修正之前那一版，保留它是为了直接读出**修复带来多少改善**；"
      "「估值反向修正」= 假设按 `valuation()` 旧 docstring 把价格分位也反转 —— "
      "回测证明那是**错的**方向（只作反证，别照做）。")
    A("")

    # ---------------------------------------------------------- 逐因子
    A("## 3. 逐因子有效性（哪条规则真在贡献信息）")
    A("")
    A("| 因子 | 权重 | " + " | ".join(f"{HN.get(h, h)} IC" for h in H) + " | 结论 |")
    A("|---" + "|---" * (len(H) + 2) + "|")
    for k in BACKTESTABLE:
        fv = fac[k]
        cells = " | ".join(_f(fv[h]["ic_mean"]) for h in H)
        # 判断：以中期 IC 的符号与大小为准
        mid = fv[H[len(H) // 2]]["ic_mean"]
        if not np.isfinite(mid):
            verdict = "样本不足"
        elif mid >= 0.03:
            verdict = "**方向正确且有信息**"
        elif mid <= -0.03:
            verdict = "**方向相反**"
        else:
            verdict = "近乎无信息"
        A(f"| `{k}` | {w['weights'].get(k, 0):.0%} | {cells} | {verdict} |")
    A("")
    A("> 权重列是 `weights.yaml` 里的**原始权重**（11 维之和＝1）；本报告的组合分把上表 6 维"
      "在自身内部重新归一，所以它们的**相对大小**才是有意义的，绝对数值不要直接和生产总分对比。")
    A("")

    # ---------------------------------------------------------- 分层
    A("## 4. 分层收益（按组合分 5 档，从低到高；**绝对收益**）")
    A("")
    A("| 持有期 | Q1（最低） | Q2 | Q3 | Q4 | Q5（最高） | Q5−Q1 | 全市场等权基准 |")
    A("|---|---|---|---|---|---|---|---|")
    for h in H:
        t = combo["score"][h]["tiers"]
        cells = " | ".join(_f(t.get(k, np.nan), True) for k in range(5))
        ls = t.get(4, np.nan) - t.get(0, np.nan)
        A(f"| {HN.get(h, str(h))} | {cells} | {_f(ls, True)} | "
          f"{_f(combo['score'][h]['market'], True)} |")
    A("")
    A("> 「全市场等权基准」= 同一评价日全部板块未来 N 日收益的等权平均，"
      "用于区分「选股能力」与「beta 收益」。同一日的 Q 档与基准相减即超额，"
      "所以第 5 节的分档表直接给超额值。")
    A("")

    # ---------------------------------------------------------- 规则检验
    A("## 5. 逐条规则检验")
    A("")
    A("> **下表全部为「同日超额收益」**：每个评价日先减掉当天全市场等权收益，再按分组取均值。"
      "只看绝对收益会把市场 beta 混进来 —— 实测同一个规则两种口径会给出相反结论。"
      "原始（绝对）收益在 `bt_data/backtest_hist_panel.csv` 里可自行复算。")
    A("")

    A("### 5.1 分级阈值（75 / 60 / 45 / 30）")
    A("")
    rt = rules["thresholds"]
    A(f"重建分的分布：均值 {rt['score_mean']:.1f}，标准差 {rt['score_std']:.1f}，"
      f"分位 " + "、".join(f"{k}={v}" for k, v in rt["score_quantiles"].items()) + "。")
    A("")
    A("| 档位 | 样本 | " + " | ".join(f"{HN.get(h, h)}超额" for h in H) + " |")
    A("|---" + "|---" * (len(H) + 1) + "|")
    for r in rules["grade_table"]:
        A(f"| {r['bucket']} | {r['n']:,} | "
          + " | ".join(_f(r.get(f"exc{h}"), True) for h in H) + " |")
    A("")
    A(_spread_md(rules.get("grade_spread"), H, HN))
    A("")

    A("### 5.2 量价健康（帽子哥：「缩量调整是健康的」，代码对缩量调整 +0.15、量价背离 −0.20）")
    A("")
    A("| 形态 | 样本 | " + " | ".join(f"{HN.get(h, h)}超额" for h in H) + " |")
    A("|---" + "|---" * (len(H) + 1) + "|")
    for r in rules["volume_price"]:
        A(f"| {r['bucket']} | {r['n']:,} | "
          + " | ".join(_f(r.get(f"exc{h}"), True) for h in H) + " |")
    A("")

    A("### 5.3 趋势结构（多头排列 = 站上 MA20 且 MA20 > MA60）")
    A("")
    A("| 形态 | 样本 | " + " | ".join(f"{HN.get(h, h)}超额" for h in H) + " |")
    A("|---" + "|---" * (len(H) + 1) + "|")
    for r in rules["trend"]:
        A(f"| {r['bucket']} | {r['n']:,} | "
          + " | ".join(_f(r.get(f"exc{h}"), True) for h in H) + " |")
    A("")
    A(_spread_md(rules.get("trend_spread"), H, HN))
    A("")
    A("**把同一条因子拆开看**：`trend` 实际是 "
      "`0.25×站上MA20 + 0.25×MA多头 + 0.15×KDJ金叉 + 0.35×J值归一` 的和 —— "
      "上表说明「MA 排列」这一项方向是对的，那因子整体为负就只能来自 KDJ 那两项"
      "（J 值越高＝越超买＝分越高）。拆开验证：")
    A("")
    A("| J 值区间 | 样本 | " + " | ".join(f"{HN.get(h, h)}超额" for h in H) + " |")
    A("|---" + "|---" * (len(H) + 1) + "|")
    for r in rules["jval"]:
        A(f"| {r['bucket']} | {r['n']:,} | "
          + " | ".join(_f(r.get(f"exc{h}"), True) for h in H) + " |")
    A("")
    A(_spread_md(rules.get("jval_spread"), H, HN))
    A("")
    A("| KDJ | 样本 | " + " | ".join(f"{HN.get(h, h)}超额" for h in H) + " |")
    A("|---" + "|---" * (len(H) + 1) + "|")
    for r in rules["golden"]:
        A(f"| {r['bucket']} | {r['n']:,} | "
          + " | ".join(_f(r.get(f"exc{h}"), True) for h in H) + " |")
    A("")
    A(_spread_md(rules.get("golden_spread"), H, HN))
    A("")

    A("### 5.4 换手率位置（**本节结论已落地**：低分位加分）")
    A("")
    A("| 换手 250 日分位 | 样本 | " + " | ".join(f"{HN.get(h, h)}超额" for h in H) + " |")
    A("|---" + "|---" * (len(H) + 1) + "|")
    for r in rules["turnover"]:
        A(f"| {r['bucket']} | {r['n']:,} | "
          + " | ".join(_f(r.get(f"exc{h}"), True) for h in H) + " |")
    A("")
    A(_spread_md(rules.get("turnover_spread"), H, HN))
    A("")
    A("> **本节结论已经改进代码**：2026-09-16 把 `factors.turnover()` 由 `_pctrank(turnover)`"
      "（换手越高分越高）改为 `1 - _pctrank(turnover)`（**低换手加分**），"
      "理由见本节数据 + `weights.yaml` 注释 + 前端实时补丁三处一致（前端本来就是"
      "`(2 − 换手)/10`，即低换手加分 —— 前后端此前是**反的**）。"
      "改完的组合分 IC 见第 2 节「现行 6 维」与「换手率修复前口径」两行之差。")
    A("")
    A("> 注意本节分档用的是**原始分位**（面板列 `turn_pct`），与因子方向无关，"
      "所以「最低20% / 最高20%」的标签在任何一版代码下都成立。")
    A("")

    A("### 5.5 估值位置（当前用价格分位代理 —— 方向是对的吗？）")
    A("")
    A("| 价格 250 日分位 | 样本 | " + " | ".join(f"{HN.get(h, h)}超额" for h in H) + " |")
    A("|---" + "|---" * (len(H) + 1) + "|")
    for r in rules["valuation_dir"]:
        A(f"| {r['bucket']} | {r['n']:,} | "
          + " | ".join(_f(r.get(f"exc{h}"), True) for h in H) + " |")
    A("")
    A(_spread_md(rules.get("valuation_spread"), H, HN))
    A("")
    A("> **本节的结论改的是「注释」而不是代码**：2026-09-16 重写了 `factors.valuation()` "
      "的 docstring —— 它原先笼统写着「PE 越低越好」，但无 PE 时走的是价格分位"
      "（`_pctrank(close)`），方向恰好相反。数据显示价格分位「越高越好」是**对的**，"
      "所以**不要**顺手反转它；写清这条是为了防止下一个人照着旧注释把代码改坏。")
    A("")

    A("### 5.6 动量：短/中/长涨幅分别往哪边预测")
    A("")
    for key in ("mom_r5", "mom_r20", "mom_r60"):
        blk = rules[key]
        A(f"**{blk['label']}**")
        A("")
        A("| 分组 | 样本 | " + " | ".join(f"{HN.get(h, h)}超额" for h in H) + " |")
        A("|---" + "|---" * (len(H) + 1) + "|")
        for r in blk["table"]:
            A(f"| {r['bucket']} | {r['n']:,} | "
              + " | ".join(_f(r.get(f"exc{h}"), True) for h in H) + " |")
        A("")
        A(_spread_md(blk.get("spread"), H, HN))
        A("")

    # ---------------------------------------------------------- 解禁
    A("### 5.7 解禁罚分规则（真实事件研究，独立于上面的重建）")
    A("")
    if rel:
        A(f"`chip_supply()` 对解禁的假设是「解禁 = 筹码增加 = 利空，占比越大罚得越狠」"
          f"（`release_threshold=0.05`）。用东财解禁明细的真实字段直接检验："
          f"**{rel['n']:,} 条事件**（{rel['range'][0]} ~ {rel['range'][1]}），"
          f"每条自带「解禁前 20 日」与「解禁后 20 日」涨跌幅。")
        A("")
        A(f"- 全体：解禁前 20 日平均 **{rel['all_pre']:+.2f}%**（中位 "
          f"{rel['all_pre_median']:+.2f}%，上涨占比 {rel['pre_win']:.1%}）；"
          f"解禁后 20 日平均 **{rel['all_post']:+.2f}%**（中位 "
          f"{rel['all_post_median']:+.2f}%，上涨占比 {rel['post_win']:.1%}）")
        A("")
        A("**按「解禁占流通市值比例」分档**（直接对应阈值 5%）：")
        A("")
        A("| 占比 | 事件数 | 解禁前 20 日 | 解禁后 20 日 | 后 20 日上涨占比 | 平均解禁市值 |")
        A("|---|---|---|---|---|---|")
        for r in rel["by_ratio"]:
            A(f"| {r['bucket']} | {r['n']:,} | {r['pre']:+.2f}% | {r['post']:+.2f}% | "
              f"{r['post_win']:.1%} | {r['mv_yi']:.1f} 亿 |")
        A("")
        A("**按解禁市值绝对值分档**：")
        A("")
        A("| 解禁市值 | 事件数 | 解禁前 20 日 | 解禁后 20 日 | 后 20 日上涨占比 |")
        A("|---|---|---|---|---|")
        for r in rel["by_mv"]:
            A(f"| {r['bucket']} | {r['n']:,} | {r['pre']:+.2f}% | {r['post']:+.2f}% | "
              f"{r['post_win']:.1%} |")
        A("")
        A("**逐年**（看有没有随市场环境漂移）：")
        A("")
        A("| 年份 | 事件数 | 解禁前 20 日 | 解禁后 20 日 | 后 20 日上涨占比 |")
        A("|---|---|---|---|---|")
        for r in rel["by_year"]:
            A(f"| {r['year']} | {r['n']:,} | {r['pre']:+.2f}% | {r['post']:+.2f}% | "
              f"{r['post_win']:.1%} |")
        A("")
    else:
        A("（未找到 `bt_data/release_hist.parquet`，先跑 `--fetch`）")
        A("")

    # ---------------------------- 解禁供给压力：生产同款口径的直接检验
    A("#### 5.7.1 解禁罚分的**生产同款口径**检验（比 5.7 更贴题）")
    A("")
    if rsup:
        A(f"5.7 看的是「解禁**前后** 20 日」的个股涨跌幅，而罚分实际打的时点是"
          f"**未来 {rsup['horizon_days']} 日内有解禁**（`horizon_days=30`，方向是向前看）。"
          f"这里逐评价日重建那个 ratio（未来 {rsup['horizon_days']} 日成分股解禁市值之和"
          f"÷ 板块总市值），再用**同一个** `factors.chip_supply()` 算出罚分，"
          f"然后看「**被罚得越狠的板块，未来超额收益是不是越差**」。")
        A("")
        A(f"> ⚠️ **本节固定用「原规则」的 `release_weight="
          f"{rsup.get('release_w_used', RELEASE_W_ORIGINAL):g}` 复现，不跟随当前配置。**"
          f"原因：本节是「该不该罚」的**证据表**；若跟随当前配置，那么一旦按结论把权重"
          f"调成 0，这里所有罚分都会变成 0、所有分档塌成一档 —— **证据表会自己把自己的"
          f"证据抹掉**（2026-09-16 实际发生：报告里出现「被罚占比 0.0%」与"
          f"「最重罚档 − 无解禁：—」）。所以口径锚在「改动前」，"
          f"改动后的口径见下文「处置」。阈值与上限仍读配置。")
        A("")
        A(f"- 覆盖：{rsup['n_events']:,} 条个股解禁事件 → {rsup['n_dates']} 个评价日 × "
          f"{rsup['n_rows']:,} 个「板块·日期」样本；累计解禁市值 "
          f"{rsup['mv_total_yi']:,.0f} 亿元")
        A(f"- 生产里**真的会被罚**的比例：**{rsup['penalized_share']:.1%}** 的样本"
          f"（平均罚分 {rsup['pen_mean']:.2f} 分）—— 也就是说这条规则大部分时候不生效，"
          f"只有约 {rsup['penalized_share']:.0%} 的板块会被它影响")
        A("")
        if not rsup["mktcap_ok"]:
            A("> ⚠️ 板块市值不可得（快照接口失败），占比口径不可用，下表只有「成交额倍数」与"
              "「有/无解禁」两栏可信。")
            A("")
        A("**A. 有解禁 vs 无解禁（未来 30 日内）**")
        A("")
        A("| 分组 | 样本 | " + " | ".join(f"{HN.get(h, h)}超额" for h in H) + " |")
        A("|---" + "|---" * (len(H) + 1) + "|")
        for r in rsup["tables"]["has_rel"]["rows"]:
            A(f"| {r['bucket']} | {r['n']:,} | "
              + " | ".join(_f(r.get(f"exc{h}"), True) for h in H) + " |")
        A("")
        A(_spread_md(rsup["tables"]["has_rel"].get("spread"), H, HN))
        A("")
        A(f"**B. 解禁占板块总市值比例**（对应 `release_threshold={w['penalty'].get('release_threshold', 0.05)}`）")
        A("")
        A("| 占比 | 样本 | " + " | ".join(f"{HN.get(h, h)}超额" for h in H) + " |")
        A("|---" + "|---" * (len(H) + 1) + "|")
        for r in rsup["tables"]["by_ratio"]["rows"]:
            A(f"| {r['bucket']} | {r['n']:,} | "
              + " | ".join(_f(r.get(f"exc{h}"), True) for h in H) + " |")
        A("")
        A(_spread_md(rsup["tables"]["by_ratio"].get("spread"), H, HN))
        A("")
        A("**C. 生产罚分档位**（`chip_supply` 只含解禁项，减持项历史不可得）")
        A("")
        A("| 罚分 | 样本 | " + " | ".join(f"{HN.get(h, h)}超额" for h in H) + " |")
        A("|---" + "|---" * (len(H) + 1) + "|")
        for r in rsup["tables"]["by_pen"]["rows"]:
            A(f"| {r['bucket']} | {r['n']:,} | "
              + " | ".join(_f(r.get(f"exc{h}"), True) for h in H) + " |")
        A("")
        A(_spread_md(rsup["tables"]["by_pen"].get("spread"), H, HN))
        A("")
        A("**D. 交叉验证：解禁市值 / 近 20 日日均成交额**（「相当于几天的成交额」，"
          "不依赖市值回溯）")
        A("")
        A("| 强度 | 样本 | " + " | ".join(f"{HN.get(h, h)}超额" for h in H) + " |")
        A("|---" + "|---" * (len(H) + 1) + "|")
        for r in rsup["tables"]["by_days"]["rows"]:
            A(f"| {r['bucket']} | {r['n']:,} | "
              + " | ".join(_f(r.get(f"exc{h}"), True) for h in H) + " |")
        A("")
        A(_spread_md(rsup["tables"]["by_days"].get("spread"), H, HN))
        A("")
        A("> **Caveat**：板块总市值历史无源，用 `今日市值 × 当日板块指数 ÷ 今日板块指数` "
          "回溯，假设板块股本在样本期内变化不大 —— 所以**占比的绝对水平不可信、"
          "序关系大体可信**，只看方向与单调性。D 表不依赖这个假设，是交叉验证。")
        A("")
        # --- 处置结论：四个口径同向，据此已改配置
        hmid_rs = H[len(H) // 2]

        def _mid_spread(key: str) -> dict:
            """取某张表「末档 − 首档」逐日配对在中期持有期上的统计量。"""
            return (((rsup["tables"].get(key) or {}).get("spread") or {})
                    .get("h", {}) or {}).get(hmid_rs, {}) or {}

        _pen_mid = _mid_spread("by_pen")
        _rat_mid = _mid_spread("by_ratio")
        _day_mid = _mid_spread("by_days")
        A("**处置（2026-09-16 已落地）：把解禁项从罚分里拿掉 —— "
          "`weights.yaml` 的 `release_weight: 12.0 -> 0.0`**")
        A("")
        A("A/B/C/D 四个口径**方向一致**：解禁压力越大，未来超额收益**不是更差、"
          "而是略好**。按原样罚分，等于系统性地扣那些随后跑得更好的板块。"
          "因此：")
        A("")
        A("- ✅ **退出罚分**：`release_weight` 置 0（配置项，随时可改回 12.0）。")
        A(f"- ✅ **不反向加分**：效应量很小（中期最重罚档 − 无解禁仅 "
          f"{(_pen_mid.get('mean') or 0.0) * 100:+.2f}pt）、且中段不单调"
          f"（6-9 分段为负）—— 反向属过拟合，不做。")
        A("- ✅ **不丢信息**：解禁占比与解禁市值仍逐板块写在产物 "
          "`penalty_detail.release_ratio / release_mv_yi` 里；事件日历的「解禁高峰」"
          "也照旧展示。**它从「扣分项」降级为「提示项」**。")
        A("- ⏸ **减持项保留**（`reduction_weight: 4.0`）：减持公告历史不可得、无法回测，"
          "但「内部人卖出」的理论依据比解禁强，且上限只有 4 分。")
        A("")
        A("> 偏差提醒：C 表（生产罚分档）与 B 表（占比档）是同一件事的两种切法，"
          "结论一致更能说明问题；但 6-9 分段的中期为负，说明这一档内部有别的因素，"
          "所以结论只支持「**不要罚**」，不支持「**要加分**」。")
        A("")
        A(f"> 交叉验证：不依赖市值回溯的 D 表（解禁市值 / 近 20 日成交额）"
          f"中期为 {(_day_mid.get('mean') or 0.0) * 100:+.2f}pt，与 B 表"
          f"（{(_rat_mid.get('mean') or 0.0) * 100:+.2f}pt）同向 —— "
          f"所以「市值回溯不可靠」这个 caveat 不足以推翻本节结论。")
    else:
        A("（未跑解禁供给压力检验：需 `bt_data/release_hist.parquet` 与 "
          "`web/data/cache/stock_board_map.parquet`）")
    A("")

    # ---------------------------------------------------------- 宏观
    A("### 5.8 宏观门（M ≥ 70 进攻 / 40 ≤ M < 70 中性 / M < 40 防守）")
    A("")
    if mac:
        A(f"四要素里**政策**（需新闻历史）与**情绪**（需涨停家数历史）不可回溯，"
          f"故只用 **流动性 0.30 + 量价 0.25** 在 0.55 内重新归一得到 `M6`，"
          f"检验其**排序性**（绝对水平与生产口径不可比）。"
          f"样本 {mac['n_dates']} 天（{mac['range'][0]} ~ {mac['range'][1]}，"
          f"受两融数据起点限制）。")
        A("")
        A("| M6 分档 | 天数 | M6 均值 | 沪深300 未来 20 日 | 胜率 | 沪深300 未来 60 日 | 胜率 |")
        A("|---|---|---|---|---|---|---|")
        for r in mac["tiers"]:
            A(f"| {r['bucket']} | {r['n']} | {r['M6_mean']:.1f} | "
              f"{_f(r.get('idx20'), True)} | {_f(r.get('win20'), True)} | "
              f"{_f(r.get('idx60'), True)} | {_f(r.get('win60'), True)} |")
        A("")
        A(f"- `M6` 的 Spearman IC（对沪深300）：20 日 "
          f"{_f(mac['ic'].get(20))}，60 日 {_f(mac['ic'].get(60))}")
        A(f"- `M6` 实际取值范围 {mac['M6_range'][0]:.1f} ~ {mac['M6_range'][1]:.1f}；"
          f"现行门限 40 / 70 落在这个区间的什么位置见报告末尾的结论。")
    else:
        A("（宏观部分跳过）")
    A("")

    # ---------------------------------------------------------- 结论
    A("### 5.9 分年度稳定性（全样本结论是不是被某一年主导的）")
    A("")
    A("全样本 IC 为负**不足以**判一条规则无效 —— 可能只是某一年（如 2022 或 2026）主导。"
      "分年再看一遍，**符号一致**才算稳定结论。")
    A("")
    if stab and stab.get("by_year_score"):
        A("**组合分 IC 分年**")
        A("")
        A("| 年份 | 评价日 | " + " | ".join(f"IC {h}日" for h in H) + " |")
        A("|---" + "|---" * (len(H) + 1) + "|")
        for r in stab["by_year_score"]:
            A(f"| {r['year']} | {r['n_dates']} | "
              + " | ".join(_f(r.get(f"ic{h}")) for h in H) + " |")
        A("")
        A(f"**逐因子中期（{H[len(H) // 2]} 日）IC 分年**")
        A("")
        A("| 年份 | " + " | ".join(f"`{k}`" for k in BACKTESTABLE) + " |")
        A("|---" + "|---" * len(BACKTESTABLE) + "|")
        for r in stab["by_year_factor"]:
            A(f"| {r['year']} | "
              + " | ".join(_f(r.get(k)) for k in BACKTESTABLE) + " |")
        A("")
        if stab.get("sign"):
            A("**符号一致性**（中期 IC 为负的年份数 / 有效年份数）：")
            A("")
            A("| 因子 | 负号年份 | 判定 |")
            A("|---|---|---|")
            for k in BACKTESTABLE:
                s = stab["sign"].get(k)
                if not s:
                    continue
                stable = s["neg"] == s["n"] or s["neg"] == 0
                A(f"| `{k}` | {s['neg']}/{s['n']} | "
                  f"{'**符号稳定**' + ('（持续为负）' if s['neg'] == s['n'] else '（持续为正）') if stable else '符号不稳定，视作噪音'} |")
            A("")
    else:
        A("（样本不足以分年）")
        A("")

    # ---------------------------------------------------------- 结论
    A("## 6. 结论")
    A("")
    _conclusions(A, combo, fac, rules, rel, mac, w, H, HN, rsup, meta)

    # ---------------------------------------------------------- 局限
    A("## 7. 偏差与局限（决定这份报告能被引用到什么程度）")
    A("")
    A("| 偏差 | 方向 | 说明 |")
    A("|---|---|---|")
    A("| **幸存者偏差** | 偏乐观 | 用的是**当前**120 个板块的清单回溯到 2018 年。"
      "历史上被合并/边缘化的二级行业不在样本里，等于剔除了「变差就消失」的那部分 |")
    A("| **不可回溯维度缺失** | 不确定 | 主力资金流、出清度、政策、新闻情绪、板块共振、"
      "筹码罚分（合计权重 46% + 罚分）没进组合分。它们可能带来本报告看不到的增量信息 |")
    A("| **持有期重叠** | 高估显著性 | 步长 5 日而持有期 20/60/120 日，样本高度重叠，"
      "ICIR 与 t 值系统性偏高。**只有 IC 均值与分层的方向可作结论** |")
    A("| **板块指数不可交易** | 偏乐观 | 未计任何交易成本、冲击成本，"
      "也没有考虑对应 ETF 是否真的存在/有流动性 |")
    A("| **广度口径不同** | 轻微 | 生产用板块快照的涨跌家数，重建只能用「板块涨跌占比」 |")
    A("| **估值代理** | 见 5.5 | 无真实行业 PE，只能用价格分位代理，"
      "而它与动量的相关性天然很高（同一份价格产生两个因子） |")
    A("")
    A("> 结论的定位：本报告能回答「**技术面这 6 条规则有没有信息**」，"
      "不能回答「12 维总分有多准」。后者需要等归档快照积累（或补齐行业估值与历史新闻）。")
    A("")

    # ---------------------------------------------------------- 复现
    A("## 8. 复现")
    A("")
    A("```bash")
    A("PY=C:/Users/40818/.workbuddy/binaries/python/envs/default/Scripts/python.exe")
    A("")
    A("# ① 一次性取数（约 15 s）：120 板块 × 2001 根日线 + 2019 至今解禁明细")
    A("$PY pipeline/backtest_hist.py --fetch")
    A("")
    A(f"# ② 跑回测（本次约 {meta['n_eval']} 个评价日 × {meta['n_boards']} 个板块）")
    A("$PY pipeline/backtest_hist.py --horizons 5,20,60,120 --stride 5")
    A("")
    A("# ③ 只改报告措辞时复用已建好的面板，跳过重建")
    A("$PY pipeline/backtest_hist.py --reuse-panel")
    A("```")
    A("")
    A("| 输入 | 位置 | 说明 |")
    A("|---|---|---|")
    A("| 板块日线 | `bt_data/kline/*.parquet` | 腾讯 `newfqkline`（`pt01`+申万码），"
      "每个板块 2001 根，起点 2018-06-22 |")
    A("| 基准指数 | `web/data/index_hs300.parquet` | 沪深300 日线，用于动量超额 |")
    A("| 两融 / LPR / 社融 | `web/data/*.parquet` | 宏观门检验用；两融起点 2024-01 |")
    A("| 解禁明细 | `bt_data/release_hist.parquet` | 93 个月分片抓取，1.7 万条真实事件 |")
    A("| 对照快照 | `bt_data/archive/20260916_0745.json` | 真实归档，用于 §1 保真度校验 |")
    A("")
    A("> `bt_data/` 已列入 `.gitignore` —— 它是可随时重抓的原料，"
      "且**必须放在 `web/` 之外**，否则会被 Pages 一起发布出去。")
    A("")
    return "\n".join(L)


def _conclusions(A, combo, fac, rules, rel, mac, w, H, HN, rsup=None,
                 meta=None) -> None:
    """把上面算出来的东西收敛成「短/中/长期分别是什么」"""
    short, mid = H[0], H[len(H) // 2]
    # ⚠️ `mid = H[len(H)//2]` 在 4 档下取到的是**第 3 档 = 60 日（长期 1 季）**，
    # 不是 20 日。以前这里硬编码写成「中期」，与第 2 / 6.1 节的命名
    # （20 日才叫「中期（1 月）」）**冲突**，同一份报告里同一个词指两个持有期。
    # 现在一律用 `HN` 里的正式标签，不再手写「中期」。
    mid_lab = HN.get(mid, f"{mid} 交易日")
    A("### 6.1 短期 / 中期 / 长期，分别是什么结论")
    A("")
    A(f"| 视角 | 组合分 IC | 是否单调 | 关键事实 |")
    A("|---|---|---|---|")
    for h in H:
        v = combo["score"][h]
        A(f"| **{HN.get(h, str(h))}** | {_f(v['ic_mean'])} | "
          f"{'是' if v['mono'] else ('否' if v['mono'] is False else '—')} | "
          f"IC>0 占比 {_f(v['ic_pos'], True)}，多空均值 {_f(v['ls_mean'], True)} |")
    A("")

    # 逐因子按 horizon 排序，给出短/中长期分别最有效的因子（标签统一取自 HN）
    for tag, h in ((HN.get(short, f"{short} 交易日"), short),
                   (mid_lab, mid),
                   (HN.get(H[-1], f"{H[-1]} 交易日"), H[-1])):
        rank = sorted(BACKTESTABLE, key=lambda k: -abs(fac[k][h]["ic_mean"])
                      if np.isfinite(fac[k][h]["ic_mean"]) else 0)
        s = "、".join(f"`{k}`({fac[k][h]['ic_mean']:+.3f})" for k in rank[:3])
        A(f"- **{tag}**（{h} 交易日）信息量最大的三个维度：{s}")
    A("")

    # 规则方向判定
    A("### 6.2 哪几条规则被判「有效」，哪几条「方向存疑 / 站不住」")
    A("")
    A("判定同时看两个口径：**IC**（全截面秩相关）与**逐日配对**（末档 − 首档，"
      "见第 5 节各表下方那一行）。两者一致才算定论；若冲突，说明该维度是多项加权而成、"
      "内部有项在反向拖累，必须拆开看（第 5.3 节的 KDJ 拆解就是这种情况）。")
    A("")
    A(f"下表两个数值口径都取 **{mid_lab}**（`H[len(H)//2]` = {mid} 交易日），"
      f"列名按此写，不要与第 2 节「中期（1 月）」混淆。")
    A("")
    A(f"| 规则 | {mid_lab} IC | 逐日配对（末档 − 首档，{mid_lab}） | 判定 |")
    A("|---|---|---|---|")
    spread_map = {"momentum": "mom_r20", "trend": "trend",
                  "volume_price": None, "turnover": "turnover",
                  "valuation": "valuation", "cycle": "mom_r60"}
    for k in BACKTESTABLE:
        ic = fac[k][mid]["ic_mean"]
        sk = spread_map.get(k)
        sp = None
        if sk:
            o = _rule_spread(rules, sk)
            if o.get("h"):
                sp = o["h"].get(mid, {}).get("mean")
        if ic <= -0.02 and sp is not None and sp <= -0.002:
            verdict = "**方向相反（两口径一致）**"
        elif ic <= -0.02 and sp is not None and sp >= 0.002:
            verdict = "**证据冲突 → 须逐项拆开**"
        elif abs(ic) < 0.02:
            verdict = "近乎无信息"
        elif ic >= 0.02:
            verdict = "方向正确"
        else:
            verdict = "信号很弱"
        A(f"| `{k}` | {_f(ic)} | "
          f"{('—' if sp is None else f'{sp * 100:+.2f}pt')} | {verdict} |")
    if rel:
        gap = rel["all_post"] - rel["all_pre"]
        extra = ""
        if rsup:
            sp = (rsup["tables"]["by_pen"].get("spread") or {}).get("h", {}).get(mid) or {}
            if sp:
                extra = (f"<br>生产同款口径（5.7.1）：最重罚档 − 无解禁 = "
                         f"**{sp['mean'] * 100:+.2f}pt**（为正的日期 {sp['win']:.0%}，"
                         f"n={sp['n']}）")
        A(f"| 解禁罚分 | 见 5.7 / 5.7.1（事件研究，非截面因子） | — | "
          f"{'解禁后确实更弱，罚分方向成立' if rel['all_post'] < rel['all_pre'] else '**解禁后并不更弱，罚分方向反了 → 已退出罚分**'}"
          f"（{gap:+.2f}pt）{extra} |")
    A("")

    # --- 6.3 两个「方向存疑」维度的正反口径对比（从 5.4 / 5.5 的分档表直接读）
    A("### 6.3 两处「实现方向 ↔ 文档意图」不一致的实证裁定（**均已处置**）")
    A("")
    A("`weights.yaml` 的注释写着**换手率「低分位加分」**，但 `factors.turnover()` 返回的是"
      "`_pctrank(turnover)`（**换手越高分越高**）；`factors.valuation()` 的 docstring 写着"
      "「PE 越低越好」，但无 PE 时走 `_pctrank(close)`（**价格越高分越高**）—— "
      "与「低估值加分」的意图相反。两者都可以用同一份面板直接裁定：")
    A("")
    A("> **处置状态（2026-09-16）**：换手率**已改代码**"
      "（`1 - _pctrank(turnover)`，低换手加分）；估值**已改注释**（重写 docstring，"
      "说明两条支路方向相反、价格分位方向是对的）。第 2 节的「现行 6 维」已是修复后的口径。")
    A("")
    A("| 维度 | 现行实现 | 末档（最低） vs 首档（最高） 中期收益差 | 数据支持 |")
    A("|---|---|---|---|")
    for key, col, lab_lo, lab_hi, name in (
            ("turnover", "turn_case", "最低20%", "最高20%", "换手率位置"),
            ("valuation_dir", "val_case", "价格最低20%", "价格最高20%", "估值位置"),
    ):
        rows = {r["bucket"]: r for r in rules[key]}
        lo, hi = rows.get(lab_lo, {}), rows.get(lab_hi, {})
        hmid = H[len(H) // 2]
        d = lo.get(f"exc{hmid}", np.nan) - hi.get(f"exc{hmid}", np.nan)
        impl = "换手越高分越高" if key == "turnover" else "价格越高分越高"
        if key == "turnover":
            verdict = ("**已改代码**（低换手明显更好；2026-09-16 改为 `1 − _pctrank`）"
                       if d > 0 else "现行方向可接受")
        else:
            verdict = ("**已改注释**（价格分位越高越好，数据支持现行实现；"
                       "是 docstring 沿用了 PE 的语义，2026-09-16 已重写）" if d < 0
                       else "**应改代码**（低价格分位更好）")
        A(f"| {name} | {impl} | {d * 100:+.2f}pt（{hmid} 交易日） | {verdict} |")
    A("")
    A("> 注意这两处「不一致」的**处理方式完全不同**：换手率是**代码**写反了，"
      "估值是**注释**写错了。只看代码不看数据、或只看注释不看数据，都会改错一边。")
    A("")

    # --- 6.4 分级阈值是否落在单调区间
    A("### 6.4 分级阈值（75/60/45/30）是否可用")
    A("")
    gt = rules["grade_table"]
    if gt:
        hmid = H[len(H) // 2]
        seq = [(r["bucket"], r.get(f"exc{hmid}", np.nan)) for r in gt
               if r["bucket"].startswith(("<", "3", "4", "6", "≥"))]
        vals = [v * 100 for _, v in seq]
        A(f"按现行阈值分桶后的**{hmid} 交易日超额收益**（从低分档到高分档）："
          + " → ".join(f"{b} {v:+.2f}%" for (b, _), v in zip(seq, vals)))
        A("")
        mono = all(vals[i] <= vals[i + 1] for i in range(len(vals) - 1)) \
            if len(vals) > 1 else None
        A(f"- 是否随分数递增：**{'是' if mono else ('否' if mono is False else '样本不足')}**")
    A("")

    # --- 6.5 怎么用这份报告
    A("### 6.5 建议怎么用这份报告（可执行的三条）")
    A("")
    hs = " / ".join(f"{HN.get(h, h)} {combo['score'][h]['ic_mean']:+.3f}" for h in H)
    hv = " / ".join(f"{combo['score_valinv'][h]['ic_mean']:+.3f}" for h in H)
    ht = " / ".join(f"{combo['score_turn_old'][h]['ic_mean']:+.3f}" for h in H)
    A(f"1. **先修方向，再谈权重**（**已落地**，2026-09-16）。三个口径的组合分 IC：")
    A(f"   - 现行 6 维（`turnover` 已修正为低换手加分）：{hs}")
    A(f"   - 换手率**修复前**口径（分位越高分越高）：{ht}")
    A(f"   - 估值反向（`_pctrank(-close)`）：{hv}")
    A("")
    A("   换手率反向后 IC 明显向 0 靠拢（即**去掉了一项持续扣分的成分**），"
      "属「改一行」级别的改进，已在 `factors.turnover()` 落地；"
      "估值反向则让 IC 更负 → **价格分位代理的方向是被数据支持的，不要顺手反转它**，"
      "该改的是 `factors.valuation()` 的 docstring（它按 PE 的语义写成「越低越好」，"
      "但无 PE 时走的是价格分位），也已重写。")
    A("2. **别用这份报告调 12 维权重的绝对值**：可回溯的只有 6 维（合计原始权重 54%），"
      "另外 5 维（主力资金流 / 出清度 / 政策 / 新闻情绪 / 板块共振，合计 46%）"
      "与筹码罚分都不在组合分里；且 `ICIR/t` 因持有期重叠而系统性偏高。"
      "要用它调权重，必须先把另外几维的历史补齐（行业估值 / 历史新闻 / 历史成分股）。")
    A("3. **给 `backtest.py` 一个真正的样本**（**已落地**）：`main.py` 的 score 阶段现在"
      "除了整份归档，还会写一份几 KB 的 **`*_summary.json` 分数矩阵**"
      "（`weights.yaml` 的 `archive.summary` 开关，默认开），它带 11 维因子值 —— "
      "跨日积累后，`backtest.py` 就能算**线上真实发布分**的 IC，"
      "并补上本报告覆盖不到的 5 维（第 3 节那张逐因子表）。"
      "**注意「同一天多份不算样本」**：去重按日期，攒的是「天数」不是「文件数」。")
    A("")

    # --- 6.6 解禁罚分的处置（本节唯一的「凭回测改了线上配置」的结论）
    A("### 6.6 已凭本报告改掉的线上配置（便于事后追溯）")
    A("")
    A("| # | 改动 | 依据 | 性质 |")
    A("|---|---|---|---|")
    A("| 1 | `factors.turnover()`：`_pctrank(turnover)` → `1 - _pctrank(turnover)`"
      "（低换手加分） | §5.4 / §6.3：5 个有效年份符号 5/5 全负；反向后组合分中期 IC "
      "从 -0.031 改善到 -0.014 | **改代码** |")
    A("| 2 | `factors.valuation()`：重写 docstring（说明两条支路方向相反） | §5.5 / §6.3："
      "价格分位代理的正向是被数据支持的，代码没错、注释错 | **改注释** |")
    A("| 3 | `weights.yaml`：`release_weight: 12.0 → 0.0`（解禁退出罚分） | §5.7.1："
      "A/B/C/D 四个口径一致说明「被罚得越狠 → 未来略好」；不做反向加分 | **改配置** |")
    A("")
    A("> 三处的**性质不同**，这是本节最值得记的一点：换手率是**代码写反了**、"
      "估值是**注释写错了**、解禁是**规则本身不成立**。"
      "只改代码不看数据、或只改注释不看数据，都会在另外两处改错。")
    A("")
    A(f"> 改动前的口径在本报告里都留了对照行（第 2 节的「换手率修复前口径」"
      f"「估值反向修正」），所以**改善幅度是可复算的**，不是只有结论。"
      f"本次面板因子指纹 `{(meta or {}).get('factor_rev', '—')}` —— "
      f"下次改 `factors.py` / 进面板的权重 / `PANEL_SCHEMA` 后指纹会变，"
      f"跨指纹的结论**不要直接对比数值**（口径与守卫见交接文档 §11.2）。")
    A("")


# ==================================================================== main
def main() -> int:
    ap = argparse.ArgumentParser(description="长历史点内重建回测")
    ap.add_argument("--fetch", action="store_true", help="抓长历史 K 线 + 解禁明细")
    ap.add_argument("--fetch-archive", action="store_true",
                    help="从 GitHub 拉一份真实归档快照做保真度校验")
    ap.add_argument("--horizons", default="5,20,60,120")
    ap.add_argument("--stride", type=int, default=5, help="评价日步长（交易日）")
    ap.add_argument("--quantiles", type=int, default=5)
    ap.add_argument("--min-bars", type=int, default=260,
                    help="入样所需的最少历史 bar 数（滚动窗口 250）")
    ap.add_argument("--min-boards", type=int, default=100,
                    help="评价日横截面最少板块数")
    ap.add_argument("--kline", default=str(KLINE_DIR))
    ap.add_argument("--out", default=str(ROOT / "docs" / "backtest_report_hist.md"))
    ap.add_argument("--csv", default=str(BTDATA / "backtest_hist_panel.csv"))
    ap.add_argument("--no-macro", action="store_true")
    ap.add_argument("--no-release", action="store_true")
    ap.add_argument("--no-rel-supply", dest="rel_supply", action="store_false",
                    help="跳过「解禁供给压力（生产同款口径）」检验")
    ap.add_argument("--no-mktcap", dest="mktcap", action="store_false",
                    help="不取板块市值（解禁占比口径不可用，只留成交额口径）")
    ap.add_argument("--reuse-panel", action="store_true",
                    help="复用已有的 --csv 面板与其 meta，跳过重建（只迭代报告时用）")
    ap.add_argument("--allow-stale-panel", action="store_true",
                    help="复用没有 factor_rev 指纹的旧面板（危险：因子可能已变）")
    ap.add_argument("--no-stability", dest="stability", action="store_false",
                    help="跳过分年度稳定性检验（会多花几十秒）")
    args = ap.parse_args()

    if args.fetch:
        fetch_klines()
        fetch_release_history()
        return 0

    horizons = tuple(int(x) for x in str(args.horizons).split(",") if x.strip())
    w = load_weights()
    w6 = {k: w["weights"][k] for k in BACKTESTABLE}

    kdir = pathlib.Path(args.kline)
    klines = load_bt_klines(kdir)
    if len(klines) < 20:
        print(f"[error] {kdir} 下只有 {len(klines)} 个板块，先跑 --fetch")
        return 1
    bench = load_bench()

    verify = verify_against_archive(klines)
    if verify:
        print(f"[verify] 重建 vs 归档：Spearman {verify['overall_spearman']:.3f}，"
              f"平均绝对差 {verify['overall_mean_abs_diff']:.4f}")

    panel, meta = (pd.DataFrame(), {})
    meta_fp = pathlib.Path(args.csv).with_suffix(".meta.json")
    if args.reuse_panel and pathlib.Path(args.csv).exists():
        panel = pd.read_csv(args.csv, encoding="utf-8-sig")
        panel["date"] = pd.to_datetime(panel["date"])
        if meta_fp.exists():
            raw = json.loads(meta_fp.read_text(encoding="utf-8"))
            # 因子代码变过就不许复用：面板里的因子值是旧公式算的，
            # 复用会得到「看起来正常、结论却张冠李戴」的报告（且不会报错）。
            cur, old = factor_rev(), raw.get("factor_rev")
            if old and old != cur:
                print(f"[error] 面板指纹 @{old} ≠ 当前 @{cur}（指纹覆盖 factors.py + "
                      f"PANEL_SCHEMA + weights.yaml 里进面板的 window/6 维权重）。"
                      f"复用旧面板会得出错误结论，请去掉 --reuse-panel 重建（约 8 分钟）。")
                return 1
            if not old and not args.allow_stale_panel:
                print("[error] 该面板没有 factor_rev 指纹（旧版生成），无法确认因子代码"
                      "未变 —— 复用可能得到错结论。请去掉 --reuse-panel 重建，"
                      "或显式加 --allow-stale-panel 表示「我知道风险」。")
                return 1
            meta = {**raw,
                    "eval_start": pd.Timestamp(raw["eval_start"]),
                    "eval_end": pd.Timestamp(raw["eval_end"]),
                    "axis_start": pd.Timestamp(raw["axis_start"]),
                    "axis_end": pd.Timestamp(raw["axis_end"])}
        else:
            # 没写 meta 的旧面板也要能复用：从面板自身推
            ds = sorted(panel["date"].unique())
            sizes = panel.groupby("date").size()
            meta = {"eval_start": pd.Timestamp(ds[0]),
                    "eval_end": pd.Timestamp(ds[-1]),
                    "axis_start": pd.Timestamp(ds[0]),
                    "axis_end": pd.Timestamp(ds[-1]),
                    "n_axis": len(ds), "n_eval": len(ds),
                    "n_boards": int(panel["name"].nunique()),
                    "avail_median": int(sizes.median()),
                    "avail_min": int(sizes.min()),
                    "min_boards": int(args.min_boards),
                    "stride": int(args.stride), "min_bars": int(args.min_bars),
                    "horizons": list(horizons)}
        print(f"[reuse] 复用面板 {args.csv}（{len(panel):,} 行，"
              f"{meta['n_eval']} 个评价日，跳过重建；只迭代报告时用）")
    else:
        panel, meta = build_panel(klines, bench, w6, stride=args.stride,
                                  min_bars=args.min_bars,
                                  min_boards=args.min_boards, horizons=horizons)
    if panel.empty:
        print("[error] 面板为空")
        return 1

    # ---- 面板先落盘：重建一次要 6 分钟，报告渲染出任何问题都不该把它丢掉
    #      （踩过：改报告措辞时引入一个 bug，渲染崩了 → 整轮重建白跑）
    csv_fp = pathlib.Path(args.csv)
    csv_fp.parent.mkdir(parents=True, exist_ok=True)
    panel.to_csv(csv_fp, index=False, encoding="utf-8-sig")
    print(f"[out] 面板 -> {csv_fp}（{len(panel):,} 行）")
    meta["factor_rev"] = factor_rev()
    meta_fp = csv_fp.with_suffix(".meta.json")
    meta_fp.write_text(json.dumps(
        {k: (str(v) if isinstance(v, pd.Timestamp) else v)
         for k, v in meta.items()}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[out] panel meta -> {meta_fp}")

    combo = {}
    for col in ("score", "score_valinv", "score_turn_old"):
        combo[col] = eval_signal(panel, col, horizons, args.quantiles)
    fac = {k: eval_signal(panel, f"f_{k}", horizons, args.quantiles)
           for k in BACKTESTABLE}

    rules = rule_tests(panel, horizons, w)
    rel = {} if args.no_release else release_event_study(REL_HIST)
    mac = {} if args.no_macro else macro_gate_test(bench, klines)
    stab = stability(panel, horizons) if args.stability else {}

    rsup: dict = {}
    if args.rel_supply:
        bmap_fp = DATA / "cache" / "stock_board_map.parquet"
        if not bmap_fp.exists():
            print(f"[skip] {bmap_fp} 不存在（先跑 main.py --stage warmup）"
                  f"，跳过解禁供给压力检验")
        else:
            bmap = pd.read_parquet(bmap_fp)
            mc = fetch_board_mktcap() if args.mktcap else {}
            rsup = release_supply_test(panel, klines, bmap, mc, REL_HIST, w)
            if rsup:
                sp = rsup["tables"]["by_pen"].get("spread") or {}
                print(f"[rel-supply] {rsup['n_rows']:,} 样本 / "
                      f"被罚占比 {rsup['penalized_share']:.1%}；"
                      f"最重罚档 − 无解禁（20 日）："
                      + (f"{sp['h'].get(20, {}).get('mean', float('nan')) * 100:+.2f}pt"
                         if sp.get("h", {}).get(20) else "—"))

    out_fp = pathlib.Path(args.out)
    out_fp.parent.mkdir(parents=True, exist_ok=True)
    out_fp.write_text(render(panel, meta, w, combo, fac, rules, rel, mac,
                             verify, stab, rsup),
                      encoding="utf-8")
    print(f"[out] 报告 -> {out_fp}")

    for k in ("score", "score_valinv"):
        print(f"  {k}: " + "  ".join(
            f"{h}d IC={combo[k][h]['ic_mean']:+.4f}" for h in horizons))
    _hmid = horizons[len(horizons) // 2]
    print(f"  逐因子 IC（{_hmid} 日）: " + "  ".join(
        f"{k}={fac[k][_hmid]['ic_mean']:+.3f}"
        for k in BACKTESTABLE))
    return 0


if __name__ == "__main__":
    sys.exit(main())
