#!/usr/bin/env python3
"""评分有效性回测

读 web/data/archive/ 下的历史评分快照 + web/data/cache/ 的板块K线，
计算评分对未来收益的预测能力，用于校准 12 维权重的先验值。

评估指标
  1. IC      : 每个快照日，评分与未来 N 日收益的 Spearman 秩相关
  2. ICIR    : mean(IC) / std(IC)，衡量稳定性
  3. 分层收益 : 按评分分 5 档，各档未来 N 日平均收益（应单调）
  4. 多空胜率 : Top 档 − Bottom 档 为正的日期占比
  5. 多空净值 : Top − Bottom 累计收益曲线

用法:
    python pipeline/backtest.py
    python pipeline/backtest.py --horizons 1,5,10,20 --min-boards 20
    python pipeline/backtest.py --out docs/backtest_report.md --csv web/data/backtest.csv

注意：需要至少 2 个不同交易日的评分快照（且快照日期与K线能对齐）才有意义。
     快照会随每日运行自动累积，因此该脚本的价值随时间增长。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import sys

import numpy as np
import pandas as pd

ROOT = pathlib.Path(__file__).resolve().parents[1]
DATA = ROOT / "web" / "data"
ARCHIVE = DATA / "archive"
CACHE = DATA / "cache"


# ------------------------------------------------------------------ 载入
def load_snapshots(archive_dir: pathlib.Path = ARCHIVE) -> list[dict]:
    """读取归档快照，返回 [{date, code->score, name->..., macro}]，按日期升序"""
    if not archive_dir.exists():
        return []
    out: list[dict] = []
    for fp in sorted(archive_dir.rglob("*.json")):
        try:
            obj = json.loads(fp.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        boards = obj.get("boards")
        if not boards:
            continue
        updated = obj.get("updated") or fp.stem
        try:
            date = pd.Timestamp(updated).normalize()
        except Exception:  # noqa: BLE001
            date = pd.Timestamp(fp.stem[:8])
        out.append({
            "date": date,
            "file": fp.name,
            "intraday": bool(obj.get("intraday")),
            "macro_score": obj.get("macro_score"),
            "scores": {str(b["code"]): float(b["score"]) for b in boards},
            "names": {str(b["code"]): str(b.get("name", "")) for b in boards},
        })
    # 同一天多次运行（盘中+收盘）只保留最后一次
    dedup: dict[pd.Timestamp, dict] = {}
    for s in out:
        dedup[s["date"]] = s
    return [dedup[k] for k in sorted(dedup)]


def load_klines(cache_dir: pathlib.Path = CACHE) -> dict[str, pd.DataFrame]:
    """读取板块K线缓存，返回 {code: DataFrame(date, close)}"""
    out: dict[str, pd.DataFrame] = {}
    if not cache_dir.exists():
        return out
    for fp in cache_dir.glob("*.parquet"):
        try:
            df = pd.read_parquet(fp)
            if "date" not in df.columns or "close" not in df.columns:
                continue
            df = df.copy()
            df["date"] = pd.to_datetime(df["date"]).dt.normalize()
            out[fp.stem] = df[["date", "close"]].sort_values("date").reset_index(drop=True)
        except Exception:  # noqa: BLE001
            continue
    return out


# ------------------------------------------------------------------ 统计
def _rank(x: np.ndarray) -> np.ndarray:
    """秩次（返回可写副本：pandas>=2.0 的 to_numpy 可能是只读视图）"""
    return np.array(pd.Series(x).rank().to_numpy(dtype=float), dtype=float, copy=True)


def spearman(a, b) -> float:
    """Spearman 秩相关（不依赖 scipy）"""
    a = np.asarray(a, dtype=float)
    b = np.asarray(b, dtype=float)
    mask = np.isfinite(a) & np.isfinite(b)
    a, b = a[mask], b[mask]
    if len(a) < 3:
        return float("nan")
    ra = _rank(a)
    rb = _rank(b)
    ra = ra - ra.mean()          # 用非原地运算，避免只读数组报错
    rb = rb - rb.mean()
    denom = np.sqrt((ra ** 2).sum() * (rb ** 2).sum())
    return float((ra * rb).sum() / denom) if denom else float("nan")


def forward_return(df: pd.DataFrame, as_of: pd.Timestamp, horizon: int) -> float | None:
    """从 as_of（含）之后第 horizon 个交易日的收益；数据不足返回 None"""
    if df is None or df.empty:
        return None
    idx = df.index[df["date"] <= as_of]
    if len(idx) == 0:
        return None
    i = int(idx[-1])
    j = i + horizon
    if j >= len(df):
        return None
    p0, p1 = float(df["close"].iloc[i]), float(df["close"].iloc[j])
    if p0 <= 0:
        return None
    return p1 / p0 - 1.0


def evaluate(snapshots: list[dict], klines: dict[str, pd.DataFrame],
             horizons: list[int], quantiles: int = 5,
             min_boards: int = 20) -> tuple[dict, pd.DataFrame]:
    """返回 (指标汇总, 明细 DataFrame)"""
    rows: list[dict] = []
    used_dates: list[pd.Timestamp] = []

    for snap in snapshots:
        codes = [c for c in snap["scores"] if c in klines]
        if len(codes) < min_boards:
            continue
        scores = np.array([snap["scores"][c] for c in codes], dtype=float)
        if np.isfinite(scores).sum() < min_boards:
            continue
        rec = {"date": snap["date"], "n": len(codes), "macro_score": snap["macro_score"]}
        ok = False
        for h in horizons:
            rets = np.array([forward_return(klines[c], snap["date"], h) or np.nan
                             for c in codes], dtype=float)
            rec[f"ic_{h}d"] = spearman(scores, rets)
            if np.isfinite(rets).sum() >= min_boards:
                ok = True
                # 分层
                try:
                    q = pd.qcut(scores, quantiles, labels=False, duplicates="drop")
                    for k in range(quantiles):
                        m = (q == k) & np.isfinite(rets)
                        rec[f"q{k + 1}_{h}d"] = float(rets[m].mean()) if m.sum() else np.nan
                    rec[f"ls_{h}d"] = rec.get(f"q{quantiles}_{h}d", np.nan) - rec.get(f"q1_{h}d", np.nan)
                except Exception:  # noqa: BLE001
                    pass
        if ok:
            rows.append(rec)
            used_dates.append(snap["date"])

    detail = pd.DataFrame(rows)
    summary: dict = {"n_dates": len(detail), "horizons": horizons,
                     "quantiles": quantiles, "metrics": {}}
    if detail.empty:
        return summary, detail

    for h in horizons:
        ic_col, ls_col = f"ic_{h}d", f"ls_{h}d"
        ics = detail[ic_col].dropna() if ic_col in detail else pd.Series(dtype=float)
        m: dict = {"ic_mean": float(ics.mean()) if len(ics) else float("nan"),
                   "ic_std": float(ics.std(ddof=1)) if len(ics) > 1 else float("nan"),
                   "ic_positive_rate": float((ics > 0).mean()) if len(ics) else float("nan")}
        m["icir"] = (m["ic_mean"] / m["ic_std"]
                     if m["ic_std"] and np.isfinite(m["ic_std"]) and m["ic_std"] > 0 else float("nan"))
        tiers = {}
        for k in range(quantiles):
            col = f"q{k + 1}_{h}d"
            tiers[f"Q{k + 1}"] = float(detail[col].mean()) if col in detail else float("nan")
        m["tiers"] = tiers
        if ls_col in detail:
            ls = detail[ls_col].dropna()
            m["ls_mean"] = float(ls.mean()) if len(ls) else float("nan")
            m["ls_win_rate"] = float((ls > 0).mean()) if len(ls) else float("nan")
            m["ls_curve"] = float((1 + ls).prod() - 1) if len(ls) else float("nan")
        m["monotonic"] = _is_monotonic([tiers[f"Q{k + 1}"] for k in range(quantiles)])
        summary["metrics"][f"{h}d"] = m
    return summary, detail


def _is_monotonic(vals: list[float]) -> bool | None:
    v = [x for x in vals if np.isfinite(x)]
    if len(v) < 3:
        return None
    return all(v[i] <= v[i + 1] for i in range(len(v) - 1))


# ------------------------------------------------------------------ 报告
def render_report(summary: dict, detail: pd.DataFrame,
                  start: pd.Timestamp | None = None) -> str:
    L: list[str] = []
    L.append("# 评分有效性回测报告")
    L.append("")
    L.append(f"> 生成时间：{dt.datetime.now():%Y-%m-%d %H:%M}　|　"
             f"样本快照数：{summary['n_dates']}")
    L.append("")
    if summary["n_dates"] < 2:
        L.append("## ⚠️ 样本不足，无法评估")
        L.append("")
        L.append(f"当前仅 {summary['n_dates']} 个可用快照日（要求 ≥ 2，且每期至少 "
                 f"{20} 个板块与K线对齐）。")
        L.append("")
        L.append("原因通常是：")
        L.append("")
        L.append("1. `web/data/archive/` 积累的评分快照还不够（每运行一次 score 归档一次）")
        L.append("2. 快照日期晚于K线缓存的最新日期（需先跑 `--stage warmup` 补K线）")
        L.append("")
        L.append("**建议**：先让每日定时任务跑 2–4 周，再回来执行本脚本。")
        L.append("")
        L.append("## 判读标准（供未来参考）")
        L.append("")
        L.append("| 指标 | 良好 | 优秀 | 含义 |")
        L.append("|---|---|---|---|")
        L.append("| IC 均值 | > 0.03 | > 0.06 | 评分与未来收益的秩相关 |")
        L.append("| ICIR | > 0.3 | > 0.5 | IC 的稳定性 |")
        L.append("| 分层单调 | 5 档递增 | — | 分数越高收益越高 |")
        L.append("| 多空胜率 | > 55% | > 60% | Top−Bottom 为正的频率 |")
        L.append("")
        return "\n".join(L)

    L.append(f"> 明细区间：{detail['date'].min():%Y-%m-%d} ~ {detail['date'].max():%Y-%m-%d}")
    L.append("")
    L.append("## 一、汇总指标")
    L.append("")
    L.append("| 持有期 | IC 均值 | IC 标准差 | ICIR | IC>0 占比 | 多空均值 | 多空胜率 | 多空累计 | 分层单调 |")
    L.append("|---|---|---|---|---|---|---|---|---|")
    for k, m in summary["metrics"].items():
        def f(x, pct=False):
            if x is None or not np.isfinite(x):
                return "—"
            return f"{x * 100:.2f}%" if pct else f"{x:.3f}"
        L.append(f"| {k} | {f(m['ic_mean'])} | {f(m['ic_std'])} | {f(m['icir'])} | "
                 f"{f(m['ic_positive_rate'], True)} | {f(m.get('ls_mean'), True)} | "
                 f"{f(m.get('ls_win_rate'), True)} | {f(m.get('ls_curve'), True)} | "
                 f"{'✅' if m.get('monotonic') else ('❌' if m.get('monotonic') is False else '—')} |")
    L.append("")
    L.append("## 二、分层收益（按评分从低到高分 5 档）")
    L.append("")
    qn = summary["quantiles"]
    L.append("| 持有期 | " + " | ".join(f"Q{i + 1}" + ("（低）" if i == 0 else "（高）" if i == qn - 1 else "")
                                        for i in range(qn)) + " |")
    L.append("|---" * (qn + 1) + "|")
    for k, m in summary["metrics"].items():
        cells = []
        for i in range(qn):
            v = m["tiers"].get(f"Q{i + 1}")
            cells.append("—" if v is None or not np.isfinite(v) else f"{v * 100:+.2f}%")
        L.append(f"| {k} | " + " | ".join(cells) + " |")
    L.append("")
    L.append("## 三、逐期明细")
    L.append("")
    cols = [c for c in detail.columns if c != "date"]
    L.append("| 日期 | " + " | ".join(c.replace("_", " ") for c in cols) + " |")
    L.append("|---" * (len(cols) + 1) + "|")
    for _, r in detail.iterrows():
        cells = []
        for c in cols:
            v = r[c]
            if isinstance(v, (float, np.floating)):
                cells.append("—" if not np.isfinite(v) else f"{v:+.2f}%"
                             if ("q" in c or "ls" in c) else f"{v:.3f}")
            else:
                cells.append(str(v))
        L.append(f"| {r['date']:%Y-%m-%d} | " + " | ".join(cells) + " |")
    L.append("")
    return "\n".join(L)


# ------------------------------------------------------------------ main
def main() -> int:
    ap = argparse.ArgumentParser(description="评分有效性回测")
    ap.add_argument("--horizons", default="1,5,10,20", help="持有期（交易日），逗号分隔")
    ap.add_argument("--quantiles", type=int, default=5)
    ap.add_argument("--min-boards", type=int, default=20)
    ap.add_argument("--archive", default=str(ARCHIVE))
    ap.add_argument("--cache", default=str(CACHE))
    ap.add_argument("--out", default=str(ROOT / "docs" / "backtest_report.md"))
    ap.add_argument("--csv", default=str(DATA / "backtest_detail.csv"))
    args = ap.parse_args()

    horizons = [int(x) for x in str(args.horizons).split(",") if x.strip()]

    snaps = load_snapshots(pathlib.Path(args.archive))
    klines = load_klines(pathlib.Path(args.cache))
    print(f"[backtest] 快照 {len(snaps)} 个，K线 {len(klines)} 个板块")

    summary, detail = evaluate(snaps, klines, horizons, args.quantiles, args.min_boards)

    out_fp = pathlib.Path(args.out)
    out_fp.parent.mkdir(parents=True, exist_ok=True)
    out_fp.write_text(render_report(summary, detail), encoding="utf-8")
    print(f"[backtest] 报告 -> {out_fp}")

    if not detail.empty:
        csv_fp = pathlib.Path(args.csv)
        csv_fp.parent.mkdir(parents=True, exist_ok=True)
        detail.to_csv(csv_fp, index=False, encoding="utf-8-sig")
        print(f"[backtest] 明细 -> {csv_fp}")
        for k, m in summary["metrics"].items():
            ic = m["ic_mean"]
            print(f"  {k}: IC={ic:.4f} ICIR={m['icir']:.3f} "
                  f"多空胜率={m.get('ls_win_rate', float('nan')):.2%} "
                  f"单调={'Y' if m.get('monotonic') else 'N'}")
    else:
        print("[backtest] 样本不足，仅输出说明性报告")
    return 0


if __name__ == "__main__":
    sys.exit(main())
