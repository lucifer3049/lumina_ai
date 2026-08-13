"""Celery task（02 §2）—— **薄包裝**：取 context → 呼叫 service → 回報。

邏輯寫進 task 的話，它就只能被 Celery 呼叫：重跑一份文件得發一個訊息、測試要起
broker，而 service 明明可以直接呼叫（`tests/integration/test_ingestion.py` 正是如此）。

分層（鐵則 2）：`worker/` 與 `api/` 同為最外層，可以 import `services/`，
但不得 import `repositories/`、`apps/`——ORM 只在 repository 裡。
"""
