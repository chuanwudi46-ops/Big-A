"""免费数据源适配层

设计原则
1. 每个函数内部做重试（免费接口本质是爬虫，偶发失败是常态）
2. 单点失败一律降级返回空表/空值，绝不中断整体评分流程
3. **双通道**：优先 akshare（列名稳定），失败时回退到东方财富公开接口直连。
   实测 akshare 部分接口硬编码分片域名（如 17.push2.eastmoney.com），
   在部分网络环境下不可达，直连兜底可显著提升可用性。
"""
from __future__ import annotations

import os
import time
import datetime as dt

import pandas as pd
import requests

RETRY = 3
SLEEP = 1.5

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

_session: requests.Session | None = None


def _sess() -> requests.Session:
    global _session
    if _session is None:
        _session = requests.Session()
        _session.headers.update({"User-Agent": UA, "Referer": "https://quote.eastmoney.com/"})
    return _session


def _retry(fn, *args, **kwargs):
    err = None
    for i in range(RETRY):
        try:
            return fn(*args, **kwargs)
        except Exception as e:  # noqa: BLE001
            err = e
            time.sleep(SLEEP * (i + 1))
    raise RuntimeError(f"{getattr(fn, '__name__', fn)} failed after {RETRY} tries: {err}")


def _ak():
    """延迟导入 akshare，便于在未安装时仍能使用直连通道"""
    import akshare as ak
    return ak


# --------------------------------------------------------------------------
# 东方财富直连通道
# --------------------------------------------------------------------------
def _push2_clist(fs: str, fields: str, pz: int = 100) -> list[dict]:
    """板块/个股列表直连（push2）。返回原始 diff 列表"""
    rows: list[dict] = []
    pn = 1
    while True:
        url = ("https://push2.eastmoney.com/api/qt/clist/get"
               f"?pn={pn}&pz={pz}&po=1&np=1&fltt=2&invt=2&fid=f3&fs={fs}&fields={fields}")
        r = _sess().get(url, timeout=15)
        r.raise_for_status()
        diff = (r.json().get("data") or {}).get("diff") or []
        if not diff:
            break
        rows.extend(diff)
        if len(diff) < pz:
            break
        pn += 1
    return rows


def _direct_board_snapshot() -> pd.DataFrame:
    """直连获取行业板块快照（含主力净流入），一次请求拿全"""
    fields = "f2,f3,f8,f12,f14,f20,f62,f104,f105,f128,f184"
    rows = _push2_clist("m:90+t:2+f:!50", fields)
    df = pd.DataFrame(rows).rename(columns={
        "f12": "code", "f14": "name", "f3": "pct", "f8": "turnover",
        "f20": "mktcap", "f104": "up", "f105": "down", "f128": "leader",
        "f62": "main_inflow", "f184": "main_inflow_pct",
    })
    if df.empty:
        raise RuntimeError("direct board snapshot empty")
    df["code"] = df["code"].astype(str)
    return df[[c for c in ["code", "name", "pct", "turnover", "up", "down",
                           "mktcap", "leader", "main_inflow", "main_inflow_pct"]
               if c in df.columns]]


def _direct_board_hist(secid: str, start: str, end: str, limit: int = 600) -> pd.DataFrame:
    """直连获取板块日 K 线。secid 形如 90.BK0475"""
    url = ("https://push2his.eastmoney.com/api/qt/stock/kline/get"
           f"?secid={secid}"
           "&fields1=f1,f2,f3,f4,f5,f6"
           "&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"
           f"&klt=101&fqt=1&beg={start}&end={end}&lmt={limit}")
    r = _sess().get(url, timeout=20)
    r.raise_for_status()
    klines = (r.json().get("data") or {}).get("klines") or []
    if not klines:
        raise RuntimeError(f"direct kline empty for {secid}")

    # 字段顺序：日期,开,收,高,低,成交量,成交额,振幅,涨跌幅,涨跌额,换手率
    recs = []
    for line in klines:
        p = line.split(",")
        recs.append({
            "date": p[0], "open": float(p[1]), "close": float(p[2]),
            "high": float(p[3]), "low": float(p[4]),
            "volume": float(p[5]), "amount": float(p[6]),
            "pct": float(p[8]), "turnover": float(p[10]),
        })
    return pd.DataFrame(recs)


