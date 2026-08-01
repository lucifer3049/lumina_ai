# 11 NFR（一）：效能、擴充性、可用性、可靠性

| 項目 | 內容 |
|------|------|
| 文件編號 | 11 |
| 版本 | v1.0 |
| 日期 | 2026-07-30 |
| 狀態 | Draft — 待審閱 |
| 相依文件 | 01、05、06；姊妹篇 12_NFR_維運與AI特有需求 |

---

## 1. Performance（效能）

### 1.1 目標值（SLO 基準，p95）

| 指標 | 目標 | 備註 |
|------|------|------|
| 一般 API Response | < 300ms | CRUD / 列表 |
| Chat TTFT（首 token） | < 3.5s | 含 RAG 全鏈路；純聊天（無 RAG）< 1.5s |
| RAG 檢索段（vector+FTS+RRF） | < 400ms | 不含 rerank |
| Rerank | < 800ms | 外部模型呼叫，最脆弱段 |
| Embedding 吞吐 | > 500 chunks/min/worker | batch=64 |
| ETL：50 頁 PDF → ready | < 5min | SLO 為分鐘級（08 §7） |
| 文件上傳（100MB） | 直傳 MinIO，API 不經手 | 09 §3.1 |
| 併發串流 | 200 併發 SSE / api replica | 容量規劃單位 |

### 1.2 TTFT Latency Budget（3.5s 的分配）

```
condense(多輪) 300ms → query embed 150ms → hybrid search 400ms
→ rerank 800ms → compression 100ms → prompt build 50ms
→ LLM TTFT 1,500ms → 餘裕 200ms
```
超支處理：rerank 逾 1.2s 直接跳過（降級鏈）；condense 單輪跳過；此預算表是效能迴歸測試的斷言基準。

### 1.3 瓶頸分析與優化策略

| 瓶頸 | 對策 |
|------|------|
| Django ORM threadpool（ADR-001） | **主要**：PgBouncer transaction pooling + Django `CONN_MAX_AGE=300`（連線重用；實測單此一項吞吐 2.4–4.4 倍，見 05 §5.5）；熱路徑（conversation 載入）加 Redis cache<br>**次要**：threadpool size = 2×CPU 起步，依 queue depth metric 調——實測 12 / 24 / 48 無顯著差異，此旋鈕**不是**主要槓桿，勿優先調它 |
| pgvector HNSW 查詢 | `ef_search=80` 起步（recall/延遲平衡點以評測集實測）；`shared_buffers` 足以容納 index（容量見 §2.3）；超量後 partial index by KB 或分離 instance |
| pgroonga 大庫 | index 常駐記憶體；查詢帶 kb filter 縮範圍 |
| N+1 | Repository 一律顯式 `select_related/prefetch_related`；CI 掛 query count 斷言（關鍵端點 query 數上限） |
| SSE 高併發 | uvicorn worker 數（**每 replica 2，見 01 附錄 A 註 1**）× 200 串流；SSE 為 IO-bound，async 原生擅長；資源限制在 LLM provider 併發配額（Gateway 有 per-provider semaphore） |
| Celery 積壓 | 佇列分離（etl/embedding/default）+ per-queue worker 獨立 scale + 深度告警 |

### 1.4 Benchmark 方法

- 負載測試：Locust（API/Chat 混合場景腳本入 repo）；每 release 在 staging 跑基準組。
- 檢索品質×效能：評測資料集同時量 recall@k 與延遲，調參（top_k、ef_search）有據可依。
- 記錄基線：首次上線前建立 baseline 報告，之後回歸比對（>15% 劣化即 block release）。
- **負載產生器必須與受測系統分機**。ADR-001 spike 的量測在同一台機器上跑 locust 與 API，同設定重複執行的變異達 ±14%（workers=4 三次量到 549–733 rps），首輪甚至到 ±35%。這種雜訊下：
  - 差距 **> 50%** 的結論（如 `CONN_MAX_AGE` 0 vs 300 的 4.4 倍）仍然成立；
  - 差距 **< 20%** 的結論（如 uvicorn workers 2 vs 4 vs 6）**無效**，不得寫入容量規劃。
  - 上述 >15% 劣化即 block release 的門檻，在同機量測下低於雜訊底噪——**baseline 必須在分機環境重建後才生效**（Phase 0 收尾前完成）。

