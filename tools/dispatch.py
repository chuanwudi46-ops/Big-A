#!/usr/bin/env python3
"""手动触发 GitHub Actions 工作流并等待结果（替代「打开网页点 Run workflow」）。

为什么需要它：
- 发布流程要求「改完 `web/` 必须重跑一次 workflow 才生效」，且换宇宙/大改后必须
  **先 warmup 再 score**。用网页点两次很容易点错顺序或点完就走人。
- 本机沙箱里 `curl` 不一定可用，直接用 Python 走 REST API 最稳。

用法:
    python tools/dispatch.py --stage warmup            # 触发并等待
    python tools/dispatch.py --stage score --wait 900  # 最多等 15 分钟
    python tools/dispatch.py --stage score --no-wait   # 触发即返回

退出码：0 = 目标 run 执行成功；1 = 失败/超时/取消。
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from api_push import OWNER, REPO, BRANCH, token   # noqa: E402  复用取 token 的逻辑

WORKFLOW = "daily.yml"
API = "https://api.github.com"


def api(tok: str, method: str, path: str, payload: dict | None = None):
    data = json.dumps(payload).encode() if payload is not None else None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(API + path, data=data, method=method)
    req.add_header("Authorization", f"Bearer {tok}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("Content-Type", "application/json")
    with opener.open(req, timeout=60) as resp:
        body = resp.read()
    return json.loads(body) if body else {}


def main() -> int:
    ap = argparse.ArgumentParser()
    # --stage 只在 daily.yml 需要：探针类 workflow 没有任何输入，
    # 硬要求 --stage 会逼着人给它传一个没人看的假参数。
    ap.add_argument("--stage", choices=["warmup", "score", "close"])
    ap.add_argument("--workflow", default=WORKFLOW,
                    help=f"要触发的 workflow 文件名（默认 {WORKFLOW}）")
    ap.add_argument("--wait", type=int, default=600, help="等待秒数上限（默认 600）")
    ap.add_argument("--no-wait", action="store_true")
    args = ap.parse_args()

    if args.workflow == WORKFLOW and not args.stage:
        ap.error("--stage 是必填（或显式 --workflow 指定别的 workflow）")

    tok = token()
    t0 = time.time()
    payload: dict = {"ref": BRANCH}
    if args.stage:
        payload["inputs"] = {"stage": args.stage}
    api(tok, "POST", f"/repos/{OWNER}/{REPO}/actions/workflows/{args.workflow}/dispatches",
        payload)
    print(f"[dispatch] {args.workflow} stage={args.stage or '-'} ref={BRANCH}")
    if args.no_wait:
        return 0

    run_id = None
    while time.time() - t0 < 90:                       # 先等 run 出现
        runs = api(tok, "GET",
                   f"/repos/{OWNER}/{REPO}/actions/workflows/{args.workflow}"
                   f"/runs?per_page=5&event=workflow_dispatch")["workflow_runs"]
        fresh = [r for r in runs if r["created_at"] >= time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(t0 - 30))]
        if fresh:
            run_id = fresh[0]["id"]
            break
        time.sleep(3)
    if run_id is None:
        print("[warn] 90 秒内没等到新 run（可能被并发组排队），请自行到 Actions 页核对")
        return 1

    print(f"[wait] run {run_id} …")
    while time.time() - t0 < args.wait:
        r = api(tok, "GET", f"/repos/{OWNER}/{REPO}/actions/runs/{run_id}")
        if r["status"] == "completed":
            print(f"[done] {r['conclusion']}  耗时 "
                  f"{int(time.time() - t0)}s  {r['html_url']}")
            return 0 if r["conclusion"] == "success" else 1
        time.sleep(5)
    print(f"[timeout] run {run_id} 仍未结束，见 Actions 页")
    return 1


if __name__ == "__main__":
    sys.exit(main())