def _direct_news(limit: int = 100) -> pd.DataFrame:
    """直连获取东财 7x24 快讯"""
    url = ("https://np-listapi.eastmoney.com/comm/web/getFastNewsList"
           f"?client=web&biz=web_724&fastColumn=102&sortEnd=&pageSize={limit}&req_trace=1")
    r = _sess().get(url, timeout=15)
    r.raise_for_status()
    items = (r.json().get("data") or {}).get("fastNewsList") or []
    if not items:
        raise RuntimeError("direct news empty")
    df = pd.DataFrame([{
        "title": it.get("title"),
        "content": it.get("summary"),
        "time": it.get("showTime"),
    } for it in items])
    df["time"] = pd.to_datetime(df["time"], errors="coerce")
    return df.dropna(subset=["time"])


# --------------------------------------------------------------------------
# 板块
# --------------------------------------------------------------------------
def board_snapshot() -> pd.DataFrame:
    """行业板块当日快照：涨跌幅 / 换手率 / 涨跌家数 / 主力净流入占比"""
    try:
        ak = _ak()
        df = _retry(ak.stock_board_industry_name_em).rename(columns={
            "板块代码": "code", "板块名称": "name", "涨跌幅": "pct",
            "换手率": "turnover", "上涨家数": "up", "下跌家数": "down",
            "总市值": "mktcap", "领涨股票": "leader",
        })
        df = df[[c for c in ["code", "name", "pct", "turnover", "up", "down",
                             "mktcap", "leader"] if c in df.columns]].copy()
        df["code"] = df["code"].astype(str)
        try:
            ff = _retry(ak.stock_sector_fund_flow_rank,
                        indicator="今日", sector_type="行业资金流")
            ff = ff.rename(columns={
                "名称": "name",
                "今日主力净流入-净额": "main_inflow",
                "今日主力净流入-净占比": "main_inflow_pct",
            })
            cols = [c for c in ["name", "main_inflow", "main_inflow_pct"] if c in ff.columns]
            df = df.merge(ff[cols], on="name", how="left")
        except Exception as e:  # noqa: BLE001
            print(f"[warn] akshare fund_flow: {e}")
        if "main_inflow_pct" not in df.columns:
            raise RuntimeError("akshare 未返回资金流，改用直连")
        return df
    except Exception as e:  # noqa: BLE001
        print(f"[info] akshare 板块快照不可用（{e}），切换直连通道")
        return _direct_board_snapshot()


def board_hist(name: str, start: str, end: str, code: str | None = None) -> pd.DataFrame:
    """板块日 K 线。start/end 格式 YYYYMMDD；code 用于直连兜底（如 BK0475）"""
    try:
        ak = _ak()
        df = _retry(ak.stock_board_industry_hist_em, symbol=name,
                    start_date=start, end_date=end, period="日k", adjust="")
        df = df.rename(columns={
            "日期": "date", "开盘": "open", "收盘": "close", "最高": "high",
            "最低": "low", "成交量": "volume", "成交额": "amount",
            "涨跌幅": "pct", "换手率": "turnover",
        })
        cols = [c for c in ["date", "open", "close", "high", "low",
                            "volume", "amount", "pct", "turnover"] if c in df.columns]
        if not df.empty:
            return df[cols].copy()
        raise RuntimeError("akshare 返回空")
    except Exception as e:  # noqa: BLE001
        if not code:
            raise
        print(f"[info] akshare K线不可用（{e}），切换直连：{name}")
        return _direct_board_hist(f"90.{code}", start, end)


