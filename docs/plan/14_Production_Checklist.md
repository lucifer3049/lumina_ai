# 14 Production Ready Checklist

| 項目 | 內容 |
|------|------|
| 文件編號 | 14 |
| 版本 | v1.0 |
| 日期 | 2026-07-30 |
| 用途 | GA 前最終驗收閘門（Phase 4 DoD）；每項須附證據（測試報告/演練紀錄/掃描結果），非口頭確認 |

---

## 1. 總體檢查（原提示詞 §14 逐項）

### □ Production Ready
- [ ] 所有 SLO 在 ×1.5 目標負載壓測下達標（11 §1.1；Locust 報告）
- [ ] 全部 P1/P2 告警經人為故障注入驗證觸發（12 §3）
- [ ] Graceful shutdown / rolling update 實測零斷線（11 §3.2）
- [ ] 無 `TODO(blocking)` / 已知 P0-P1 bug 清零

### □ Enterprise Best Practice
- [ ] 分層與 import-linter 契約 CI 全綠（02 §3）
- [ ] OpenAPI 契約治理生效（oasdiff + codegen 同步；09 §4）
- [ ] Migration 三步走規則有 CI 防護（05 §5.6）
- [ ] 全域錯誤格式/分頁/冪等慣例無例外端點（09 §1）

### □ AI Best Practice
- [ ] Prompt 版本化：published 不可變、activate 可回退實測（04/09）
- [ ] 全鏈路版本快照：任一歷史回答可查到 prompt_version/model/chunk 來源（06）
- [ ] Provider 抽象：切換 provider 不改業務碼實測（Gateway）
- [ ] LLM 測試全 Mock，CI 無真實 API 呼叫（12 §6.2）

### □ Security Best Practice
- [ ] 跨租戶矩陣測試全綠（10 §4；每資源 × 讀寫 × 他租戶 → 404/403）
- [ ] 外部滲透測試完成且 High 以上全修（Phase 4）
- [ ] AI 紅隊集通過率 ≥ 基線（10 §8.3）
- [ ] Secrets 掃描（gitleaks）零洩漏；憑證輪替 runbook 演練過
- [ ] OWASP Top 10 + LLM Top 10 對照表逐項有對策證據（10 §9）
- [ ] 依賴掃描（pip-audit/npm audit/trivy）無 Critical；SBOM 產出；License 檢查通過（無 GPL 污染）

### □ Cloud Native Best Practice
- [ ] 12-factor 檢核：設定=env、log=stdout、無狀態實測（kill 任一 api/worker 容器服務不中斷）
- [ ] health/readiness 語意正確（外部依賴不摘節點；11 §3.2）
- [ ] 單一 image 多角色、image 以 digest 固定、non-root 執行

### □ Maintainability
- [ ] 新人 onboarding 實測：30 分鐘起環境、半天內合入首個 PR
- [ ] 文件完備：00–14 全系列 + runbook + ADR 與程式碼一致（抽查）
- [ ] 測試覆蓋：Service 層 ≥80%、關鍵路徑（chat/etl/quota/auth）≥90%

### □ Scalability
- [ ] 無狀態驗證：api ×3 replica 下 SSE/上傳/quota 行為正確
- [ ] 容量規劃參數實測校正（halfvec 實際佔用 vs 11 §2.3 估算）
- [ ] 各擴充觸發條件寫入 runbook 且有對應 dashboard（11 §2）

### □ Testability
- [ ] 四層測試（unit/integration/api/e2e）CI 全自動
- [ ] MockProvider 情境庫涵蓋：timeout/429/malformed/串流中斷
- [ ] 評測 golden set ≥100 題且 nightly 門檻生效（12 §6.1）

### □ Observability
- [ ] request_id 全鏈路貫穿實測（API→Celery→LLM→log/trace/audit 一 ID 查穿）
- [ ] 六 Dashboard 上線且值班者受訓（12 §2）
- [ ] PII 不入 log 抽查通過（10 §5）

