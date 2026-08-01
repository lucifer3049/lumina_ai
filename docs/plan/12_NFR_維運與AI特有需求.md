# 12 NFR（二）：可觀測性、維運、DR、成本、CI/CD、Cloud Native、AI 特有需求

| 項目 | 內容 |
|------|------|
| 文件編號 | 12 |
| 版本 | v1.1 |
| 日期 | 2026-07-30 |
| 狀態 | Draft — 待審閱 |
| 相依文件 | 11_NFR_效能與可用性；06（rag_trace）、10（安全） |
| 變更紀錄 | v1.1：§3 告警改寫為「自癒優先」模型（開發模式定調為 1 人 + AI，無 24/7 值班團隊）。v1.2：§6 測試策略補前端層（vitest；15 審查報告 F-03） |

---

## 1. Observability（可觀測性）

### 1.1 三支柱規格

| 支柱 | 方案 | 要點 |
|------|------|------|
| Logging | structlog → JSON stdout → Loki（Grafana stack） | 標準欄位：ts、level、logger、request_id、trace_id、tenant_id、user_id(hash)、event；PII 自動遮罩 processor（10 §5）；等級紀律：ERROR=需人看、WARN=需統計、INFO=業務事件 |
| Metrics | Prometheus client → /metrics | RED（rate/error/duration per endpoint）+ 業務指標（§1.2）；高基數限制：label 不放 user_id/doc_id |
| Tracing | OpenTelemetry SDK → Tempo/Jaeger | FastAPI/Celery/httpx/psycopg 自動 instrument；**跨進程傳播**：API → Celery task headers 帶 traceparent → LLM 呼叫為 span；取樣：錯誤全採、正常 10% |

Correlation：`request_id`（對外可見）+ `trace_id`（內部關聯）雙軌；SSE 事件、audit、usage_logs、rag_trace 全部帶 request_id——一個 ID 查穿全鏈路。

### 1.2 必監控指標（核心清單）

**系統**：API RED、DB（連線數/慢查詢/replication lag）、Redis（記憶體/命中率）、Celery（佇列深度/任務時長/失敗率）、threadpool queue depth（ADR-001 專屬）。
**AI**：LLM TTFT/總延遲/錯誤率/fallback 觸發率（per provider+model）、token 用量與成本速率（per tenant）、embedding 吞吐、rerank 延遲/跳過率、RAG 檢索延遲/候選數/rerank 分數分布、citation 驗證失敗率、prompt cache 命中率、tool 成功率/時長/circuit 狀態、groundedness 抽測分數趨勢。
**業務**：串流併發、對話數、文件 ready 延遲、quota 使用率 per tenant。

## 2. Monitoring Dashboards

| Dashboard | 內容 | 受眾 |
|-----------|------|------|
| System Overview | API RED、佇列、DB/Redis 健康、SLO 燃盡 | 值班 |
| AI Operations | provider 延遲/錯誤/fallback、token 成本速率、模型分布 | AI 維運 |
| RAG Quality | 檢索延遲分布、rerank 分數、citation 失敗率、groundedness 趨勢、評測 run 結果 | AI 工程 |
| ETL Pipeline | 佇列深度、stage 時長、失敗/DLQ、丟棄率警示、per-tenant 積壓 | 維運 |
| Tenant Analytics | per-tenant 用量/成本/quota、Top N 消耗 | 營運/成本 |
| Infra | CPU/Mem/Disk/Network（node exporter + cadvisor） | 維運 |

## 3. Alerting（告警）——自癒優先模型

**設計前提：維運人力為 1 人 + AI，無 24/7 值班團隊。** 因此告警體系的第一原則是**自癒優先於告警**：凡機器能自動處理的故障一律機制化，告警只保留「機器救不了、且不處理會持續擴大」的少數情況。

### 3.1 自癒層（不告警，只記錄）

| 故障 | 自動處置 |
|------|----------|
| 容器 crash / 無回應 | restart policy + LB 摘除，恢復後自動回歸 |
| 單 provider 故障 | circuit breaker + fallback 鏈自動切換（僅記 metric） |
| 暫時性任務失敗 | Celery 重試 + 退避（耗盡才升級為告警） |
| stale job 卡死 | Beat 巡檢器自動標 failed、可重跑者自動重排 |
| Redis 短暫不可用 | rate limit fail-open + quota DB 慢路徑（11 §3.1） |
| SSE 斷線 | client 端自動 reconnect + resume |

### 3.2 告警層（真正需要人）

