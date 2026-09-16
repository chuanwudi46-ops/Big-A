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
REFERER = "https://quote.eastmoney.com/"

# 东财公开接口的可用域名，按顺序尝试。
# 实测 push2 / push2his 的边缘节点不稳定：从 GitHub Actions 的海外 runner 访问时，
# push2 会 302→502，push2his 连续请求几次后直接连接失败（000）；
# 国内部分网络下两者同样不可达。push2delay 是其镜像域名，海内外均稳定，
# 且 clist 与 kline 两条路径都能供数，字段与实时域名完全一致
# （含 f62 主力净流入、f184 净占比），故作为首选。
EM_HOSTS = (
    "https://push2delay.eastmoney.com",
    "https://push2his.eastmoney.com",
    "https://push2.eastmoney.com",
    "https://82.push2.eastmoney.com",
)
CLIST_HOSTS = EM_HOSTS          # 板块/个股列表
KLINE_HOSTS = EM_HOSTS          # 板块日 K 线

# 腾讯行情：申万一级行业指数。作为 K 线的**首选**通道。
# 原因：东财的 K 线域名在海外 runner 上全不可用（push2his 连几次即断连、
# push2 固定 502），push2delay 虽可达但会软限流（返回 HTTP 200 而 klines
# 为空、dktotal=0），实测 31 个板块里稳定掉 1~2 个。
# 腾讯此接口：海内外均可达、无地区限制，实测 31/31 全部成功、总耗时约 3 秒，
# 且其「行业」分类恰为申万一级 31 个，与 board_universe.json 完全同名。
TENCENT_KLINE = ("https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get"
                 "?param={code},day,{start},{end},{count},qfq")
TENCENT_RANK = ("https://proxy.finance.qq.com/cgi/cgi-bin/rank/pt/getRank"
                "?board_type=hy&sort_type=price&direct=down&offset=0&count=60")

_session: requests.Session | None = None
_session_noproxy: requests.Session | None = None


def _build_session(trust_env: bool) -> requests.Session:
    s = requests.Session()
    s.trust_env = trust_env
    s.headers.update({"User-Agent": UA, "Referer": REFERER})
    return s


def _sess() -> requests.Session:
    global _session
    if _session is None:
        _session = _build_session(True)
    return _session


def _sess_noproxy() -> requests.Session:
    """忽略环境代理的会话。

    本机若配置了系统级代理（Windows 注册表 / HTTP_PROXY 环境变量），
    requests 默认会走它，而该代理对东财域名常常不可用（ProxyError）。
    首次请求失败后自动改用本会话重试。
    """
    global _session_noproxy
    if _session_noproxy is None:
        _session_noproxy = _build_session(False)
    return _session_noproxy


def _get(url: str, timeout: int = 20) -> requests.Response:
    """带「代理降级」的 GET：正常会话失败后改用忽略代理的会话再试一次"""
    try:
        return _sess().get(url, timeout=timeout)
    except Exception:  # noqa: BLE001
        return _sess_noproxy().get(url, timeout=timeout)


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
def _push2_clist_on(base: str, fs: str, fields: str, pz: int) -> list[dict]:
    """在指定域名上分页拉取 clist。

    注意：服务端把单页上限硬编码为 100（请求 pz=200/500 也只回 100 条），
    因此翻页条件不能拿 pz 比，必须拿「服务端实际页大小」比，否则大数据集
    （如 496 个行业板块、成分股数百只的板块）会被静默截断。
    """
    page_size = min(pz, 100)
    rows: list[dict] = []
    total: int | None = None
    pn = 1
    while pn <= 50:                       # 防御：pn 失效时不至于死循环
        url = (f"{base}/api/qt/clist/get"
               f"?pn={pn}&pz={page_size}&po=1&np=1&fltt=2&invt=2&fid=f3"
               f"&fs={fs}&fields={fields}")
        r = _get(url, timeout=15)
        r.raise_for_status()
        data = r.json().get("data") or {}
        diff = data.get("diff") or []
        if total is None and data.get("total") is not None:
            try:
                total = int(data["total"])
            except (TypeError, ValueError):
                total = None
        if not diff:
            break
        rows.extend(diff)
        if total is not None and len(rows) >= total:
            break
        if len(diff) < page_size:         # 不足一页 => 已到末页
            break
        pn += 1
    return rows


