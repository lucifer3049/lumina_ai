# 09 REST API 設計

| 項目 | 內容 |
|------|------|
| 文件編號 | 09 |
| 版本 | v1.1 |
| 日期 | 2026-07-30 |
| 狀態 | Draft — 待審閱 |
| 相依文件 | 01（ADR-004）、03（codegen）、04（模組對映）、10（認證與權限細節） |
| 變更紀錄 | v1.1：新增附錄 A 錯誤 code 初始字典（15 審查報告 F-05） |

---

## 1. 設計理念與全域慣例

### 1.1 基本慣例

| 項目 | 規範 |
|------|------|
| Base path | `/api/v1`（URL path 版本化；v1 內只做向後相容變更，breaking change 才開 v2） |
| 資源命名 | 複數名詞、kebab-case path、snake_case JSON 欄位 |
| 方法語意 | GET 讀 / POST 建立與動作 / PATCH 部分更新 / DELETE 刪除（soft）；動作型端點用 `POST /{res}/{id}:action` 風格改為子資源（如 `/publish`） |
| 分頁 | cursor-based：`?cursor=&limit=`（預設 20、上限 100）；回應 `{items, next_cursor, total?}`（total 僅小集合提供） |
| 過濾/排序 | `?status=&q=&sort=-created_at`（白名單欄位） |
| 冪等 | 所有 POST 建立端點支援 `Idempotency-Key` header（Redis 記錄 24h，重複回原結果） |
| 並發控制 | 更新端點支援 `If-Match: {etag}`（樂觀鎖，version 欄位） |
| Rate limit 回應 | `429` + `Retry-After` + `X-RateLimit-Remaining` |
| Trace | 回應一律帶 `X-Request-ID`；client 可傳入沿用 |

### 1.2 認證方式

| 方式 | 用途 | 憑證位置 |
|------|------|----------|
| JWT Bearer | Web 前端（人） | `Authorization: Bearer <access>`；refresh 走 httpOnly cookie `POST /auth/refresh` |
| API Key | 系統整合（機器） | `X-API-Key: <key>`；scope 受限、無 refresh |

Tenant 解析：JWT/API Key 內含 tenant 綁定，**不接受 client 自報 tenant_id**（防橫向越權）；平台管理員跨租戶操作走 `/admin` 且必帶 audit。

### 1.3 統一錯誤格式（RFC 9457 Problem Details）

```json
{
  "type": "https://docs.example.com/errors/quota-exceeded",
  "title": "Quota exceeded",
  "status": 429,
  "code": "QUOTA_EXCEEDED",          // 機器可判斷的穩定碼
  "detail": "Monthly token quota reached (1,000,000).",
  "request_id": "req_01H...",
  "errors": [{"field": "kb_ids", "message": "..."}]   // 422 驗證錯誤時
}
```

狀態碼慣例：400 格式錯誤 / 401 未認證 / 403 無權限 / 404 不存在或無權可見（不區分，防資源枚舉）/ 409 衝突 / 413 過大 / 415 媒體型別不符 / 422 語意驗證失敗 / 423 帳號鎖定 / 429 限流或配額 / 5xx 伺服器錯（不洩內部細節）。完整 code 字典見附錄 A。

## 2. Endpoint 總覽

> 權限欄為必要 permission code；★ = 冪等鍵支援；所有端點皆隱含 tenant scope。

### 2.1 /auth

| Method | Path | 說明 | 權限 |
|--------|------|------|------|
| POST | /auth/login | 帳密登入 → TokenPair（失敗鎖定策略見 10） | 公開 |
| POST | /auth/refresh | refresh cookie → 新 TokenPair（rotation） | cookie |
| POST | /auth/logout | 撤銷 refresh + access jti | 登入者 |
| POST | /auth/password/change | 變更密碼（撤銷全部 session） | 登入者 |
| POST | /auth/password/forgot · /reset | 重設流程（email token） | 公開 |

### 2.2 /users · /tenants

