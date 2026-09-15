#!/usr/bin/env python3
"""经 GitHub REST API（api.github.com）把本地文件的改动提交到远端分支。

背景：本机到 github.com:443 的连接会被中断，git push 走不通，但
api.github.com 仍可达。本脚本用 Git Data API 复刻一次提交。

设计要点（避免踩坑）：
- 以远端当前 commit 的 tree 为 base_tree，只覆盖/新增指定路径，
  绝不整体替换 tree —— 否则会抹掉 CI 正在写入的数据产物。
- 不依赖本地存在远端历史：直接读 API 的 tree，因此远端被 CI 推进后
  本地仍可正常推送（本地无需先 fetch）。
- 显式禁用代理并重试：Windows 上 urllib 会读注册表代理，该代理对
  GitHub 常常不可用（RemoteDisconnected）。

用法:
    python tools/api_push.py --only path/a.py --only path/b.json
    python tools/api_push.py --only x.py --delete old/probe.yml
    python tools/api_push.py                      # 仅预览默认集合
"""
from __future__ import annotations

import argparse
import base64
import json
import pathlib
import subprocess
import sys
import time
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
OWNER = "chuanwudi46-ops"
REPO = "Big-A"
BRANCH = "main"
API = "https://api.github.com"
TEXT_EXT = {".py", ".json", ".yaml", ".yml", ".md", ".txt", ".js", ".css",
            ".html", ".svg", ".toml", ".cfg", ".ini"}
TEXT_NAMES = {".gitignore", ".gitattributes"}
# 默认预览时跳过的目录：这些是 CI 每日写入的数据产物，本地不应覆盖
SKIP_PREFIX = ("web/data",)


def token() -> str:
    """从 Windows 凭据管理器（GCM）取出 GitHub token"""
    p = subprocess.run(
        ["git", "-c", "credential.helper=manager", "credential", "fill"],
        cwd=ROOT, input="protocol=https\nhost=github.com\n\n",
        capture_output=True, text=True, check=True)
    for line in p.stdout.splitlines():
        if line.startswith("password="):
            return line.split("=", 1)[1]
    raise SystemExit("未能从凭据管理器取到 token")


def api(tok: str, method: str, path: str, payload: dict | None = None,
        tries: int = 4):
    data = json.dumps(payload).encode() if payload is not None else None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    last: Exception | None = None
    for i in range(tries):
        try:
            req = urllib.request.Request(API + path, data=data, method=method)
            req.add_header("Authorization", f"Bearer {tok}")
            req.add_header("Accept", "application/vnd.github+json")
            req.add_header("Content-Type", "application/json")
            with opener.open(req, timeout=60) as resp:
                body = resp.read()
            return json.loads(body) if body else {}
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 * (i + 1))
    raise SystemExit(f"GitHub API {method} {path} 失败：{last}")


def local_head_tree() -> dict[str, str]:
    """本地 HEAD 的 path -> blob sha"""
    p = subprocess.run(["git", "ls-tree", "-r", "HEAD"], cwd=ROOT,
                       capture_output=True, text=True, check=True)
    out = {}
    for line in p.stdout.splitlines():
        meta, path = line.split("\t", 1)
        out[path] = meta.split()[2]
    return out


def remote_tree(tok: str, commit_sha: str) -> tuple[str, dict[str, str]]:
    """返回 (tree_sha, {path: blob_sha})"""
    tree_sha = api(tok, "GET",
                   f"/repos/{OWNER}/{REPO}/git/commits/{commit_sha}")["tree"]["sha"]
    data = api(tok, "GET",
               f"/repos/{OWNER}/{REPO}/git/trees/{tree_sha}?recursive=1")
    files = {e["path"]: e["sha"] for e in data.get("tree", [])
             if e["type"] == "blob"}
    return tree_sha, files


def normalize(path: str, raw: bytes) -> bytes:
    """与 .gitattributes 的 LF 约定保持一致，避免 CRLF 混入历史"""
    if pathlib.Path(path).suffix.lower() in TEXT_EXT or path in TEXT_NAMES:
        return raw.replace(b"\r\n", b"\n")
    return raw


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", action="append", default=[],
                    help="要推送的路径（相对仓库根，可重复）")
    ap.add_argument("--delete", action="append", default=[],
                    help="要删除的远端路径（可重复）")
    ap.add_argument("--apply", action="store_true", help="实际提交（默认仅预览）")
    args = ap.parse_args()

    tok = token()
    ref = api(tok, "GET", f"/repos/{OWNER}/{REPO}/git/ref/heads/{BRANCH}")
    base_sha = ref["object"]["sha"]
    base_tree, rtree = remote_tree(tok, base_sha)
    print(f"远端 {BRANCH} = {base_sha[:7]}（{len(rtree)} 个文件）")

    if args.only or args.delete:
        targets = list(args.only)
        deletes = list(args.delete)
    else:
        ltree = local_head_tree()
        targets = [p for p, s in ltree.items()
                   if rtree.get(p) != s and not p.startswith(SKIP_PREFIX)]
        deletes = [p for p in rtree
                   if p not in ltree and p.startswith(".github/workflows/probe")]

    changed = []
    for path in targets:
        fp = ROOT / path
        if not fp.exists():
            print(f"  [skip] 本地不存在: {path}")
            continue
        raw = normalize(path, fp.read_bytes())
        blob = api(tok, "POST", f"/repos/{OWNER}/{REPO}/git/blobs",
                   {"content": base64.b64encode(raw).decode(), "encoding": "base64"})
        if rtree.get(path) == blob["sha"]:
            print(f"  [same] {path}（内容一致，跳过）")
            continue
        changed.append((path, blob["sha"]))
        print(f"  [push] {path}  {len(raw)} B -> {blob['sha'][:7]}")
    for path in deletes:
        if path in rtree:
            changed.append((path, None))
            print(f"  [del ] {path}")

    if not changed:
        print("无实际改动")
        return 0
    if not args.apply:
        print("\n（预览模式，加 --apply 实际提交）")
        return 0

    entries = [{"path": p, "mode": "100644", "type": "blob", "sha": s}
               for p, s in changed]
    tree = api(tok, "POST", f"/repos/{OWNER}/{REPO}/git/trees",
               {"base_tree": base_tree, "tree": entries})
    msg = subprocess.run(["git", "log", "-1", "--pretty=%B"], cwd=ROOT,
                         capture_output=True, text=True, check=True).stdout.strip()
    ident = {"name": "chuanwudi46-ops",
             "email": "chuanwudi46-ops@users.noreply.github.com"}
    commit = api(tok, "POST", f"/repos/{OWNER}/{REPO}/git/commits",
                 {"message": msg, "tree": tree["sha"], "parents": [base_sha],
                  "author": ident, "committer": ident})
    api(tok, "PATCH", f"/repos/{OWNER}/{REPO}/git/refs/heads/{BRANCH}",
        {"sha": commit["sha"], "force": False})
    print(f"\n[ok] 已提交 {commit['sha'][:7]} -> {BRANCH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
