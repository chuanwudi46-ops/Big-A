#!/usr/bin/env python3
"""经 GitHub REST API（api.github.com）提交本地改动。

用途：当本机到 github.com:443 的连接被中断、git push 走不通，
但 api.github.com 仍可达时，用 Git Data API 复刻一次本地提交。

用法:
    python tools/api_push.py            # 预览将要提交的文件
    python tools/api_push.py --apply    # 实际提交
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
            ".html", ".svg", ".gitignore", ".gitattributes"}


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
    """调用 GitHub REST API。

    显式禁用代理：Windows 上 urllib 会读取注册表里的系统代理设置，
    而该代理对 GitHub 常常不可用（RemoteDisconnected）。
    """
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


def local_diff(base_sha: str) -> list[tuple[str, str]]:
    """返回 [(状态, 路径)]，状态取值 A/M/D"""
    p = subprocess.run(["git", "diff", "--name-status", base_sha, "HEAD"],
                       cwd=ROOT, capture_output=True, text=True, check=True)
    out = []
    for line in p.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            out.append((parts[0][0], parts[-1]))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="实际提交（默认仅预览）")
    args = ap.parse_args()

    tok = token()
    ref = api(tok, "GET", f"/repos/{OWNER}/{REPO}/git/ref/heads/{BRANCH}")
    base_sha = ref["object"]["sha"]
    base_tree = api(tok, "GET",
                    f"/repos/{OWNER}/{REPO}/git/commits/{base_sha}")["tree"]["sha"]
    print(f"远程 {BRANCH} = {base_sha[:7]}")

    diff = local_diff(base_sha)
    if not diff:
        print("本地与远程无差异，无需提交")
        return 0
    print(f"待提交 {len(diff)} 个文件：")
    for st, path in diff:
        print(f"  {st}  {path}")
    if not args.apply:
        print("\n（预览模式，加 --apply 实际提交）")
        return 0

    entries = []
    for st, path in diff:
        if st == "D":
            entries.append({"path": path, "mode": "100644",
                            "type": "blob", "sha": None})
            continue
        raw = (ROOT / path).read_bytes()
        # 与 .gitattributes 的 LF 约定保持一致，避免 CRLF 混入历史
        if pathlib.Path(path).suffix.lower() in TEXT_EXT or path in (".gitignore",
                                                                   ".gitattributes"):
            raw = raw.replace(b"\r\n", b"\n")
        blob = api(tok, "POST", f"/repos/{OWNER}/{REPO}/git/blobs",
                   {"content": base64.b64encode(raw).decode(), "encoding": "base64"})
        entries.append({"path": path, "mode": "100644",
                        "type": "blob", "sha": blob["sha"]})
        print(f"  blob {path} -> {blob['sha'][:7]}")

    tree = api(tok, "POST", f"/repos/{OWNER}/{REPO}/git/trees",
               {"base_tree": base_tree, "tree": entries})
    msg = subprocess.run(["git", "log", "-1", "--pretty=%B"],
                         cwd=ROOT, capture_output=True, text=True,
                         check=True).stdout.strip()
    ident = {"name": "chuanwudi46-ops",
             "email": "chuanwudi46-ops@users.noreply.github.com"}
    commit = api(tok, "POST", f"/repos/{OWNER}/{REPO}/git/commits",
                 {"message": msg, "tree": tree["sha"], "parents": [base_sha],
                  "author": ident, "committer": ident})
    api(tok, "PATCH", f"/repos/{OWNER}/{REPO}/git/refs/heads/{BRANCH}",
        {"sha": commit["sha"], "force": False})
    print(f"\n[ok] 已提交 {commit['sha'][:7]} -> {BRANCH}")
    print(f"     https://github.com/{OWNER}/{REPO}/commit/{commit['sha']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
