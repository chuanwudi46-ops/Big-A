"""筹码供给压力：解禁 + 减持

对应帽子哥「解禁筹码供给分析」：
"解禁是悬在科技头上的一把刀" —— 解禁前不碰，全部出清后的黄金坑才是抄底时点。

归属方式由「行业名模糊匹配」升级为「个股 ∩ 板块成分股」精确关联：
  1. 遍历板块成分股，构建 stock_code → [board_code] 映射（缓存 parquet，7 天刷新）
  2. 解禁/减持明细按 stock_code 汇总到板块
  3. 解禁市值 / 板块总市值 → 解禁压力比

接口字段名可能变动，故全部用候选列名匹配；匹配不到则退化为 0（不阻断主流程）。
"""
from __future__ import annotations

import datetime as dt
import pathlib

import numpy as np
import pandas as pd

import sources

ROOT = pathlib.Path(__file__).resolve().parents[1]
CACHE_DIR = ROOT / "web" / "data" / "cache"
MAP_FP = CACHE_DIR / "stock_board_map.parquet"
MAP_MAX_AGE_DAYS = 7

CODE_CANDS = ("股票代码", "代码", "证券代码", "股票代码(带后缀)", "股票代码-带后缀")
RELEASE_MV_CANDS = ("实际解禁市值", "解禁市值", "解禁金额", "实际解禁金额")
RELEASE_QTY_CANDS = ("实际解禁数量", "解禁数量", "解禁股数")
RELEASE_DATE_CANDS = ("解禁时间", "解禁日期", "解禁公告日期")
SHARE_QTY_CANDS = ("变动股数", "变动数量", "增减持股数", "变动股份")
SHARE_DATE_CANDS = ("变动日期", "公告日期", "日期", "截止日期")
SHARE_DIR_CANDS = ("变动方向", "增减持方向", "方向")


def _pick(df: pd.DataFrame, cands: tuple[str, ...]) -> str | None:
    for c in cands:
        if c in df.columns:
            return c
    return None


def _norm_code(x) -> str | None:
    """'002985.SZ' / '002985' / 2985 → '002985'"""
    if x is None:
        return None
    s = str(x).strip().split(".")[0]
    s = "".join(ch for ch in s if ch.isdigit())
    if not s:
        return None
    return s.zfill(6) if len(s) <= 6 else s


def _to_dates(s: pd.Series) -> pd.Series:
    return pd.to_datetime(s, errors="coerce")


# ==========================================================================
# 个股 → 板块 映射（构建 / 缓存 / 读取）
# ==========================================================================
def load_map(max_age_days: int = MAP_MAX_AGE_DAYS) -> pd.DataFrame:
    """读取缓存的映射表；过期或不存在返回空表（由调用方决定是否重建）"""
    if not MAP_FP.exists():
        return pd.DataFrame()
    age = dt.datetime.now() - dt.datetime.fromtimestamp(MAP_FP.stat().st_mtime)
    if age.days > max_age_days:
        return pd.DataFrame()
    try:
        return pd.read_parquet(MAP_FP)
    except Exception as e:  # noqa: BLE001
        print(f"[warn] read board map: {e}")
        return pd.DataFrame()


def build_map(boards: list[dict], force: bool = False) -> pd.DataFrame:
    """构建并缓存映射表（板块成分股并集）"""
    if not force:
        cached = load_map()
        if not cached.empty:
            print(f"[chips] 复用板块映射缓存（{len(cached)} 条）")
            return cached

    print(f"[chips] 构建个股-板块映射，共 {len(boards)} 个板块 …")
    df = sources.stock_board_map(boards)
    if df.empty:
        print("[warn] 映射表为空（成分股接口不可用），筹码维度将退化为 0")
        return df
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        df.to_parquet(MAP_FP, index=False)
        print(f"[chips] 映射表已缓存：{len(df)} 条 -> {MAP_FP}")
    except Exception as e:  # noqa: BLE001
        print(f"[warn] write board map: {e}")
    return df


def _boards_of(code: str | None, bmap: pd.DataFrame) -> list[str]:
    if not code or bmap is None or bmap.empty:
        return []
    sub = bmap.loc[bmap["stock_code"] == code, "board_code"]
    return sub.astype(str).tolist()


