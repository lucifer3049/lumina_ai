#!/usr/bin/env python3
"""查本 repo 最近一次 CI run 的結果（`make ci-status`）。

存在的理由：CI 曾自 2026-08-18 起連紅四次（run 57–60）無人察覺——
`test_ci_pipeline.py` 只防「workflow 步驟缺漏」，防不了「內容真的紅」。
push 之後跑這一個指令盯到終局，是 CLAUDE.md Git 規則的一部分。

設計取捨：

- **不硬性依賴 gh CLI**：多裝一個工具就多一個「本機有、CI 教訓現場沒有」的
  變因（最初除錯時 gh 正好不在）。但**匿名只在公開 repo 成立**——GitHub 對
  私有資源一律回 404（連 repo 存在與否都不透露），本 repo 轉私有之後這支腳本
  就再也查不到任何 run。因此 token 分三層：環境變數 → `gh auth token` → 匿名，
  三層都是可選的，缺席時往下退，全缺就是原本的匿名行為。
- **對照的是本機 HEAD**：latest run 可能屬於別人剛推的 commit；答錯 commit
  的綠燈比沒有答案更糟。找不到本機 HEAD 的 run 時明講，不拿別的 run 充數。
- **進行中就輪詢**（15s 間隔、20 分鐘上限）：這個指令的用途是「盯到終局」，
  回一句 in_progress 就退出的話，使用者還是得自己記得回來看。**run 尚未建立
  也一樣輪詢**（另計 90 秒）——push 到 run 出現有數十秒延遲，而典型用法就是
  push 完立刻跑。
"""

from __future__ import annotations

import functools
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

API_TIMEOUT_S = 10  # 對外呼叫必有 timeout（CLAUDE.md）
POLL_INTERVAL_S = 15
POLL_BUDGET_S = 20 * 60
# push 到 run 出現有數十秒延遲，而本指令的典型用法就是 push 完立刻跑。與上面的
# 預算分開計：「還沒建立」與「跑太久」是兩件事，混用會讓 90 秒的等待吃掉 20 分鐘。
CREATE_BUDGET_S = 90


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


@functools.cache
def token() -> str | None:
    """token 三層：環境變數 → `gh auth token` → 匿名（None）。

    私有 repo 下匿名一定 404，而 404 與「repo 打錯字」長得一模一樣——所以取不到
    token 不在這裡失敗，改由 `api()` 在真的 404 時把兩種可能一起講出來。
    順帶把匿名的 60 次/小時 rate limit 拉到 5000。
    """
    for name in ("GITHUB_TOKEN", "GH_TOKEN"):
        value = os.environ.get(name)
        if value:
            return value
    try:
        result = subprocess.run(
            ["gh", "auth", "token"],
            capture_output=True,
            text=True,
            check=True,
            timeout=API_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return None  # gh 沒裝或沒登入，退回匿名
    return result.stdout.strip() or None


def api(path: str) -> dict:
    headers = {"Accept": "application/vnd.github+json"}
    bearer = token()
    if bearer is not None:
        headers["Authorization"] = f"Bearer {bearer}"
    request = urllib.request.Request(f"https://api.github.com{path}", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=API_TIMEOUT_S) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        # 404 在 GitHub 上是「不存在**或**你看不到」的合稱：私有 repo 對匿名請求
        # 就回這個。少了這句提示，症狀（查不到任何 run）會被讀成「CI 沒觸發」。
        #
        # **不能只在匿名時提示**：有 token 而看不到才是私有 repo 下最常見的那一種
        # （token 過期、fine-grained token 少了 Actions: read、或 `gh auth token`
        # 給的是別台 GHE 的 token），GitHub 一樣回 404。把那個情況丟回泛用訊息，
        # 等於把提示留給最不需要它的人。所以一律攔 404，只依有無 token 換說法。
        if error.code == 404:
            hint = (
                "而目前沒有 token。設 GITHUB_TOKEN／GH_TOKEN，或 `gh auth login` 後重跑。"
                if bearer is None
                else "而目前這個 token 看不到它——過期、權限不含 Actions: read、或它屬於"
                "另一個 GitHub 主機。換一個 token 或 `gh auth login` 後重跑。"
            )
            raise SystemExit(f"GitHub API 404（{path}）——repo 不存在，或它是私有的{hint}") from error
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

    # 這裡輪詢的是「run 有沒有被建立」，下面那個迴圈輪詢的是「跑完了沒」。
    # 原本這一段是 `return 3`，而 push 完立刻跑必然撞上建立延遲——那個離開碼
    # 與「CI 真的沒觸發」完全一樣，於是每次都得靠人去 GitHub 上再看一次。
    create_deadline = time.monotonic() + CREATE_BUDGET_S
    run = find_run(slug, sha)
    while run is None:
        if time.monotonic() > create_deadline:
            print(
                f"{sha[:7]} 等了 {CREATE_BUDGET_S} 秒仍無 CI run——確認已 push、"
                "Actions 未被停用，且 workflow 的觸發條件涵蓋本分支。"
            )
            return 3
        print(f"{sha[:7]} 的 run 尚未出現，{POLL_INTERVAL_S}s 後再查…")
        time.sleep(POLL_INTERVAL_S)
        run = find_run(slug, sha)

    # 完成預算從 run 真的存在之後才起算——與上面的建立預算共用一個 deadline 的話，
    # 等了 80 秒才出現的 run 只剩 18 分半可跑。
    deadline = time.monotonic() + POLL_BUDGET_S
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
