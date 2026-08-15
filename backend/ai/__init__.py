"""AI 子系統（04 §5）——Gateway、Prompt、Model routing。

分層位置（02 §2、鐵則 2）：與 `etl/`、`rag/`、`tool/` 同為內層，**不得 import
`api/`、`services/`、`apps/`、`repositories/`**。上層要用它，由 service 呼叫進來。

鐵則 5 的實施點在這裡：**所有 LLM / embedding / rerank 呼叫都經 `ai/gateway/`**，
provider SDK 只准出現在 `ai/gateway/providers/`（由
`tests/unit/test_ai_gateway.py::TestProviderSdkIsolation` 掃原始碼守門）。
"""