def board_cons(name: str) -> pd.DataFrame:
    """板块成分股（用于共振度计算）。失败返回空表"""
    try:
        return _retry(_ak().stock_board_industry_cons_em, symbol=name)
    except Exception as e:  # noqa: BLE001
        print(f"[warn] cons {name}: {e}")
        return pd.DataFrame()


# --------------------------------------------------------------------------
# 新闻
# --------------------------------------------------------------------------
def fetch_news() -> pd.DataFrame:
    """财联社电报 + 东财 7x24，合并去重，返回 [title, content, time]"""
    frames = []

    try:
        ak = _ak()
        frames.append(_retry(ak.stock_info_global_cls).rename(
            columns={"标题": "title", "内容": "content", "发布时间": "time"}))
    except Exception as e:  # noqa: BLE001
        print(f"[warn] cls news: {e}")

    try:
        frames.append(_retry(_ak().stock_info_global_em).rename(
            columns={"标题": "title", "摘要": "content", "发布时间": "time"}))
    except Exception as e:  # noqa: BLE001
        print(f"[warn] em news: {e}")

    if not frames:
        try:
            frames.append(_direct_news())
            print("[info] 新闻使用直连通道")
        except Exception as e:  # noqa: BLE001
            print(f"[warn] direct news: {e}")

    if not frames:
        return pd.DataFrame(columns=["title", "content", "time"])

    df = pd.concat(frames, ignore_index=True)
    df["time"] = pd.to_datetime(df["time"], errors="coerce")
    df = (df.dropna(subset=["time"])
            .drop_duplicates(subset=["title"])
            .sort_values("time", ascending=False)
            .reset_index(drop=True))
    return df[["title", "content", "time"]]


# --------------------------------------------------------------------------
# 筹码（解禁 / 减持）
# --------------------------------------------------------------------------
def release_calendar(start: str, end: str) -> pd.DataFrame:
    """限售解禁明细，start/end 格式 YYYYMMDD"""
    try:
        return _retry(_ak().stock_restricted_release_detail_em,
                      start_date=start, end_date=end)
    except Exception as e:  # noqa: BLE001
        print(f"[warn] release calendar: {e}")
        return pd.DataFrame()


def reduction_detail() -> pd.DataFrame:
    """高管及股东持股变动（用于减持压力计数）"""
    try:
        return _retry(_ak().stock_hold_management_detail_em)
    except Exception as e:  # noqa: BLE001
        print(f"[warn] reduction detail: {e}")
        return pd.DataFrame()


# --------------------------------------------------------------------------
# 估值（tushare 可选）
# --------------------------------------------------------------------------
def sw_valuation(ts_code: str, start: str, end: str) -> pd.DataFrame:
    """申万行业指数估值（PE_TTM / PB）。需环境变量 TUSHARE_TOKEN"""
    token = os.getenv("TUSHARE_TOKEN")
    if not token:
        raise RuntimeError("TUSHARE_TOKEN not set")
    import tushare as ts

    pro = ts.pro_api(token)
    return pro.index_dailybasic(ts_code=ts_code, start_date=start, end_date=end,
                                fields="trade_date,pe_ttm,pb")


# --------------------------------------------------------------------------
# 宏观 / 基准
# --------------------------------------------------------------------------
def index_daily(symbol: str = "sh000300") -> pd.DataFrame:
    """指数日线，用于相对强弱基准"""
    try:
        return _retry(_ak().stock_zh_index_daily, symbol=symbol)
    except Exception as e:  # noqa: BLE001
        print(f"[warn] index_daily: {e}")
        secid = {"sh000300": "1.000300", "sz399006": "0.399006",
                 "sh000001": "1.000001"}.get(symbol)
        if not secid:
            raise
        return _direct_board_hist(secid, "20200101", "20500101", limit=1200)


def margin_balance() -> pd.DataFrame:
    """两融余额（宏观流动性代理）"""
    try:
        end = dt.date.today().strftime("%Y%m%d")
        return _retry(_ak().stock_margin_sse, start_date="20240101", end_date=end)
    except Exception as e:  # noqa: BLE001
        print(f"[warn] margin balance: {e}")
        return pd.DataFrame()


