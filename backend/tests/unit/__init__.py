"""unit 測試（02 §2）：Service / RAG / ETL / 純邏輯，**不需要任何外部依賴**。

沒有 DB、沒有 Redis、沒有網路；LLM 一律 MockProvider。
CI 會單獨先跑這一層——它最快，壞掉時也最容易定位。
"""
