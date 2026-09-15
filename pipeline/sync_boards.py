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

# 东方财富行业板块：m:90+t:2 为行业板块（f:!50 排除三级细分行业）
PUSH2 = ("https://push2.eastmoney.com/api/qt/clist/get"
         "?pn=1&pz=200&po=1&np=1&fltt=2&invt=2&fid=f3"
         "&fs=m:90+t:2+f:!50&fields=f12,f14")


def _fetch_boards() -> list[tuple[str, str]]:
    """优先 akshare，失败回退东财直连。实测 akshare 硬编码的分片域名
    （17.push2.eastmoney.com）在部分网络下不可达，故需要兜底。"""
    try:
        import akshare as ak

        df = ak.stock_board_industry_name_em()
        out = [(str(r["板块代码"]), str(r["板块名称"])) for _, r in df.iterrows()]
        if out:
            print(f"  数据来源：akshare（{len(out)} 个板块）")
            return out
    except Exception as e:  # noqa: BLE001
        print(f"  akshare 不可用（{e}），切换直连通道")

    import requests

    r = requests.get(PUSH2, timeout=15,
                     headers={"User-Agent": "Mozilla/5.0",
                              "Referer": "https://quote.eastmoney.com/"})
    r.raise_for_status()
    diff = (r.json().get("data") or {}).get("diff") or []
    if not diff:
        raise RuntimeError("直连通道也未能获取板块清单，请检查网络")
    out = [(str(d["f12"]), str(d["f14"])) for d in diff]
    print(f"  数据来源：东财直连（{len(out)} 个板块）")
    return out


def build_meta(boards_raw: list[tuple[str, str]], seed: dict) -> dict:
    """把板块清单与关键词种子合并"""
    seed_boards = seed.get("boards") or {}
    clearing = seed.get("clearing") or {}
    boards = []
    for code, name in boards_raw:
        cl = clearing.get(name, {})
        boards.append({
            "code": code,
            "name": name,
            "keywords": seed_boards.get(name, [name]),
            "clearing_stage": cl.get("stage", "未出清"),
            "clearing_score": float(cl.get("score", 0.5)),
        })
    return {
        "version": "1.0",
        "updated": "2026-09-15",
        "boards": boards,
        "blacklist": seed.get("blacklist", []),
        "policy_signals": seed.get("policy_signals", {}),
        "price_signals": seed.get("price_signals", []),
    }


def write_meta(meta: dict) -> pathlib.Path:
    META_FP.parent.mkdir(parents=True, exist_ok=True)
    META_FP.write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                       encoding="utf-8")
    n = len(meta.get("boards") or [])
    print(f"[ok] {n} 个板块 -> {META_FP}")
    missing = [b["name"] for b in meta.get("boards", [])
               if b.get("keywords") == [b["name"]]]
    if missing:
        print(f"[hint] {len(missing)} 个板块暂无专用关键词，"
              f"建议补进 keywords_seed.json 的 boards（可显著提升新闻匹配率）：")
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