# --------------------------------------------------------------------------
# 交易日历
# --------------------------------------------------------------------------
def trade_dates() -> list[str]:
    """A 股历史交易日列表（YYYY-MM-DD），用于精确判断交易日"""
    try:
        df = _retry(_ak().tool_trade_date_hist_sina)
        col = "trade_date" if "trade_date" in df.columns else df.columns[0]
        return [pd.Timestamp(x).strftime("%Y-%m-%d") for x in df[col]]
    except Exception as e:  # noqa: BLE001
        print(f"[warn] trade calendar: {e}")
        return []


# --------------------------------------------------------------------------
# 个股 → 板块 映射（筹码归属的基础）
# --------------------------------------------------------------------------
def stock_board_map(boards: list[dict], workers: int = 6) -> pd.DataFrame:
    """遍历板块成分股，构建 [stock_code, stock_name, board_code, board_name] 映射

    首次构建约需数十次请求，结果由上层缓存为 parquet，之后只需增量更新。
    """
    from concurrent.futures import ThreadPoolExecutor  # 局部导入，避免顶层开销

    def one(b: dict):
        cons = board_cons(b["name"])
        if cons is None or cons.empty:
            return []
        code_col = next((c for c in ("代码", "股票代码", "证券代码") if c in cons.columns), None)
        name_col = next((c for c in ("名称", "股票名称", "证券简称") if c in cons.columns), None)
        if not code_col:
            return []
        out = []
        for _, r in cons.iterrows():
            out.append({
                "stock_code": str(r[code_col]).zfill(6),
                "stock_name": str(r[name_col]) if name_col else "",
                "board_code": b["code"],
                "board_name": b["name"],
            })
        return out

    rows: list[dict] = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for res in ex.map(one, boards):
            rows.extend(res or [])
    if not rows:
        return pd.DataFrame(columns=["stock_code", "stock_name", "board_code", "board_name"])
    return pd.DataFrame(rows).drop_duplicates(subset=["stock_code", "board_code"])


# --------------------------------------------------------------------------
# 市场情绪原始数据
# --------------------------------------------------------------------------
def limit_up_pool(date: str) -> tuple[int | None, int | None]:
    """涨停 / 跌停家数。date 格式 YYYYMMDD"""
    zt = dt_ = None
    try:
        zt = len(_retry(_ak().stock_zt_pool_em, date=date))
    except Exception as e:  # noqa: BLE001
        print(f"[warn] limit up pool: {e}")
    try:
        dt_ = len(_retry(_ak().stock_zt_pool_dtgc_em, date=date))
    except Exception as e:  # noqa: BLE001
        print(f"[warn] limit down pool: {e}")
    return zt, dt_


def share_change() -> pd.DataFrame:
    """高管/股东持股变动明细（用于减持压力计数）"""
    for fn_name in ("stock_hold_management_detail_em", "stock_hold_change_cninfo"):
        try:
            fn = getattr(_ak(), fn_name, None)
            if fn is None:
                continue
            df = _retry(fn)
            if df is not None and not df.empty:
                return df
        except Exception as e:  # noqa: BLE001
            print(f"[warn] {fn_name}: {e}")
    return pd.DataFrame()


# --------------------------------------------------------------------------
# 宏观原始数据
# --------------------------------------------------------------------------
def lpr() -> pd.DataFrame:
    """LPR 报价（一年期利率水平与趋势）"""
    try:
        return _retry(_ak().macro_china_lpr)
    except Exception as e:  # noqa: BLE001
        print(f"[warn] lpr: {e}")
        return pd.DataFrame()


def social_financing() -> pd.DataFrame:
    """社会融资规模增量（财政发力代理）"""
    try:
        return _retry(_ak().macro_china_shrzgm)
    except Exception as e:  # noqa: BLE001
        print(f"[warn] shrzgm: {e}")
        return pd.DataFrame()

