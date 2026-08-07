"""驗收：壓測腳本的每個 HTTP 呼叫都帶 timeout（CLAUDE.md、11 §4.1）。

**為什麼壓測腳本也算「對外呼叫」**：locust 的 ``HttpSession`` 預設沒有 timeout，
掛住的請求會無限占住一條 greenlet。對壓測來說這比對產品程式更毒——掛住的請求既不
算成功也不算失敗，直接從延遲分佈裡消失，同時 offered load 悄悄下降。得到的是一份
「看起來還可以」的數字，而 locustfile 開頭警告的環境雜訊至少還會抖，這個只會讓數字
變好看。基準線一旦被這種數字污染，之後每次比較都是錯的。

**為什麼用原始碼比對而不是 import 模組**：locust 在 ``loadtest`` 這個獨立的
dependency group（見 Makefile 的 ``LOCUST_BIN``），預設的 ``uv run`` 環境沒有它，
import 會直接 ImportError。這裡刻意接受「原始碼層斷言」這個較弱的形式，理由是
11 §4.1 明確要求「所有外呼必有 timeout，CI lint 稽核」——弱守門仍優於沒有。
"""

from __future__ import annotations

import re
from pathlib import Path

LOCUSTFILE = Path(__file__).resolve().parents[2] / "loadtest" / "locustfile.py"

# `self.client.get(...)` / `.post(...)` …——locust 的 HttpSession 呼叫。
# DOTALL + 非貪婪：呼叫是多行的，要抓到對應的右括號為止。
_CLIENT_CALL = re.compile(r"self\.client\.(get|post|put|patch|delete)\((.*?)\n\s*\)", re.DOTALL)


def test_every_client_call_passes_a_timeout() -> None:
    source = LOCUSTFILE.read_text(encoding="utf-8")
    calls = _CLIENT_CALL.findall(source)

    assert calls, f"在 {LOCUSTFILE.name} 找不到任何 self.client 呼叫——本守門會變成空轉"

    missing = [verb for verb, args in calls if "timeout=" not in args]
    assert not missing, (
        f"以下 self.client 呼叫沒帶 timeout：{missing}——"
        "掛住的請求會從延遲分佈裡消失，並讓 offered load 悄悄下降"
    )
