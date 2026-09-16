#!/usr/bin/env python3
"""生成 config/board_universe.json：把「申万官方分级名单」收敛成板块宇宙。

背景（为什么要有这个脚本）
--------------------------------------------------------------------------
东财现行行业板块体系已扩到约 496 个，**混装了一级 / 二级 / 三级细分**，
且接口不返回任何层级字段（`m:90+t:2` 就是一个平铺列表 + `f:!50` 过滤）。
所以没法直接从东财这边"取二级行业"，只能反过来：
    申万官方分级名单  ∩  东财实时板块名
两边**同名**（东财的板块名大量沿用申万，二级里同名的会带 Ⅱ 后缀用于与一级区分），
实测一级 31/31 全中、二级 131 里命中 127。

用法:
    python pipeline/build_universe.py                    # 生成/刷新（默认保持现有一级）
    python pipeline/build_universe.py --level 申万二级行业  # 切到二级细分
    python pipeline/build_universe.py --report            # 只打印对齐报告，不写文件

改完层级后需要重建板块元数据与缓存：
    python pipeline/sync_boards.py --force
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import sys

CFG = pathlib.Path(__file__).resolve().parent / "config"
UNIVERSE_FP = CFG / "board_universe.json"

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

LEVELS = ["申万一级行业", "申万二级行业", "申万三级行业"]


def _sw_lists() -> dict[str, list[tuple[str, str, str]]]:
    """申万三级名单：{层级: [(名称, 代码, 上级名称)]}。

    数据源 akshare（申万宏源官网口径）。失败则抛异常 —— 这份名单是
    板块宇宙的**唯一权威来源**，quietly 降级会让板块口径悄悄漂移。
    """
    import akshare as ak

    out: dict[str, list[tuple[str, str, str]]] = {}
    for level, fn in [("申万一级行业", "sw_index_first_info"),
                      ("申万二级行业", "sw_index_second_info"),
                      ("申万三级行业", "sw_index_third_info")]:
        df = getattr(ak, fn)()
        rows = []
        for _, r in df.iterrows():
            rows.append((str(r["行业名称"]).strip(),
                         str(r["行业代码"]).strip(),
                         str(r.get("上级行业", "")).strip()))
        out[level] = rows
    return out


def _em_boards() -> dict[str, str]:
    """东财行业板块全量：{名称: 代码}"""
    import sources
    return {b["name"]: b["code"] for b in sources.board_list()}


def _probe_kline(name2code: dict[str, str], workers: int = 6) -> set[str]:
    """探测腾讯行情是否收录这些申万行业指数的日 K 线。

    腾讯板块指数代码 = `pt01` + 申万 6 位码。实测申万二级 131 个里有 6 个
    （农业综合Ⅱ / 其他家电Ⅱ / 旅游零售Ⅱ / 体育Ⅱ / 油气开采Ⅱ / 医疗美容）
    在腾讯没有行情 —— 它们要么是申万新版分类、要么腾讯指数库没跟上。

    这些板块拿不到 K 线就无法评分，留在宇宙里只会让前端出现「点进去没数据」
    的死条目，所以在生成宇宙时就剔掉，让「配置里的板块」与「有数据的板块」
    始终一致。
    """
    import json
    import urllib.request
    from concurrent.futures import ThreadPoolExecutor

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    headers = {"User-Agent": "Mozilla/5.0"}

    def one(item):
        name, sic = item
        code = "pt01" + sic
        url = ("https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get"
               f"?param={code},day,,,800,qfq")
        try:
            raw = opener.open(urllib.request.Request(url, headers=headers), timeout=15).read()
            node = ((json.loads(raw).get("data") or {}).get(code)) or {}
            bars = node.get("day") or node.get("qfqday") or []
            if len(bars) < 250:
                return None
            # 末根 K 线必须是近期的。有「僵尸指数」—— 指数已停更、腾讯数据
            # 停在几年前（实测林业Ⅱ 止于 2021-12-10），这类取回来会被区间
            # 裁剪剪成空表，等于没数据，不该进宇宙
            try:
                last = dt.date.fromisoformat(str(bars[-1][0]))
            except ValueError:
                return None
            if (dt.date.today() - last).days > 45:
                return None
            return name
        except Exception:  # noqa: BLE001
            return None

    with ThreadPoolExecutor(max_workers=workers) as ex:
        return {r for r in ex.map(one, name2code.items()) if r}


def build(level: str) -> dict:
    sw = _sw_lists()
    em = _em_boards()
    print(f"申万名单：一级 {len(sw['申万一级行业'])} / 二级 {len(sw['申万二级行业'])}"
          f" / 三级 {len(sw['申万三级行业'])}")
    print(f"东财行业板块全量：{len(em)}")

    levels: dict[str, list[str]] = {}
    codes: dict[str, str] = {}
    sw_codes: dict[str, str] = {}
    missing: dict[str, str] = {}
    for lv in LEVELS:
        hit = [(n, c) for n, c, _ in sw[lv] if n in em]
        levels[lv] = [n for n, _ in hit]
        codes.update({n: em[n] for n, _ in hit})
        sw_codes.update({n: c.split(".")[0] for n, c in hit})
        miss = [n for n, _, _ in sw[lv] if n not in em]
        if miss:
            missing[lv] = (f"{len(miss)} 个未在东财板块清单中找到（东财未做同名板块或"
                           f"层级切分不同）：{'、'.join(miss)}")
        print(f"  {lv}: {len(sw[lv])} → 对齐 {len(hit)}")

    if level not in levels:
        raise SystemExit(f"未知层级 {level}，可选：{LEVELS}")
    if not levels.get(level):
        raise SystemExit(f"{level} 对齐结果为空，拒绝写入（避免把板块宇宙清空）")

    # ---- K 线可用性探测（决定性地影响能不能出分）
    cand = levels[level]
    print(f"  探测腾讯 K 线可用性（{len(cand)} 个）…")
    try:
        usable = _probe_kline({n: sw_codes[n] for n in cand if n in sw_codes})
    except Exception as e:  # noqa: BLE001
        usable = set()
        print(f"  [warn] K 线探测异常（{str(e)[:80]}），跳过剔除")

    no_kline: list[str] = []
    if usable and len(usable) >= len(cand) * 0.5:
        no_kline = [n for n in cand if n not in usable]
        if no_kline:
            print(f"  K 线可用 {len(usable)}/{len(cand)}，剔除无源板块 {len(no_kline)} 个："
                  f"{'、'.join(no_kline)}")
            levels[level] = [n for n in cand if n in usable]
    elif usable:
        # 命中率过低说明大概率是探测侧的网络问题，而非真的没数据。
        # 宁可留下几个死条目，也不能把宇宙误砍一半。
        print(f"  [warn] K 线探测命中率异常偏低（{len(usable)}/{len(cand)}），"
              f"判定为探测故障，保留原名单不剔除")
    else:
        print("  [warn] K 线探测无任何命中，判定为网络故障，保留原名单")

    # 二级 → 一级 的归属，供 clearing / blacklist 继承
    parents = {n: up for n, _, up in sw["申万二级行业"]
               if n in codes and up in levels["申万一级行业"]}

    return {
        "_comment": ("板块宇宙白名单：sync_boards.py 用它把东财全量板块（约 496 个，"
                     "混装一级/二级/三级）收敛到指定层级。名称必须与东财实时板块清单"
                     "完全一致。改 level 字段即可切换层级，之后需跑 "
                     "`python pipeline/sync_boards.py --force` 重建元数据。"
                     "本文件由 pipeline/build_universe.py 生成，请勿手改。"),
        "level": level,
        "source": "akshare.sw_index_*_info() ∩ 东财行业板块清单",
        "updated": dt.date.today().isoformat(),
        "board_codes": codes,
        "sw_codes": sw_codes,
        "names": levels[level],
        "levels": levels,
        "parents": parents,
        "no_kline": no_kline,
        "missing": missing,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--level", default=None, help=f"板块层级，可选 {' / '.join(LEVELS)}")
    ap.add_argument("--report", action="store_true", help="只打印报告，不写文件")
    args = ap.parse_args()

    level = args.level
    if not level and UNIVERSE_FP.exists():
        level = json.loads(UNIVERSE_FP.read_text(encoding="utf-8")).get("level")
    level = level or "申万一级行业"

    try:
        uni = build(level)
    except Exception as e:  # noqa: BLE001
        print(f"[error] 构建板块宇宙失败：{e}")
        return 1

    print(f"\n生效层级：{uni['level']}  →  {len(uni['names'])} 个板块")
    for lv, msg in uni["missing"].items():
        print(f"  [未对齐·{lv}] {msg}")

    if args.report:
        print("\n（--report 模式，未写文件）")
        return 0

    UNIVERSE_FP.parent.mkdir(parents=True, exist_ok=True)
    UNIVERSE_FP.write_text(json.dumps(uni, ensure_ascii=False, indent=1),
                           encoding="utf-8")
    print(f"\n[ok] 已写入 {UNIVERSE_FP}")
    print("     下一步：python pipeline/sync_boards.py --force")
    return 0


if __name__ == "__main__":
    sys.exit(main())