| Method | Path | 說明 | 權限 |
|--------|------|------|------|
| GET | /users | 使用者列表（分頁/過濾） | user:read |
| POST ★ | /users | 邀請/建立使用者 | user:write |
| GET/PATCH | /users/{id} | 詳情 / 更新（角色指派含在 PATCH） | user:read / user:write |
| POST | /users/{id}/deactivate | 停用（撤銷 session） | user:write |
| GET/PATCH | /users/me | 個人資料與偏好 | 登入者 |
| GET/PATCH | /tenants/current | 本租戶資訊 / 設定 | tenant:read / tenant:admin |
| GET | /tenants/current/quota | 配額與用量即時狀態 | tenant:read |
| POST | /tenants/current/export | 租戶資料匯出（202 + job） | tenant:admin |
| GET/POST/PATCH/DELETE | /roles, /roles/{id} | 自訂角色 CRUD | role:admin |
| GET/POST/DELETE | /api-keys, /api-keys/{id} | API Key 管理（建立時一次性回明文） | apikey:admin |

### 2.3 /knowledge · /documents

| Method | Path | 說明 | 權限 |
|--------|------|------|------|
| GET/POST ★ | /knowledge-bases | KB 列表 / 建立 | knowledge:read / write |
| GET/PATCH/DELETE | /knowledge-bases/{id} | 詳情 / 設定（chunk、檢索參數）/ 刪除 | 同上（資源級 grant 疊加） |
| POST | /knowledge-bases/{id}/reindex | 重嵌入（202 + job） | knowledge:admin |
| GET | /knowledge-bases/{id}/documents | KB 內文件列表 | knowledge:read |
| POST ★ | /knowledge-bases/{id}/documents | 上傳（multipart；大檔走 §3.1 分塊流程） | knowledge:write |
| GET | /documents/{id} | 詳情含 ETL 狀態/進度/stats | knowledge:read |
| DELETE | /documents/{id} | 刪除（級聯排程） | knowledge:write |
| POST | /documents/{id}/reingest | 重新處理（202） | knowledge:write |
| GET | /documents/{id}/chunks | chunk 預覽（分頁） | knowledge:read |
| GET/POST/PATCH/DELETE | /sources, /sources/{id} | 同步來源（API/DB/Web）設定 | knowledge:admin |
| POST | /sources/{id}/sync | 手動觸發同步（202） | knowledge:write |

### 2.4 /chat · /conversations

| Method | Path | 說明 | 權限 |
|--------|------|------|------|
| GET/POST ★ | /conversations | 列表 / 建立（kb_ids、model、prompt_key） | chat:use |
| GET/PATCH/DELETE | /conversations/{id} | 詳情（含訊息分頁）/ 改名/釘選/封存 / 刪除 | 擁有者 |
| **POST** | **/conversations/{id}/messages** | **發送訊息 → SSE 串流回應**（`Accept: text/event-stream`）；非串流模式 `?stream=false` | chat:use |
| POST | /conversations/{id}/messages/{mid}/stop | 中止生成 | 擁有者 |
| POST | /conversations/{id}/messages/{mid}/regenerate | 重新生成 | 擁有者 |
| GET | /conversations/{id}/export | 匯出 markdown/json | 擁有者 |

### 2.5 /prompts · /models · /tools · /rag

| Method | Path | 說明 | 權限 |
|--------|------|------|------|
| GET/POST ★ | /prompts | 模板列表 / 建立 | prompt:read / write |
| GET/PATCH/DELETE | /prompts/{id} | 詳情 / 更新 meta / 刪除 | 同上 |
| GET/POST | /prompts/{id}/versions | 版本列表 / 建立 draft | prompt:write |
| POST | /prompts/{id}/versions/{ver}/publish | 發佈（不可變） | prompt:publish |
| POST | /prompts/{id}/versions/{ver}/activate | 切換 active（rollback 同此） | prompt:publish |
| POST | /prompts/{id}/test | 對測試輸入試跑指定版本 | prompt:write |
| GET | /models | 本租戶可用模型（含能力/價格） | model:read |
| PATCH | /models/{id} | 租戶級啟用/參數/fallback 設定 | model:admin |
| GET | /tools | 可用工具（含 schema） | tool:read |
| PATCH | /tools/{name} | 租戶啟用/政策覆寫 | tool:admin |
| GET | /tools/{name}/executions | 執行紀錄（分頁） | tool:read |
| POST | /rag/query | 獨立檢索 API（不生成，回 chunks+scores；供整合方與除錯） | rag:query |
| POST | /rag/evaluations | 觸發評測 run（202） | eval:run |
| GET | /rag/evaluations/{id} | 評測結果 | eval:read |