# ==========================================================================
# 解禁压力
# ==========================================================================
def release_pressure(release_df: pd.DataFrame, bmap: pd.DataFrame,
                     horizon_days: int = 30,
                     as_of: dt.date | None = None) -> dict[str, float]:
    """返回 {board_code: 归一化解禁压力 [0,1]}

    压力 = Σ(未来 horizon 内解禁市值) / 该板块当前总市值
    分级：ratio >= 5% 视为满级压力。
    """
    out: dict[str, float] = {}
    if (release_df is None or release_df.empty
            or bmap is None or bmap.empty):
        return out

    as_of = as_of or dt.date.today()
    end = as_of + dt.timedelta(days=horizon_days)

    df = release_df.copy()
    code_col = _pick(df, CODE_CANDS)
    mv_col = _pick(df, RELEASE_MV_CANDS)
    qty_col = _pick(df, RELEASE_QTY_CANDS)
    date_col = _pick(df, RELEASE_DATE_CANDS)
    if code_col is None or (mv_col is None and qty_col is None):
        print("[warn] 解禁明细缺少代码/市值列，无法按板块归属")
        return out

    df["_code"] = df[code_col].map(_norm_code)
    df = df[df["_code"].notna()]

    if date_col:
        d = _to_dates(df[date_col])
        df = df[(d >= pd.Timestamp(as_of)) & (d <= pd.Timestamp(end))]

    val_col = mv_col or qty_col
    df["_val"] = pd.to_numeric(df[val_col], errors="coerce").fillna(0.0)
    # 若使用的是股数而非市值，按 10 元近似折算（仅作量级归一，避免除零）
    if mv_col is None:
        df["_val"] = df["_val"] * 10.0

    agg = df.groupby("_code")["_val"].sum()

    per_board: dict[str, float] = {}
    for code, val in agg.items():
        for bc in _boards_of(code, bmap):
            per_board[bc] = per_board.get(bc, 0.0) + float(val)

    for bc, val in per_board.items():
        out[bc] = float(min(val, 1e15))       # 原始金额，归一化在 main 里用市值完成
    return out


def release_ratio(board_code: str, board_mktcap: float | None,
                  raw: dict[str, float]) -> tuple[float, dict]:
    """把解禁金额换算成占板块总市值的**原始比例**

    注意：这里返回的是原始比例（如 0.025 = 2.5%），不是归一化分值。
    归一化统一由 factors.chip_supply() 完成（阈值 5%），避免双重归一化。
    """
    val = raw.get(str(board_code), 0.0)
    if not val or not board_mktcap or board_mktcap <= 0:
        return 0.0, {"release_mv": 0.0, "ratio": None, "norm": 0.0}
    ratio = val / float(board_mktcap)
    return float(ratio), {
        "release_mv_yi": round(val / 1e8, 1),
        "ratio": round(ratio, 5),
        "norm": round(min(ratio / 0.05, 1.0), 3),
    }



# ==========================================================================
# 减持压力
# ==========================================================================
def reduction_counts(share_df: pd.DataFrame, bmap: pd.DataFrame,
                     days: int = 30,
                     as_of: dt.date | None = None) -> dict[str, int]:
    """返回 {board_code: 近 days 日减持记录数}"""
    out: dict[str, int] = {}
    if share_df is None or share_df.empty or bmap is None or bmap.empty:
        return out

    as_of = as_of or dt.date.today()
    df = share_df.copy()

    code_col = _pick(df, CODE_CANDS)
    qty_col = _pick(df, SHARE_QTY_CANDS)
    date_col = _pick(df, SHARE_DATE_CANDS)
    dir_col = _pick(df, SHARE_DIR_CANDS)
    if code_col is None or (qty_col is None and dir_col is None):
        print("[warn] 持股变动明细缺少代码/股数/方向列，减持维度退化为 0")
        return out

    df["_code"] = df[code_col].map(_norm_code)
    df = df[df["_code"].notna()]

    if date_col:
        d = _to_dates(df[date_col])
        df = df[d >= pd.Timestamp(as_of - dt.timedelta(days=days))]

    if dir_col:
        is_red = df[dir_col].astype(str).str.contains("减", na=False)
    elif qty_col:
        is_red = pd.to_numeric(df[qty_col], errors="coerce").fillna(0) < 0
    else:
        return out
    df = df[is_red]
    if df.empty:
        return out

    for code in df["_code"].unique():
        for bc in _boards_of(code, bmap):
            out[bc] = out.get(bc, 0) + int((df["_code"] == code).sum())
    return out
