#!/usr/bin/env python3
"""生成 config/sector_meta.json

把「实时板块清单」与「关键词种子」合并成评分所需的板块元数据。

- 首次部署无需手动运行：`pipeline/main.py` 在发现该文件缺失时会自动调用
  `ensure_sector_meta()`，因此 GitHub Actions 首次构建即可自举，不依赖本地网络。
- 新增板块 / 修改关键词 / 调整出清阶段后，用 `--force` 强制重建。

用法:
    python pipeline/sync_boards.py            # 缺失时生成，已存在则跳过
    python pipeline/sync_boards.py --force    # 强制重新拉取并覆盖
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

CFG = pathlib.Path(__file__).resolve().parent / "config"
META_FP = CFG / "sector_meta.json"

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

# 东财行业板块：m:90+t:2 为行业板块（f:!50 排除三级细分行业）
# 域名列表与 sources.CLIST_HOSTS 共用同一份定义，避免两处漂移。
# 排序键用静态的 f12（代码）：用 f3 涨幅排序时盘中翻页会串位（重复 + 漏块）。
CLIST_PATH = ("/api/qt/clist/get?po=0&np=1&fltt=2&invt=2&fid=f12"
              "&fs=m:90+t:2+f:!50&fields=f12,f14")


def _load_universe() -> dict:
    """读取板块宇宙白名单（可能为空 dict）"""
    fp = CFG / "board_universe.json"
    if not fp.exists():
        return {}
    try:
        return json.loads(fp.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        print(f"  [warn] 读取 {fp.name} 失败，改用全量板块：{e}")
        return {}


def _fetch_boards() -> list[tuple[str, str]]:
    """优先 akshare，失败回退东财直连。实测 akshare 硬编码的分片域名
    （17.push2.eastmoney.com）以及 push2.eastmoney.com 本身在部分网络下
    不可达（含 GitHub Actions 的海外 runner），故做多域名容灾。

    注意：必须走分页取全量。服务端单页上限为 100，只请求一次会静默丢掉
    3/4 的板块（东财现行行业体系约 496 个）。
    """
    # 板块宇宙白名单：存在则只保留名单内的板块（用于收敛到一级/二级行业）
    universe = _load_universe()
    include: set[str] | None = set(universe["names"]) if universe.get("names") else None
    if include:
        print(f"  已加载板块宇宙白名单（{universe.get('level', '?')}）：{len(include)} 个名称")

    def _apply_universe(out: list[tuple[str, str]]) -> list[tuple[str, str]]:
        if not include:
            return out
        kept = [x for x in out if x[1] in include]
        missed = sorted(include - {x[1] for x in kept})
        if missed:
            print(f"  [warn] 白名单中 {len(missed)} 个名称在实时清单里不存在："
                  f"{'、'.join(missed[:15])}{'…' if len(missed) > 15 else ''}")
        return kept

    if not include:
        try:
            import akshare as ak

            df = ak.stock_board_industry_name_em()
            out = [(str(r["板块代码"]), str(r["板块名称"])) for _, r in df.iterrows()]
            if out:
                print(f"  数据来源：akshare（{len(out)} 个板块）")
                return out
        except Exception as e:  # noqa: BLE001
            print(f"  akshare 不可用（{str(e)[:90]}），切换直连通道")

    from sources import CLIST_HOSTS, REFERER, UA

    import requests

    headers = {"User-Agent": UA, "Referer": REFERER}
    last_err: Exception | None = None
    for host in CLIST_HOSTS:
        # trust_env 两轮：先按环境代理设置走，失败则忽略代理重试
        for trust_env in (True, False):
            try:
                s = requests.Session()
                s.trust_env = trust_env
                out: list[tuple[str, str]] = []
                pn = 1
                while pn <= 50:
                    u = (f"{host}{CLIST_PATH}&pn={pn}&pz=100")
                    r = s.get(u, timeout=20, headers=headers)
                    r.raise_for_status()
                    data = r.json().get("data") or {}
                    diff = data.get("diff") or []
                    if not diff:
                        break
                    out += [(str(d["f12"]), str(d["f14"])) for d in diff]
                    if len(diff) < 100 or len(out) >= int(data.get("total") or 0):
                        break
                    pn += 1
                if out:
                    kept = _apply_universe(out)
                    print(f"  数据来源：东财直连 {host}"
                          f"（全量 {len(out)} 个 → 采用 {len(kept)} 个）")
                    return kept
                last_err = RuntimeError(f"{host} 返回空 diff")
            except Exception as e:  # noqa: BLE001
                last_err = e
    raise RuntimeError(f"直连通道也未能获取板块清单，请检查网络：{last_err}")


def build_meta(boards_raw: list[tuple[str, str]], seed: dict,
               universe: dict | None = None) -> dict:
    """把板块清单与关键词种子合并。

    按当前宇宙层级取关键词词典（boards_by_level[level]）；二级板块的
    clearing / blacklist 从上级一级行业**继承** —— 这两张表是人工按一级
    行业维护的，二级层面再维护一份既冗余又容易不一致。
    """
    universe = universe if universe is not None else _load_universe()
    level = universe.get("level") or "申万一级行业"
    by_level = seed.get("boards_by_level") or {}
    kw_dict = by_level.get(level) or seed.get("boards") or {}
    parents = universe.get("parents") or {}
    sw_codes = universe.get("sw_codes") or {}
    clearing = seed.get("clearing") or {}
    blacklist = seed.get("blacklist") or []

    boards = []
    inherited = 0
    for code, name in boards_raw:
        parent = parents.get(name, "")

        # 出清度：自身没有就继承上级
        cl = clearing.get(name)
        if cl is None and parent:
            cl = clearing.get(parent)
            if cl is not None:
                inherited += 1
        cl = cl or {}

        # 负面清单：按一级行业维护，二级板块名往往不含一级名（如"普钢" vs "钢铁"）
        black = any(k in name for k in blacklist) or (
            bool(parent) and any(k in parent for k in blacklist))

        kw = kw_dict.get(name)
        if not kw:
            # 兜底关键词：用板块名本身，去掉申万用来与一级区分的 Ⅱ/Ⅲ 后缀
            kw = [name.replace("Ⅱ", "").replace("Ⅲ", "")]

        boards.append({
            "code": code,
            "name": name,
            "parent": parent,
            # 申万行业指数 6 位码：腾讯 K 线主通道靠它（pt01 + 该码），
            # 一级二级同一套规则，不依赖腾讯那份只有一级的行业列表
            "sw_code": sw_codes.get(name, ""),
            "keywords": kw,
            "clearing_stage": cl.get("stage", "未出清"),
            "clearing_score": float(cl.get("score", 0.5)),
            "blacklisted": black,
        })
    if inherited:
        print(f"  {inherited} 个板块的出清度继承自上级一级行业")
    return {
        "version": "1.1",
        "updated": "2026-09-16",
        "level": level,
        "boards": boards,
        "blacklist": blacklist,
        "policy_signals": seed.get("policy_signals", {}),
        "price_signals": seed.get("price_signals", []),
    }


def write_meta(meta: dict) -> pathlib.Path:
    META_FP.parent.mkdir(parents=True, exist_ok=True)
    META_FP.write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                       encoding="utf-8")
    n = len(meta.get("boards") or [])
    print(f"[ok] {n} 个板块（{meta.get('level', '?')}）-> {META_FP}")
    missing = [b["name"] for b in meta.get("boards", [])
               if len(b.get("keywords") or []) <= 1
               and (b.get("keywords") or [""])[0] == b["name"].replace("Ⅱ", "").replace("Ⅲ", "")]
    if missing:
        print(f"[hint] {len(missing)} 个板块暂无专用关键词，"
              f"建议补进 keywords_seed.json 的 boards_by_level（可显著提升新闻匹配率）：")
        print("       " + "、".join(missing[:30])
              + ("…" if len(missing) > 30 else ""))
    return META_FP


def ensure_sector_meta(force: bool = False) -> pathlib.Path:
    """确保 sector_meta.json 存在，缺失则拉取板块清单生成。

    供 main.py 在首次运行时自动调用，免去「必须先手动跑一次」的前置步骤。
    注意：本函数会发起网络请求，失败时抛异常由调用方决定如何降级。
    """
    if META_FP.exists() and not force:
        return META_FP

    seed_fp = CFG / "keywords_seed.json"
    if not seed_fp.exists():
        raise FileNotFoundError(f"缺少关键词种子文件 {seed_fp}")
    seed = json.loads(seed_fp.read_text(encoding="utf-8"))

    print("拉取实时行业板块清单 …")
    boards_raw = _fetch_boards()
    return write_meta(build_meta(boards_raw, seed))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true",
                    help="强制重新拉取并覆盖已有 sector_meta.json")
    args = ap.parse_args()
    try:
        ensure_sector_meta(force=args.force)
    except Exception as e:  # noqa: BLE001
        print(f"[error] 生成 sector_meta.json 失败：{e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