## 2. Scalability（擴充性）

### 2.1 無狀態原則

API / worker 全部無狀態（session 在 JWT+Redis、上傳直傳 MinIO、SSE resume buffer 在 Redis）→ **水平擴充 = 加 replica，無特殊條件**。唯一有狀態元件：PostgreSQL、Redis、MinIO。

### 2.2 各元件擴充路徑

| 元件 | 第一步（Compose） | 第二步 | 第三步 |
|------|-------------------|--------|--------|
| API | replica ×N + LB | K8s HPA（CPU+串流數自訂 metric） | — |
| Celery worker | per-queue 進程數 | K8s per-queue Deployment + KEDA（佇列深度驅動） | — |
| PostgreSQL | 垂直（記憶體優先，餵 HNSW） | read replica（報表/Analytics 讀分流） | 向量分離 instance → citus/分庫（遠期） |
| Redis | 單機 + AOF | Sentinel（HA） | Cluster（key 已 hash-tag 設計：`t:{tenant}` 前綴天然可分片） |
| MinIO | 單機 | 分散式 4 節點（erasure coding） | 雲物件儲存（S3 相容，零程式改動） |

### 2.3 容量規劃

| 規模 | 估算 | 結論 |
|------|------|------|
| 10 萬份文件 | ~1,500 萬 chunks；embedding(1536 float32) ≈ 92GB → **halfvec 減半 ≈ 46GB**；HNSW index ≈ 資料 1.1× | 單機 128GB RAM instance 可承載；halfvec 從 day-1 採用 |
| 100 萬份文件 | ~1.5 億 chunks、embedding ≈ 460GB(halfvec) | 超出單機甜蜜點 → 向量分離 instance + 分 KB partial index；此規模觸發 ADR-003 演進評估（Qdrant/Milvus） |
| 1,000 使用者（~100 併發） | chat 併發 ~30 串流、API ~200 rps | 2× api replica + 基準 DB 即可 |
| 10,000 使用者（~1,000 併發） | ~300 串流、~2,000 rps | api ×6、worker 分佇列擴充、DB read replica、Redis Sentinel；瓶頸预期在 LLM provider 配額（多 provider 分流 + 佇列化） |

**平滑升級原則**：每一步只改部署拓撲，不改程式（介面已抽象）；觸發條件量化（CPU >70% 持續、佇列深度、p95 劣化）寫入 runbook。

## 3. Availability(高可用)

### 3.1 元件 HA 需求分級

| 元件 | 等級 | 理由與做法 |
|------|------|------------|
| API / Gateway | 必須（≥2 replica） | 入口單點；LB health check 摘除 |
| PostgreSQL | 必須（Phase 2 起） | Compose 期：單機+自動重啟+PITR 備份（RTO 換簡單）；K8s 期：streaming replication + Patroni failover |
| Redis | 必須（Phase 2 起） | quota/ratelimit/resume 依賴；Sentinel；**降級設計：Redis 不可用時 rate limit fail-open + quota 走 DB 慢路徑，服務不中斷** |
| Celery worker | 天然 HA | 多進程互備；任務冪等可重跑 |
| MinIO | 中 | 讀路徑（RAG）不依賴 MinIO；僅上傳/匯出受影響 → 可接受短暫降級 |
| LLM Provider | 外部 | fallback 鏈（ADR-005/06 §4）即其 HA 機制 |

### 3.2 健康檢查與部署

- `/healthz`（liveness：進程活著）與 `/readyz`（readiness：DB/Redis 可達、migration 已跑、provider 探測不阻擋——外部依賴不納入 readiness，避免外因摘掉全部節點）。
- **Graceful shutdown**：SIGTERM → 停收新請求 → SSE 送 `event: error(retryable)` 並等待 ≤30s → Celery `warm shutdown`（任務跑完不領新）→ 退出。
- **Zero downtime**：rolling update（LB drain + 新舊並存）；migration 向後相容三步走（05 §5.6）是零停機的前提；Compose 期用 `docker compose up` 的 rolling 腳本（start-first）。