### 2.6 /analytics · /settings · /admin

| Method | Path | 說明 | 權限 |
|--------|------|------|------|
| GET | /analytics/usage | 用量彙總（range、group_by） | analytics:read |
| GET | /analytics/costs | 成本分解 | analytics:read |
| GET | /audit-logs | 稽核查詢（分頁/過濾） | audit:read |
| GET | /notifications · PATCH /notifications/{id}/read | 通知收件匣 | 登入者 |
| GET/PATCH | /settings | 租戶級設定（含 provider 憑證寫入，唯寫不回讀明文） | tenant:admin |
| GET | /settings/feature-flags | 本租戶 flag 狀態 | 登入者 |
| — | **/admin/**（平台管理面）| tenants CRUD、全域 model catalog、系統 flag、跨租戶用量、DLQ 重放 | platform_admin（獨立角色）|

## 3. 關鍵流程規格

### 3.1 大檔上傳（>32MB）

`POST /documents:init`（回 upload_id + presigned part URLs，MinIO 直傳）→ client 分塊 PUT → `POST /documents:complete`（校驗 → 建 document → 觸發 ETL）。小檔直接 multipart 單請求。前端 uploadService 已對應（03 §2）。

### 3.2 SSE 事件協定

```
POST /api/v1/conversations/{id}/messages
Accept: text/event-stream

← id: 1            event: meta       data: {"message_id","model","conversation_id"}
← id: 2..n         event: delta      data: {"text":"..."}
← id: k            event: tool_call  data: {"name","params_preview","status":"running|done|failed"}
← id: n+1          event: citations  data: [{"chunk_id","doc_id","doc_name","page","score"}]
← id: n+2          event: usage      data: {"prompt_tokens","completion_tokens","cost"}
← id: n+3          event: done       data: {"message_id","finish_reason"}
（錯誤時）         event: error      data: {"code","title","retryable":true}
（每 15s）         : heartbeat
```

- 斷線重連：`Last-Event-ID` header → 從 Redis resume buffer 續傳（TTL 5min，過期回 `409 RESUME_EXPIRED`，client 改抓最終 message）。
- 所有 SSE 錯誤皆為 event（HTTP 已 200），錯誤碼與 §1.3 共用 code 字典。

### 3.3 非同步任務慣例（202 模式）

所有觸發長任務的端點回 `202 + {"job_id"}`；統一 `GET /jobs/{id}` 查詢（status/progress/result/error）。前端 usePolling 對應。

## 4. OpenAPI 治理

- FastAPI 自動生成 + 手動補 `operation_id`（決定 codegen 函式名，命名穩定性視同 API 契約）。
- CI：schema diff 檢查（oasdiff）——breaking change 需明確標記與 review；前端 generated client 過期即 fail（03 §3.1）。
- 錯誤 code 字典維護於單一 enum，文件自動生成。

## 5. 優點 / 缺點 / 適用情境

**優點**：慣例統一（分頁/冪等/錯誤/202/ETag 全域一致），整合方學一次通用全部；SSE 協定含 resume 與結構化事件，前端狀態機簡單；404 not-found/no-permission 合併防枚舉。
**缺點**：cursor 分頁不支援跳頁（管理列表場景以過濾彌補）；`/admin` 與租戶 API 同進程部署，隔離靠權限而非網路層（K8s 階段可拆 ingress 隔離）。
**適用情境**：Web 前端與 B2B 整合共用同一套 API（API Key + scope 控管），無需維護兩套。

## 6. Architecture Review

1. **SOLID / Clean**：Router 薄層規範不變；schema 與 service DTO 邊界清楚。
2. **DRY**：分頁/錯誤/202/冪等皆為全域 middleware 或 dependency，單點實作。
3. **KISS**：未採 GraphQL / gRPC——REST + codegen 已滿足型別安全需求，且 B2B 整合 REST 接受度最高。
4. **YAGNI**：webhook 對外推播（如 ETL 完成回呼整合方）列入 Notification 未來項，未先建端點。
5. **可測試性**：契約測試（schemathesis 對 OpenAPI 跑 property-based）+ API test 覆蓋權限矩陣。
6. **Technical Debt**：`?stream=false` 的非串流模式與 SSE 共用一個端點，實作需小心分流——已標註於實作注意事項。
7. **更好方案**：無實質更優；若未來行動端出現，考慮 BFF 層而非改動本 API。

---

## 附錄 A：錯誤 Code 初始字典（F-05）

單一 enum 維護於 `core/exceptions.py`，對映 04 附錄 B 的例外階層；新增 code 視同 API 契約變更（需 review）。`retryable` 表示 client 可否原樣重試。

| Code | HTTP | retryable | 說明 |
|------|------|-----------|------|
| AUTH_INVALID_CREDENTIALS | 401 | ✗ | 帳密錯誤（不區分帳號不存在） |
| AUTH_TOKEN_EXPIRED | 401 | ✗ | access token 過期 → client 走 refresh |
| AUTH_TOKEN_REVOKED | 401 | ✗ | 已撤銷（jti denylist / token_version） |
| AUTH_ACCOUNT_LOCKED | 423 | ✗ | 登入失敗鎖定中（附解鎖時間） |
| PERMISSION_DENIED | 403 | ✗ | 功能類權限不足（資源類回 404） |
| RESOURCE_NOT_FOUND | 404 | ✗ | 不存在或無權可見（合併，防枚舉） |
| RESOURCE_CONFLICT | 409 | ✗ | 唯一性/狀態衝突（如重複 slug、狀態機非法轉移） |
| STALE_WRITE | 409 | ✗ | If-Match ETag 不符（樂觀鎖） |
| RESUME_EXPIRED | 409 | ✗ | SSE resume buffer 過期 → client 改抓最終 message |
| VALIDATION_FAILED | 422 | ✗ | 語意驗證失敗（errors[] 帶欄位明細） |
| UPLOAD_TOO_LARGE | 413 | ✗ | 超過大小上限 |
| UNSUPPORTED_MEDIA_TYPE | 415 | ✗ | MIME 白名單外（magic bytes 判定） |
| RATE_LIMITED | 429 | ✓ | 頻率限制（帶 Retry-After） |
| QUOTA_EXCEEDED | 429 | ✗ | 配額用盡（附 resource 與 reset 時間；重試無用） |
| TENANT_SUSPENDED | 403 | ✗ | 租戶停權 |
| DOCUMENT_NOT_READY | 409 | ✓ | 文件尚未 ready（ETL 進行中） |
| ETL_FAILED | — | — | 非同步錯誤：出現在 job/document.error 與通知，不作 HTTP 回應碼 |
| MODEL_NOT_ENABLED | 422 | ✗ | 指定模型未對本租戶啟用 |
| CONTEXT_LENGTH_EXCEEDED | 422 | ✗ | 輸入超出模型可用預算（前端先擋，此為兜底） |
| PROVIDER_UNAVAILABLE | 503 | ✓ | fallback 鏈耗盡（SSE 中以 error event 呈現） |
| STREAM_INTERRUPTED | — | ✓ | SSE error event 專用：生成中斷、partial 已保存 |
| TOOL_NOT_AVAILABLE | 422 | ✗ | 工具未啟用/無權限/circuit open |
| TOOL_EXECUTION_FAILED | — | ✓ | SSE tool_call event 內的失敗狀態 |
| IDEMPOTENT_REPLAY | 200 | — | 非錯誤：Idempotency-Key 重放，回原結果（header 標記 `Idempotent-Replay: true`） |
| INTERNAL_ERROR | 500 | ✓ | 未預期錯誤（不洩細節，附 request_id） |
| SERVICE_UNAVAILABLE | 503 | ✓ | 維護/過載（帶 Retry-After） |

---

*下一步：確認本文件後，進行 Stage 8（10_安全設計.md）。*