| 等級 | 條件（節選） | 通道與預期回應 |
|------|-------------|----------------|
| **P1（推播，盡快處理）** | DB down 且自動重啟失敗；磁碟 >90%；**全 provider 皆失敗**（fallback 鏈耗盡）；備份連續失敗；憑證/金鑰過期 <72h；單租戶成本熔斷觸發 | 即時推播；非工作時間以「服務降級可接受」為前提設計（見 3.3） |
| **P2（Slack/Email，當日處理）** | DLQ 新增；佇列深度超閾值 15min；ETL 失敗率 >20%；API 5xx >5%/5min（未自癒）；慢查詢激增；單租戶成本速率 >3× 基線 | 工作時間內處理 |
| **P3（週報彙整）** | groundedness 趨勢下滑；cache 命中率下滑；索引膨脹；備份時長劣化 | 每週檢視 |

### 3.3 告警紀律

- 每條告警必附 runbook 連結；連續誤報 3 次必須修閾值或刪除（防告警疲勞）——單人維運下告警疲勞是致命的，此條嚴格執行。
- **SLA 對外承諾與人力誠實對齊**：Compose 期對外承諾「工作時間內回應、系統自癒能力涵蓋常見故障」，不承諾 24/7 人工回應；團隊擴編後再升級承諾。
- 每次 P1/P2 事後：若該故障可機制化自癒，優先補自癒機制而非只修當下問題（告警清單應隨時間變短，不是變長）。

## 4. Disaster Recovery（災難復原）

### 4.1 備份策略

| 資產 | 方式 | 頻率 | RPO |
|------|------|------|-----|
| PostgreSQL（含 pgvector、prompt、audit） | pgBackRest：全量週 + 增量日 + **WAL 歸檔（PITR）** | 連續 | **≤ 5min** |
| MinIO | 版本化 bucket + 異地 rclone 同步 | 日 | ≤ 24h |
| Redis | AOF everysec；**定位為可重建快取**——quota 以 DB 對帳恢復 | — | 可丟（設計保證） |
| 組態/Secrets | IaC in git + SOPS 密文；Secrets Manager 自身備份 | 即時 | ~0 |

Embeddings 特別註記：隨 DB 備份；極端情境可由 chunks 重算（成本 = embedding API 費用，百萬 chunk 約數百美元 + 數小時），故 DB 備份是唯一關鍵路徑。

### 4.2 RTO / 演練

| 情境 | RTO 目標 |
|------|----------|
| 單容器故障 | < 1min（自動重啟/LB 摘除） |
| DB 毀損 → PITR 還原 | < 2h（Compose 期）→ < 15min（K8s + standby） |
| 整機失聯 → 異地重建 | < 8h（IaC + 備份還原，演練驗證） |

紀律：**每季還原演練**（未演練的備份視同不存在）；恢復 runbook 步驟化並計時。

## 5. Cost Optimization（成本最佳化）

| 槓桿 | 做法 | 預期效果 |
|------|------|----------|
| Model Routing | condense/摘要/標題生成用小模型；主回答依租戶 plan；routing 規則集中 Gateway | LLM 成本 -30~50% |
| Prompt Caching | 前綴穩定設計（06 §4）；provider cache 命中計入成本報表 | 輸入 token 成本 -50~90%（長 context 對話） |
| Embedding Cache | content hash 去重 + cache（06 §6）；re-ingest 未變更 chunk 零成本 | 重複內容零重算 |
| Context 精實 | rerank 後只留 6-8 chunks + compression；token budget 硬上限 | 每請求輸入 token 可控 |
| halfvec | 向量儲存減半（11 §2.3） | 記憶體/儲存 -50% |
| 分區 + 歸檔 | 冷資料 DETACH 轉冷儲存 | DB 儲存可控 |
| 熔斷 | 單租戶日成本熔斷（10 §8.2）；平台月預算告警 | 防失控帳單 |
| 監控閉環 | Tenant Analytics dashboard + 成本異常告警（§3 P2） | 異常 30min 內可見 |

## 6. CI/CD & DevOps

### 6.1 流程

- **Branch**：trunk-based——main 保護、短命 feature branch、PR 必經 CI + 1 review；release tag 觸發部署。
- **Pre-commit**：ruff（lint+format，取代 black/isort 三合一）、mypy、eslint/prettier、conventional commits 檢查。
- **CI Pipeline**（PR）：lint/type（ruff/mypy + eslint/vue-tsc）→ unit（後端 pytest + **前端 vitest**，並行）→ integration（testcontainers: PG+Redis+MinIO）→ API test → build image → trivy 掃描 → OpenAPI diff（09 §4）→ import-linter → migration check → query-count 斷言。
- **CI（nightly）**：E2E（compose 全鏈路）、評測資料集迴歸（prompt/檢索參數變更的品質門檻）、Locust 基準、pip-audit/npm audit、SBOM（syft）+ license 檢查（G-18：拒 GPL 類入依賴）。
- **CD**：staging 自動部署 → 冒煙測試 → 手動核准 → production rolling；rollback = 重部署前一 tag（migration 向後相容保證可回退）；image 以 digest 固定。