## 4. Reliability（可靠性）

### 4.1 通用機制

| 機制 | 規格 |
|------|------|
| Timeout 全域字典 | DB 5s / Redis 500ms / MinIO 30s / LLM 見 06 §4 / tool 見 policy / HTTP 對外 15s——**所有外呼必有 timeout，CI lint 稽核** |
| Retry | 僅冪等操作；指數退避 + jitter；上限 3；retry 必記 metric（高 retry 率 = 早期警訊） |
| Circuit Breaker | 對外依賴（LLM provider、rerank、外部 tool）：失敗率窗口式熔斷（07 §3.3 同規格）；open 時走 fallback 或明確報錯 |
| Idempotency | API 層 Idempotency-Key（09）；task 層冪等鍵（08 §6）；事件消費 at-least-once + 冪等處理 |
| DLQ | Celery 重試耗盡 → dead letter queue（Redis list + DB 記錄）→ 告警 → `/admin` DLQ 重放介面 |
| Backpressure | 佇列深度上限 → 上游 429（帶 Retry-After）；ETL per-tenant 公平佇列（08 §6） |

### 4.2 AI 相關故障處理（題目指定四情境）

| 故障 | 處理 |
|------|------|
| **AI API Timeout** | TTFT 前：fallback 鏈換模型重試（使用者無感，僅 metadata 記錄）；串流中：不換模型（拼接不一致），SSE `error(retryable)` + partial 持久化，使用者按重生成 |
| **Embedding 失敗** | 批次中單筆失敗 → 記錄續走；整批失敗 → stage 重試（退避）；provider 持續故障 → circuit open、job 暫停、恢復後自動續跑（狀態機斷點）；**絕不寫入部分維度或零向量** |
| **ETL 失敗** | 08 §6：stage 級重試、毒檔不重試、DLQ + 通知、斷點續跑；document 永遠處於明確狀態（不會卡 processing——stale job 巡檢器（Beat）將逾時 job 標 failed） |
| **Tool 失敗** | 07 執行鏈：retry（冪等）→ circuit breaker → 結構化錯誤回 LLM（LLM 可解釋或改道）→ 對話不中斷；工具連續失敗告警 |

### 4.3 一致性與交易

- 交易邊界 = Service 方法（UoW）；跨聚合以 Domain Event 最終一致。
- **Outbox pattern**：DB 寫入與事件發佈同交易（outbox 表）→ relay 投遞 Celery——防「資料已寫、事件丟失」不一致；outbox relay 是 Compose 期唯一額外常駐小進程。
- 對帳機制：quota Redis 計數 vs usage_logs 日結校正（04 §8.1）；MinIO 孤兒物件週掃。

## 5. 優點 / 缺點 / 適用情境

**優點**：目標值全部量化且綁定測試斷言（latency budget、query count、baseline 比對），NFR 不是口號是迴歸項；降級設計成體系（rerank 跳過、Redis fail-open、fallback 鏈），單一依賴故障不放大。
**缺點**：Compose 期 PostgreSQL 單機是明確的可用性妥協（以 PITR+快速重啟兜底，RTO 見 12_NFR）；outbox relay 增加一個小元件。
**適用情境**：SLA 99.5%（Compose 期）→ 99.9%（K8s + DB HA 後）的承諾範圍。

## 6. Architecture Review

1. **KISS/YAGNI**：未引入 service mesh、多區域、讀寫分離 day-1；每項擴充有量化觸發條件。
2. **可測試性**：SLO → Locust 斷言、budget → 迴歸測試、故障 → chaos 腳本（kill provider mock）。
3. **Technical Debt**：Compose 期 DB 單機（已記錄，Phase 2 解）；quota fail-open 在 Redis 故障窗口有超額風險（金額上限低，接受）。
4. **更好方案**：無結構性更優；halfvec day-1 採用已回寫 05（v1.1）保持一致（15 審查報告 F-01 已結案）。

---

*同階段文件：12_NFR_維運與AI特有需求.md。*
