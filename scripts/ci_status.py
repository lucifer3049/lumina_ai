#!/usr/bin/env python3
"""查本 repo 最近一次 CI run 的結果（`make ci-status`）。

存在的理由：CI 曾自 2026-08-18 起連紅四次（run 57–60）無人察覺——
`test_ci_pipeline.py` 只防「workflow 步驟缺漏」，防不了「內容真的紅」。
push 之後跑這一個指令盯到終局，是 CLAUDE.md Git 規則的一部分。

設計取捨：

- **不依賴 gh CLI**：repo 是公開的，匿名 REST API 就查得到；多裝一個工具
  就多一個「本機有、CI 教訓現場沒有」的變因（這次除錯時 gh 正好不在）。
- **對照的是本機 HEAD**：latest run 可能屬於別人剛推的 commit；答錯 commit
  的綠燈比沒有答案更糟。找不到本機 HEAD 的 run 時明講，不拿別的 run 充數。
- **進行中就輪詢**（15s 間隔、20 分鐘上限）：這個指令的用途是「盯到終局」，
  回一句 in_progress 就退出的話，使用者還是得自己記得回來看。
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

API_TIMEOUT_S = 10  # 對外呼叫必有 timeout（CLAUDE.md）
POLL_INTERVAL_S = 15
POLL_BUDGET_S = 20 * 60


def repo_slug() -> str:
    """從 origin 推導 owner/repo，SSH 與 HTTPS 兩種形式都接。"""
    url = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        capture_output=True,
        text=True,
        check=True,
        timeout=API_TIMEOUT_S,
    ).stdout.strip()
    match = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?$", url)
    if match is None:
        raise SystemExit(f"origin 不是 GitHub remote：{url}")
    return match.group(1)


def local_head() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
        timeout=API_TIMEOUT_S,
    ).stdout.strip()


def api(path: str) -> dict:
    request = urllib.request.Request(
        f"https://api.github.com{path}",
        headers={"Accept": "application/vnd.github+json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=API_TIMEOUT_S) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        # 403 常見於匿名 rate limit（每 IP 每小時 60 次）；訊息要指得到原因。
        raise SystemExit(f"GitHub API {error.code}：{error.reason}（{path}）") from error


def find_run(slug: str, sha: str) -> dict | None:
    runs = api(f"/repos/{slug}/actions/runs?head_sha={sha}&per_page=1")
    workflow_runs = runs.get("workflow_runs", [])
    return workflow_runs[0] if workflow_runs else None


def print_jobs(slug: str, run_id: int) -> None:
    for job in api(f"/repos/{slug}/actions/runs/{run_id}/jobs").get("jobs", []):
        mark = "✅" if job["conclusion"] == "success" else "❌"
        print(f"  {mark} {job['name']} => {job['conclusion']}")
        for step in job.get("steps", []):
            if step["conclusion"] not in ("success", "skipped", None):
                print(f"       失敗步驟：{step['name']}")


def main() -> int:
    slug = repo_slug()
    sha = local_head()
    deadline = time.monotonic() + POLL_BUDGET_S

    run = find_run(slug, sha)
    if run is None:
        print(f"找不到 {sha[:7]} 的 CI run——還沒 push，或 workflow 尚未觸發。")
        return 3

    while run["status"] != "completed":
        if time.monotonic() > deadline:
            print(f"輪詢超過 {POLL_BUDGET_S // 60} 分鐘仍未完成：{run['html_url']}")
            return 3
        print(f"run #{run['run_number']}（{sha[:7]}）{run['status']}…每 {POLL_INTERVAL_S}s 再查")
        time.sleep(POLL_INTERVAL_S)
        run = find_run(slug, sha)
        assert run is not None  # 已存在的 run 不會消失

    print(f"run #{run['run_number']}（{sha[:7]}）=> {run['conclusion']}")
    print(f"  {run['html_url']}")
    if run["conclusion"] == "success":
        return 0
    print_jobs(slug, run["id"])
    return 1


if __name__ == "__main__":
    sys.exit(main())