### 6.2 測試策略（G-15 細化）

| 層 | 範圍 | LLM 處理 |
|----|------|----------|
| Unit（後端） | Service/RAG/ETL/Tool 純邏輯 | **MockProvider**（錄製回放 + 情境注入：timeout/429/malformed）——禁真實 API |
| Unit（前端） | composables/services/stores（vitest；詳見 03 §6.1） | SSE 以 mock EventSource；API 以 msw mock |
| Integration | Repository+RLS、快取、outbox | 不涉 LLM |
| API | 權限矩陣、錯誤格式、SSE 協定 | MockProvider 串流 |
| E2E | 上傳→ready→問答→引用（Playwright） | staging 可用真實小模型（成本上限） |
| 評測 | RAG 品質（golden set） | 真實模型、nightly、預算帽 |

factory_boy 對映全表；tenant fixture 雙租戶標配（隔離測試內建）。

## 7. Cloud Native Readiness（Compose → K8s 平滑升級）

**Day-1 已備**：全元件容器化、12-factor（設定=env、log=stdout、無狀態）、health/readiness 端點、graceful shutdown、單一 image 多角色（api/worker/beat 同 image 不同 command）。

**升級步驟**（每步可獨立回退）：
1. Helm chart 化（api/worker/beat Deployment、HPA、Ingress、ESO secrets）——應用零改動。
2. 有狀態遷出：managed PostgreSQL（含 pgvector 支援確認/自管 Patroni）、managed Redis、S3 相容儲存——連線字串替換。
3. KEDA（佇列深度驅動 worker）、PodDisruptionBudget、topology spread。
4. 觀測棧沿用（Prometheus Operator / Loki / Tempo）。

**判斷點**：租戶數或負載觸發 11 §2 條件、或需要 SLA 99.9%，才啟動——K8s 不是目標，是工具。

## 8. AI-Specific NFR 彙整

多數已散落各文件定案，此處彙整為單一對照（原提示詞 §13 逐項）：

| 需求 | 定案位置 | 補充 |
|------|----------|------|
| Prompt/Model/Embedding/Knowledge/Chunk Versioning | 04/05/06 | 全鏈路快照可回溯 |
| Prompt Rollback | 09（activate 端點） | 指標回歸即一鍵回退 |
| 各類 Cache | 06 §6 | 版本化失效原則 |
| Prompt/LLM Evaluation、RAG Recall/Precision | 04 Evaluation、06 §3.3、§6.2 nightly | CI 品質門檻：評測分數低於基線 -5% 即 block |
| Hallucination Detection、Citation Validation、Groundedness/Faithfulness | 06 §3.3 | 線上抽測 5% + 趨勢告警 |
| Context Window / Memory Strategy | 06 §3.2/§5 | budget 表 |
| Prompt Injection / Tool Abuse Detection | 10 §8、07 §3.4 | 紅隊迴歸 |
| Token Budget / Cost Monitoring | 本文件 §5、Quota 模組 | 熔斷 + 告警 |
| Model Routing / Fallback / Multi-Model | 06 §4、Model 模組 | per-tenant 設定 |

## 9. 優點 / 缺點 / 適用情境 / Review

**優點**：觀測三支柱 + request_id 貫穿使 MTTR 可控；備份含 PITR 且季度演練；成本槓桿全部機制化（非人工紀律）；CI 把架構紀律（import-linter、query count、OpenAPI diff、評測門檻）變成強制項。
**缺點**：觀測棧（Grafana/Loki/Tempo/Prometheus）自管有維運成本——Compose 期以精簡 profile 起步，K8s 期考慮 managed。
**Review 重點**：(1) KISS——工具鏈全部標準開源組合，無自研；(2) YAGNI——多區域 DR、SOC2 認證流程列為商業觸發項；(3) Technical Debt——nightly 評測依賴 golden set 品質，初期集小、需持續擴充（已列 Roadmap 常設任務）；(4) 無結構性更優方案。

---

*下一步：確認 Stage 9 兩份文件後，進行最終 Stage 10（13_開發Roadmap.md、14_Production_Checklist.md）。*