### □ Reliability
- [ ] 四大 AI 故障情境注入測試通過（11 §4.2：provider timeout/embedding 失敗/ETL 毒檔/tool 失敗）
- [ ] Outbox：DB 寫入後 kill relay，事件不丟失實測
- [ ] DLQ 重放流程實測；stale job 巡檢生效
- [ ] Redis 故障降級實測（fail-open + DB 慢路徑；11 §3.1）

### □ Disaster Recovery
- [ ] PITR 還原演練：RPO ≤5min、RTO ≤2h 實測計時（12 §4）
- [ ] 異地重建演練完成（IaC + 備份 → 完整服務）
- [ ] MinIO 版本化 + 異地同步驗證；備份監控告警生效

### □ Cost Optimization
- [ ] 成本八槓桿全部生效並可在 dashboard 觀測（12 §5）
- [ ] 租戶日成本熔斷實測；平台月預算告警設定
- [ ] Model routing 實測（小模型分流比例達設計值）

### □ Multi Tenant
- [ ] 隔離四層實測（DB/RLS、Redis 前綴、MinIO prefix、檢索 filter）
- [ ] Quota 全資源類強制生效（token/文件/儲存/併發）
- [ ] 租戶生命週期：onboarding 自動化、offboarding 級聯清理實測（含 vector/MinIO/Redis 殘留掃描）
- [ ] 租戶資料匯出（可攜權）實測

### □ AI Governance
- [ ] Audit 涵蓋敏感操作全清單（04 §8.3）；審計不可竄改（append-only）驗證
- [ ] 資料保留政策生效（到期歸檔/刪除 job 實測）
- [ ] Groundedness 線上抽測運行中且有趨勢告警
- [ ] DPA/no-training 條款文件化（10 §8.2）；PII 政策租戶可組態

### □ Microservice 演進準備
- [ ] Bounded Context 邊界 CI 強制無違規（import-linter 報告）
- [ ] EventPublisher/Provider/Retriever 抽象介面替換性 code review 確認
- [ ] AI Gateway 拆分預演文件（第一拆分對象；01 §6）

---

## 2. 已知缺漏與改善方案（誠實清單）

| # | 缺漏 | 優先級 | 改善方案與時點 |
|---|------|--------|----------------|
| 1 | Compose 期 PostgreSQL 單機（HA 妥協） | 高 | K8s/managed DB 遷移（Phase 5 觸發條件見 12 §7）；期間以 PITR+RTO 2h 兜底 |
| 2 | injection 防禦為機率性 | 高（已兜底） | 權限上限為確定性兜底（10 §8.2）；偵測模型持續迭代、紅隊集每季擴充 |
| 3 | 掃描件 OCR 品質 | 中 | 預設關閉、明確告知租戶；需求出現後 plugin loader 接外部解析服務（08 §8.7） |
| 4 | golden set 初期規模小 | 中 | 常設任務每 sprint 增補；beta 真實問題回流（13 R5） |
| 5 | 觀測棧自管維運成本 | 中 | K8s 期評估 managed（Grafana Cloud）；Compose 期精簡 profile |
| 6 | SOC 2 / ISO 27001 未認證 | 低（商業觸發） | 控制項已對應（10 §10），enterprise 客戶出現時啟動認證程序 |
| 7 | 多區域 DR | 低 | 異地備份已有；多區域 active 待商業需求 |

## 3. 簽核

| 角色 | 確認範圍 | 簽核 |
|------|----------|------|
| Tech Lead | 全部技術項 | ☐ |
| Security Owner | Security / AI Governance 節 | ☐ |
| Ops Owner | DR / Observability / Alerting 節 | ☐ |
| Product Owner | Beta 回饋處理完畢、GA 範圍確認 | ☐ |

---

*本文件為 00–14 系列之終章。系列文件構成完整開發藍圖：00 總覽 → 01 架構 → 02/03 結構 → 04 模組 → 05 資料庫 → 06 AI Pipeline → 07 Tool → 08 ETL → 09 API → 10 安全 → 11/12 NFR → 13 Roadmap → 14 Checklist。*