def _push2_clist(fs: str, fields: str, pz: int = 100) -> list[dict]:
    """板块/个股列表直连（clist）。返回原始 diff 列表。

    按 CLIST_HOSTS 顺序做多域名容灾，任一域名返回有效数据即成功。
    """
    last_err: Exception | None = None
    for base in CLIST_HOSTS:
        try:
            rows = _push2_clist_on(base, fs, fields, pz)
            if rows:
                return rows
            last_err = RuntimeError(f"{base} 返回空列表")
        except Exception as e:  # noqa: BLE001
            last_err = e
            print(f"[info] clist 通道不可用（{base}）：{str(e)[:90]}")
    raise RuntimeError(f"所有 clist 通道均失败：{last_err}")


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
    """直连获取板块日 K 线。secid 形如 90.BK0475。

    按 KLINE_HOSTS 顺序容灾（push2delay 优先，见常量注释），并整体重试 2 轮。
    实测 runner 上偶发 RemoteDisconnected（同一 secid 重试即成功），单轮循环
    会让个别板块静默掉出评分（实测 30/31），故加一轮短退避重试。
    """
    last_err: Exception | None = None
    for attempt in range(2):
        if attempt:
            time.sleep(2.5)
        for base in KLINE_HOSTS:
            url = (f"{base}/api/qt/stock/kline/get"
                   f"?secid={secid}"
                   "&fields1=f1,f2,f3,f4,f5,f6"
                   "&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"
                   f"&klt=101&fqt=1&beg={start}&end={end}&lmt={limit}")
            try:
                r = _get(url, timeout=20)
                r.raise_for_status()
                klines = (r.json().get("data") or {}).get("klines") or []
                if not klines:
                    last_err = RuntimeError(f"{base} 返回空 K 线")
                    continue
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
            except Exception as e:  # noqa: BLE001
                last_err = e
                if attempt == 0 and base == KLINE_HOSTS[0]:
                    print(f"[info] K线通道 {base} 抖动（{secid}）：{str(e)[:60]}")
    print(f"[warn] 所有 K 线通道均失败（{secid}，已重试 2 轮）：{str(last_err)[:90]}")
    raise RuntimeError(f"所有 K 线通道均失败（{secid}）：{last_err}")


_tx_map: dict[str, str] | None = None


def _tencent_industry_map() -> dict[str, str]:
    """申万一级行业名 -> 腾讯板块代码（形如 pt01801120）。结果 memo。"""
    global _tx_map
    if _tx_map is not None:
        return _tx_map
    try:
        r = _get(TENCENT_RANK, timeout=15)
        r.raise_for_status()
        rows = (r.json().get("data") or {}).get("rank_list") or []
        _tx_map = {str(x["name"]): str(x["code"]) for x in rows if x.get("name")}
    except Exception as e:  # noqa: BLE001
        print(f"[warn] 腾讯行业清单不可用：{str(e)[:80]}")
        _tx_map = {}
    return _tx_map


