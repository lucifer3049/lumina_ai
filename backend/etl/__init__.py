"""ETL Pipeline（08）。

分層位置（02 §2、鐵則 2）：與 `repositories/`、`ai/`、`rag/`、`tool/` 同為內層，
**不得 import `api/`、`services/`、`apps/`、`repositories/`**——編排與資料存取都在
`services/`，這裡只做「來源 → 中間格式 → chunk」的純轉換。
"""