def _tencent_board_hist(name: str, start: str, end: str, count: int = 800) -> pd.DataFrame:
    """腾讯行情：申万一级行业指数日 K 线。

    start/end 格式 YYYYMMDD（与本地包接口一致）。bar 字段位置经实测校准：
    [日期, 开, 收, 高, 低, 成交量(手), {}, 换手率%, 成交额(万元), ...]。
    涨跌幅不取腾讯的字段位置（不同品种长度不一），改用收盘价自行推算，
    为保证首根 bar 的涨跌幅正确，取数区间向前多留 15 个自然日再裁剪。
    """
    code = _tencent_industry_map().get(name)
    if not code:
        raise RuntimeError(f"腾讯未收录该行业：{name}")

    def _iso(d: str) -> str:
        return f"{d[0:4]}-{d[4:6]}-{d[6:8]}"

    # 向前多取 15 天，供 pct_change 有前收可比；再按原始 start 裁剪
    s_date = dt.datetime.strptime(start, "%Y%m%d").date() - dt.timedelta(days=15)
    url = TENCENT_KLINE.format(code=code, start=s_date.strftime("%Y-%m-%d"),
                               end=_iso(end), count=count)
    r = _get(url, timeout=20)
    r.raise_for_status()
    bars = ((r.json().get("data") or {}).get(code) or {}).get("day") or []
    if len(bars) < 30:
        raise RuntimeError(f"腾讯 K 线不足（{name}：{len(bars)} 根）")

    def _f(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return float("nan")

    recs = []
    for b in bars:
        if len(b) < 9:
            continue
        recs.append({
            "date": b[0],
            "open": _f(b[1]), "close": _f(b[2]),
            "high": _f(b[3]), "low": _f(b[4]),
            "volume": _f(b[5]),
            "turnover": _f(b[7]),
            # 腾讯成交额单位为万元，换算成「元」与东财口径对齐
            "amount": _f(b[8]) * 1e4,
        })
    df = pd.DataFrame(recs)
    if df.empty:
        raise RuntimeError(f"腾讯 K 线解析为空（{name}）")
    df["pct"] = (df["close"].pct_change() * 100).fillna(0.0)
    raw_start = _iso(start)
    df = df[df["date"] >= raw_start].reset_index(drop=True)
    if df.empty:
        raise RuntimeError(f"腾讯 K 线裁剪后为空（{name}）")
    return df[["date", "open", "close", "high", "low",
               "volume", "amount", "pct", "turnover"]]


def _direct_news(limit: int = 100) -> pd.DataFrame:
    """直连获取东财 7x24 快讯"""
    url = ("https://np-listapi.eastmoney.com/comm/web/getFastNewsList"
           f"?client=web&biz=web_724&fastColumn=102&sortEnd=&pageSize={limit}&req_trace=1")
    r = _get(url, timeout=15)
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


def _direct_board_cons(code: str) -> pd.DataFrame:
    """直连获取板块成分股。列名与 akshare 对齐。

    必须带上「涨跌幅」（f3）：factors.resonance 靠它算上涨家数占比与离散度，
    缺列会把板块共振因子静默压成中性 0.5。
    """
    rows = _push2_clist(f"b:{code}+f:!50", "f12,f14,f3", pz=100)
    if not rows:
        raise RuntimeError(f"direct cons empty for {code}")
    return pd.DataFrame({
        "代码": [str(r["f12"]) for r in rows],
        "名称": [str(r["f14"]) for r in rows],
        "涨跌幅": [r.get("f3") for r in rows],
    })


# --------------------------------------------------------------------------
# 板块
# --------------------------------------------------------------------------
def board_list(include: set[str] | None = None) -> list[dict]:
    """行业板块清单（全量分页），返回 [{code, name}]。

    东财现行行业体系约 496 个板块，混合了一级 / 二级 / 三级细分，
    接口不提供层级字段。`include` 为可选的名称白名单，用于把板块宇宙
    收敛到指定的那份名单（见 config/board_universe.json）。
    """
    rows = _push2_clist("m:90+t:2+f:!50", "f12,f14", pz=100)
    out = [{"code": str(r["f12"]), "name": str(r["f14"])} for r in rows
           if r.get("f12") and r.get("f14")]
    if include:
        out = [b for b in out if b["name"] in include]
    return out


def board_snapshot() -> pd.DataFrame:
    """行业板块当日快照：涨跌幅 / 换手率 / 涨跌家数 / 主力净流入占比

    直连优先：一次请求即拿到全量板块（含 f62 主力净流入 / f184 净占比），
    比 akshare 的「列表 + 资金流两次调用」更快，也避开了 push2 的不可用。
    """
    try:
        return _direct_board_snapshot()
    except Exception as e:  # noqa: BLE001
        print(f"[info] 直连板块快照不可用（{str(e)[:80]}），回退 akshare")

    ak = _ak()
    df = _retry(ak.stock_board_industry_name_em).rename(columns={
        "板块代码": "code", "板块名称": "name", "涨跌幅": "pct",
        "换手率": "turnover", "上涨家数": "up", "下跌家数": "down",
        "总市值": "mktcap", "领涨股票": "leader",
    })
    df = df[[c for c in ["code", "name", "pct", "turnover", "up", "down",
                         "mktcap", "leader"] if c in df.columns]].copy()
    df["code"] = df["code"].astype(str)
    ff = _retry(ak.stock_sector_fund_flow_rank,
                indicator="今日", sector_type="行业资金流")
    ff = ff.rename(columns={
        "名称": "name",
        "今日主力净流入-净额": "main_inflow",
        "今日主力净流入-净占比": "main_inflow_pct",
    })
    cols = [c for c in ["name", "main_inflow", "main_inflow_pct"] if c in ff.columns]
    df = df.merge(ff[cols], on="name", how="left")
    if "main_inflow_pct" not in df.columns:
        raise RuntimeError("akshare 未返回资金流")
    return df


def board_hist(name: str, start: str, end: str, code: str | None = None) -> pd.DataFrame:
    """板块日 K 线。start/end 格式 YYYYMMDD；code 为东财板块代码（如 BK1201）。

    通道优先级：**腾讯（申万一级行业指数）→ 东财直连 → akshare**。
    腾讯优先的三个理由：① 海内外均可达，不受东财分片域名封锁影响；
    ② 31 个一级行业与 board_universe.json 同名，天然一一对应；
    ③ 实测 31/31 全成功、总耗时约 3 秒，远快于 akshare 的逐块重试。
    """
    try:
        df = _tencent_board_hist(name, start, end)
        if not df.empty:
            return df
    except Exception as e:  # noqa: BLE001
        print(f"[info] 腾讯K线不可用（{name}）：{str(e)[:80]}")

    if code:
        try:
            df = _direct_board_hist(f"90.{code}", start, end)
            if not df.empty:
                return df
        except Exception as e:  # noqa: BLE001
            print(f"[info] 直连K线不可用（{name}）：{str(e)[:80]}，回退 akshare")

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
    return df[cols].copy()


_cons_cache: dict[str, pd.DataFrame] = {}


def board_cons(name: str, code: str | None = None) -> pd.DataFrame:
    """板块成分股（用于共振度与筹码归属）。失败返回空表。

    直连优先（单次请求，含涨跌幅），akshare 兜底。结果按板块名 memo，
    避免同一次运行内重复请求（评分阶段 31 个板块各调用一次）。
    """
    cached = _cons_cache.get(name)
    if cached is not None:
        return cached

    df = pd.DataFrame()
    if code:
        try:
            df = _direct_board_cons(code)
        except Exception as e:  # noqa: BLE001
            print(f"[warn] 直连成分股不可用（{name}）：{str(e)[:80]}，回退 akshare")

    if df is None or df.empty:
        try:
            df = _retry(_ak().stock_board_industry_cons_em, symbol=name)
        except Exception as e:  # noqa: BLE001
            print(f"[warn] cons {name}: {e}")
            df = pd.DataFrame()

    if df is None:
        df = pd.DataFrame()
    _cons_cache[name] = df
    return df


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
        cons = board_cons(b["name"], b.get("code"))
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

