<!--
Sync Impact Report
==================
Version change: 1.1.0 → 2.0.0
Bump rationale: MAJOR——**重新定義原則 II**。多租戶隔離（per-row tenant_id + RLS +
                TenantContext）自本版起作廢，改為單一資料域 + 部署邊界；這是向後不相容
                的治理變更，且使 ADR-002 與 Phase 1–2 的多租戶設計失效。

Modified principles:
  - II. 租戶隔離 Fail Fast → **II. 單一資料域與部署邊界**。決定依據：本系統實際只有
    一位使用者，對外交付走 container-per-customer，隔離由部署提供而非資料列提供
    （2026-09-06 人類裁決）。新原則明寫它賣掉了什麼（DB 層不再有防線）與它的效力條件
    （一份部署只服務一個擁有者），並禁止個別 Feature 自行加回擁有者欄位——半套隔離
    比沒有隔離更危險。
  - IV. 測試層定義：integration 由「Repository + RLS」改為「Repository + 真實 DB 行為」
    （RLS 移除後該層仍在，守的對象換成約束、索引與 migration）。

Removed sections: 無（原則 II 為改寫而非移除）

待處理的下游同步（**本次僅修訂憲章，程式碼與文件尚未跟上；這是刻意的順序**）:
  - CLAUDE.md 架構鐵則 4（租戶隔離）必須改寫——Governance 明訂兩者衝突時同步修正
  - docs/plan：01（ADR-002）、05（RLS 與 22 張表）、09（登入帶 tenant_slug）、
    10（§4 租戶隔離）、11、12、13 全部需要同步
  - specs/003-api-key/spec.md：整份建立在「租戶管理者發 key」之上，需重寫
  - 程式碼：230 檔／3,819 處 tenant 參照、22 張表的欄位、8 個含 tenant 的唯一鍵、
    20+ 支 RLS migration、2 張以 tenant 為主鍵的表（identity_tenant_directory、
    platform_tenantdatakey）
  - 拆除本身走完整 SDD，且必須切成多個工作包分批進行（原則 VI 與〈開發工作流〉）

歷史：v1.1.0（2026-09-05）新增原則 VI（規格先行與分層授權）。
      v1.0.0（2026-09-05）首次批准，原檔是未替換的 constitution-template 佔位樣板。

---
（v1.1.0 當時的 Sync Impact Report 保留於下，供追溯）

Version change: 1.0.0 → 1.1.0
Bump rationale: MINOR——新增原則 VI（規格先行與分層授權），並實質擴充〈開發工作流與
                品質閘門〉與 Governance。未移除、未重新定義任何既有原則，無向後不相容
                變更。原則 IV 的驗收語意錨定屬 PATCH 級釐清，併入本次 MINOR。

Modified principles:
  - IV. 驗收測試先行與四層測試 — 補上「DoD 必須可回溯至 Specification 的
    Acceptance Criteria」，建立單一驗收語意鏈，避免 spec 與 DoD 各自維護一套標準

Added sections:
  - VI. 規格先行與分層授權（NON-NEGOTIABLE）
  - 開發工作流與品質閘門 §SDD 與工作包制度的整合（六層流程 + 三條權責邊界）
  - Governance §跨層裁決權；Governance 對 Spec Kit 樣板的優先聲明

Removed sections: 無

Follow-up TODOs:
  - docs/plan/13 的工作包表格尚未改為引用 spec 路徑。依〈開發工作流〉第 2 點，既有
    工作包（Phase 0–2、W 系列）不回溯改寫，新工作包開工時才套用。

歷史：v1.0.0（2026-09-05）為首次批准，原檔是未替換的 constitution-template 佔位樣板。

來源依據：CLAUDE.md（架構鐵則、Git 規則、AI 任務卡）、README.md、
docs/plan/01（ADR-001～007）、docs/plan/13 §1.2（人機協作開發規則）、
.specify/ 的 spec／plan／tasks 樣板與 speckit workflow。
-->

# Lumina AI Constitution

## Core Principles

### I. 單一入口與單向分層（NON-NEGOTIABLE）

FastAPI 必須是唯一對外 HTTP 入口；Django 僅提供 ORM、Migration 與 Admin，禁止對外
暴露業務 API。層與層之間的 import 方向必須單向：`api/` → `services/` →
`repositories/` / `ai/` / `rag/` / `etl/` / `tool/`，`core/` 為各層共用基礎設施，
`common/` 禁止 import 任何其他層。`api/` 禁止 import `repositories/` 與 `apps/`；
`services/` 禁止 import `apps.*.models`；`ai/ rag/ etl/ tool/` 禁止 import `api/`
與 `services/`。Django ORM 為 sync，必須封裝在 `repositories/` 內經 `sync_to_async`
呼叫，禁止在 async endpoint 直接呼叫 ORM。Endpoint 遵守三行原則：解析請求 →
呼叫一個 Service 方法 → 回傳；業務邏輯、RAG 與 Tool 邏輯禁止寫在 endpoint。
Celery task 同樣遵守三行原則（取 context → 呼叫 service → 回報）且必須冪等，
冪等鍵採 `(doc_id, doc_version, stage)` 模式。Model 保持薄：`apps/*/models.py`
只有欄位、Meta 與 `__str__`。

本專案刻意不引入 `core/interfaces/` 抽象層——service 直接依賴具體 repository 類別
（建構子注入 + 預設值）。這是單一部署單元下的取捨，不得以「解耦」為由自行加回。

依賴方向由 import-linter 的 9 條 contract（定義於 `backend/pyproject.toml`）機器強制，
違反即 CI 紅燈，不接受人工豁免。

理由：架構風格為 Modular Monolith + Clean Architecture，唯有機器可驗證的邊界能在
單人 + AI 的高速迭代下保住未來拆分 microservices 的能力（ADR-001、ADR-006）。

### II. 單一資料域與部署邊界（NON-NEGOTIABLE）

本系統是**單一擁有者、單一資料域**的部署。一個部署單元內的全部資料屬於同一個擁有者；
交付給第三方的方式是**交付一份獨立部署**（container 映像 + 其自有的資料卷），兩份部署
之間不共用任何儲存體、快取或物件前綴。

因此：

- 禁止在任何資料表引入 `tenant_id` 或任何等價的擁有者欄位。
- 禁止建立以「日後可能要支援多租戶」為由的隔離抽象——TenantContext、
  `TenantScopedRepository`、Redis 的擁有者 key 前綴、per-owner 的加密層皆屬此列。
- Repository 禁止帶隱含的擁有者過濾：查詢條件必須全部來自呼叫端明示的參數，
  「基底自動幫你加一個 WHERE」不再是這個系統的行為。

**必須明白寫下這個決定買到什麼、賣掉什麼。** 隔離邊界移到部署層之後，資料庫層不再有
任何防線：RLS 過去會在應用層漏過濾時擋下錯誤，移除之後那一類 bug 直接讀到資料而不會
有任何症狀。這是本原則**自願接受**的代價，不是疏忽——單一資料域下「不該讀的資料」不
存在，代價只會在「同一份部署被兩個擁有者共用」的那一天兌現。

**因此本原則有一個效力條件：一份部署只服務一個擁有者。** 這個前提一旦改變，必須先修訂
本原則（MAJOR 級），**禁止由個別 Feature 自行加回擁有者欄位或隔離層**——那會造出一個
「有一半隔離」的系統，而半套隔離比沒有隔離更危險：它看起來像有防線。

與擁有者數量無關、因此**繼續成立**的兩條：

- 身分必須由伺服器端從已驗證的憑證推導，**禁止採信呼叫端自報的身分、權限或範圍**。
- 憑證與 secrets 沿用 envelope 加密（KEK → DEK → 密文），DEK 改為**每份部署一把**。
  envelope 的價值在於輪替 KEK 時只需重包 DEK 而不必重新加密每一列密文，這個價值與
  擁有者數量無關。

理由：本系統的實際使用者只有一位，而對外交付走 container-per-customer——隔離由部署提供，
不由資料列提供。在此前提下，per-row 的租戶機制不再是防線，只是每個 Feature 都要繳的稅
（2026-09-06 人類裁決；此前的 ADR-002 與 Phase 1–2 的多租戶設計自本版起作廢，其歷史紀錄
保留於 `docs/plan/13`）。

### III. AI 呼叫收斂於 Gateway

所有 LLM 呼叫必須經 AI Gateway（`ai/gateway/`）；任何位置禁止直接 import provider SDK。
所有 Prompt 必須經 PromptBuilder 使用版本化模板，禁止散落於 Python string。
模型名稱、API key、endpoint URL 必須來自設定，禁止 hardcode。

理由：供應商可替換性、成本與用量歸因、Prompt 版本管理與 Evaluation 全部依賴單一
收斂點；一旦有繞道的呼叫，計費、稽核與回歸評測同時失去可信度（docs/plan/06）。

### IV. 驗收測試先行與四層測試（NON-NEGOTIABLE）

每個工作包開工時，必須先依該工作包的 DoD 產出驗收測試，經人類 review 確認「測試驗對
了東西」之後才開始實作，實作進行至測試通過為止。

走完整 SDD 的工作包，其 DoD 必須可回溯至 Specification 的 Acceptance Criteria：
**Acceptance Criteria 是驗收語意的唯一上游，DoD 是它在該工作包上的收斂**。兩者衝突時
以 Specification 為準，停止並回報，禁止各自維護一套標準。走輕量路徑（見原則 VI）時沒有
Specification，該工作包的 DoD 即為驗收語意的最上游。

測試分四層並必須分層維護：unit
（Service／純邏輯）、integration（Repository + 真實 DB 行為：約束、索引、migration）、
api（權限矩陣 + 錯誤格式）、e2e。
LLM 測試一律使用 MockProvider，禁止呼叫真實 API。每個 Model 必須有對應的 factory_boy
factory。

每次任務結束必須跑 E2E smoke suite（`make smoke`：登入→上傳→ready→問答→引用）；
smoke 不過視同任務未完成。開發過程中可用窄目標（`make test-changed` / `test-lf` /
`test-k`）加速迴圈，但它們是啟發式、不是安全網——結束前的全套（分三層跑：
`make test-unit && make test-integration && make test-api`）與 `make smoke` 那一次
不得省略。測試不過或 DoD 未達，禁止標記工作包完成。

理由：人的 review 頻寬是本專案唯一的稀缺資源，review 測試的成本遠低於 review 實作；
而跨 session 的 AI 開發存在迴歸盲區，smoke 是唯一能穩定抓到它的網（docs/plan/13 §1.2）。

### V. 契約與結構變更受控

Schema 變更只用 Django Migration，禁止 Alembic。上線變更走三步走：加欄位帶 default →
backfill → 加約束；大表索引必須使用 `AddIndexConcurrently`。

後端 schema 變更後必須完整跑 `make openapi && make gen-api` 兩段——`gen:api` 讀的是
repo 根目錄的 `openapi.json`，單跑它只會用舊契約重產一次而看到假的 no diff。
`frontend/src/api/generated/` 由 codegen 產生，禁止手改；前端 views 禁止直接 fetch，
一律經 services/stores；LLM 輸出禁止用 `v-html` 直接渲染。

新增或修改資源端點時必須同步更新：權限 code、OpenAPI `operation_id`（其命名穩定性
視同 API 契約）、以及審計事件（若屬敏感操作）。

理由：契約漂移的代價落在 CI 與前端，而非改動當下；把同步動作綁進「改端點」這個動作
本身，是唯一能在單人開發下維持契約可信的做法（docs/plan/09）。

### VI. 規格先行與分層授權（NON-NEGOTIABLE）

開發生命週期固定為六層，每層只回答一個問題，且必須向其所有上層負責：

| 層 | 回答的問題 | 產物 |
|----|-----------|------|
| Constitution | 這個專案永遠不能違反什麼？ | 本文件（適用於所有 Feature） |
| Specification | 這個 Feature 到底要做什麼？ | `specs/<###-feature>/spec.md` |
| Plan | 這個 Feature 要怎麼實現？ | `specs/<###-feature>/plan.md`（含 research／data-model／contracts） |
| Tasks | 我要一步一步做什麼？ | `specs/<###-feature>/tasks.md`，對齊 `docs/plan/13` 的工作包 |
| Implementation | 程式碼實際長什麼樣？ | 程式碼與測試 |
| Verification | 它真的做到了嗎？ | lint／三層測試／smoke／openapi-check／CI 結果 |

**各層的責任邊界**：

- **Constitution** 定義專案級不可違反的架構、品質、安全與治理規則，適用於所有
  Feature。它不描述任何單一 Feature 的需求。
- **Specification** 必須描述使用者需求、系統行為、功能範圍、非功能需求、錯誤情境、
  邊界條件與 Acceptance Criteria。**禁止**決定程式架構、目錄配置、類別切分或任何
  實作細節。
- **Plan** 必須在 Constitution 與 Specification 的約束下，定義技術方案、架構修改、
  資料模型、API、受影響元件（service／repository／ai／rag／etl／tool／前端）、
  migration、測試策略、相容性與部署風險。**禁止**改變 Specification 的需求語意。
- **Tasks** 必須把已核准的 Specification 與 Plan 拆解為可執行步驟。**禁止**新增
  Specification 未定義的需求、自行改變 Plan、擴張工作範圍，或進行未授權的順手改善。

**優先關係**：`Constitution > Specification > Plan > Tasks > Implementation`。

此處的 `>` 是**治理與約束的優先級，不是取代關係**——上層不會、也不得替下層回答它的
問題。下層必須同時符合其所有上層；上下衝突時以上層為準，並且必須**停止並回報**，
不得由 AI 自行修改任一方使其一致。

**AI 禁止跨層自行決策**（違反下列任一條視同任務失敗）：

1. 禁止跳過 Specification 直接進入 Implementation。
2. 未經人類 review 的 Specification 禁止進入 Plan。
3. 未經人類 review 的 Plan 禁止進入 Tasks 或 Implementation。
4. Tasks 禁止產生 Specification 未定義的新需求。
5. Plan 禁止以技術考量自行改變需求語意。
6. Specification、Plan、Tasks 與既有程式碼之間發生衝突時，必須停止並回報，禁止自行
   選擇一方。
7. 發現範圍外的問題時只回報、不修改（同〈開發工作流與品質閘門〉的禁區規則）。

原則 I–V 的架構、資料域、Gateway、測試與契約規則，以及 Git／Review／CI 規則，在 SDD 的
每一層都持續有效，不因走了規格流程而放寬。

**輕量路徑**：不是所有變更都需要完整的 Specification。

- 走**完整 SDD**：新增或改變使用者可見行為、新增或修改 API 端點、schema 變更、
  新增外部依賴、跨模組改動。
- 走**輕量路徑**（只需 AI 任務卡 + DoD 驗收測試，不產 `spec.md`）：不改變既定行為的
  bug 修正、純重構、文件、測試補強、設定調整。

走哪一條路徑**由人類裁決**；AI 禁止自行判定某項變更適用輕量路徑。

理由：本專案的實作速度來自 AI，正確性來自人類的 review；讓 review 有效的唯一方式是
**在便宜的地方 review**——需求文件遠比實作便宜，方案文件遠比除錯便宜。分層授權不是
儀式，它是把人類的判斷插進成本最低的幾個點。輕量路徑同時存在，是因為本文件自己要求
「複雜度必須被證成」：對不改變行為的變更強制產出規格，只會製造沒有讀者的文件。

## 技術與交付約束

技術棧為專案級決策，變更必須走本文件的修訂程序：Python 3.12 + uv／Django 5.2 LTS
（僅 ORM + Migration + Admin）／FastAPI + Uvicorn（唯一對外入口，REST / OpenAPI / SSE）
／Celery + Redis／PostgreSQL 16 + pgvector(halfvec) + pgroonga／PgBouncer（transaction
mode）／MinIO／Vue 3 + TypeScript(strict) + Pinia + pnpm／Docker Compose。

程式碼品質門檻（`make lint` 必須全綠）：ruff（lint + format）、mypy strict、
import-linter。型別註記必寫。例外必須使用 `core/exceptions.py` 的業務例外階層，
禁止裸 `except Exception: pass`。

所有對外呼叫（DB／Redis／HTTP／LLM／tool）必須設定 timeout；retry 僅限冪等操作。

設定與 secrets：一律經 Pydantic Settings 讀環境變數；禁止 hardcode API key、model name
或 URL；secrets 禁止進入 log、錯誤訊息與前端。

docs/plan 內的參數值（top_k、timeout、TTL 等）是起始點而非定論；調整必須在 PR 說明中
標注並引用依據（評測或壓測數據）。

## 開發工作流與品質閘門

**工作包節奏**：依 `docs/plan/13` 的 Phase 與工作包順序開發；一次任務對齊一個工作包
（或其子項），完成即停，等人類 review。禁止 AI 連續自主推進多個工作包。

**SDD 與工作包制度的整合**——只有一套流程，不存在第二套：

```text
需求
  ↓
Specification（specs/<###-feature>/spec.md）
  ↓ ← 閘門 1：Human Review
Plan（specs/<###-feature>/plan.md）
  ↓ ← 閘門 2：Human Review
Tasks／Work Package（tasks.md，對齊 docs/plan/13 的工作包編號）
  ↓
Acceptance Tests（依 DoD 產出，刻意留紅）
  ↓ ← 閘門 3：Human Review（焦點是「測試是否驗對了東西」）
Implementation
  ↓
Verification（make lint／分三層全套／make smoke／make openapi-check）
  ↓ ← 閘門 4：Human Review
Git commit（人類執行）→ push → make ci-status 盯到終局
```

三條權責邊界必須明確：

1. **`docs/plan/13` 保留**，仍是 Phase、工作包排序、DoD、結案紀錄與遺留缺口的
   **單一事實來源**；SDD 不取代它，也不另立第二份進度紀錄。
2. **需求細節寫在 `specs/<###-feature>/spec.md`，不寫在 13**；13 的工作包表格改為
   引用 spec 路徑，避免同一份需求存在兩處而分岔。既有工作包（Phase 0–2、W 系列）的
   記載維持原樣，不回溯改寫。
3. **`/speckit-implement` 的單次執行範圍必須限制在單一工作包對應的 task 區段**，
   跑完即停等人類 review；禁止一次跑完 `tasks.md` 的全部 Phase。

**任務卡邊界**：任務以固定格式下達（任務／先讀／Spec／DoD 測試／禁區）；欄位缺漏時
必須先要求補齊再動工。走輕量路徑時 Spec 欄填「輕量路徑（人類裁決）」。**禁區絕對優先**：即使發現禁區內有 bug 或可優化處，只回報、不修改。
範圍外的「順手改善」一律禁止，發現的問題記入回報清單由人類決定。任務結束時必須回報：
變更檔案清單、測試結果、smoke 結果、發現但未處理的問題。

**設計文件優先**：設計文件與實作衝突時必須停下並回報差異，由人類決定改文件或改實作；
禁止擅自偏離文件。文件按任務類型讀取（索引見 CLAUDE.md），禁止憑記憶推測設計。

**Git 安全規則**：完成程式修改後，AI 禁止自行執行任何 Git 操作（`add`／`commit`／
`push`／`tag`／`merge`／`rebase`／`checkout -b`）。必須先輸出 Changed Files、Summary、Impact Analysis
（Migration／Backward Compatibility／Deployment Risk）與 Commit Message 建議，待人類
確認後由人類完成 commit 與 push。

**Commit 格式**：Conventional Commits（`type(scope): subject`，subject ≤ 72 字元、
句末不加句號，body 說明「為什麼」且與 subject 空一行）。一律走 `.gitmessage` 模板或
`git commit -F <檔案>`；禁止用單一 `-m` 貼多行。

**CI 是終局**：push 之後必跑 `make ci-status` 盯到 run 完成。「本地全綠」不能替代這一步。

## Governance

本文件優先於其他一切開發實務。與本文件衝突的既有慣例、工具預設或 AI 既有訓練直覺，
一律以本文件為準；`CLAUDE.md` 是本文件在日常開發中的執行摘要，兩者衝突時以本文件為準，
並必須同步修正 `CLAUDE.md`。Spec Kit 的樣板、workflow 與 skill 若與本文件衝突，同樣以
本文件為準，並必須同步修正該樣板——樣板是機器會實際讀取並照做的文字，留著不改等同於
本文件被靜默推翻。

**跨層裁決權**：Constitution、Specification、Plan、Tasks 與既有程式碼之間的任何衝突，
裁決權屬於人類。AI 的義務止於停止並回報差異——禁止自行修改任一方使其一致，也禁止以
「較上層優先」為由逕自捨棄下層文件的內容。

**修訂程序**：任何修訂必須（1）在 PR 中說明變更動機與影響範圍；（2）標注版本號變更
與其等級依據；（3）若影響既有程式碼或流程，附遷移計畫（要改什麼、由誰、何時）；
（4）經人類明確核准後合併。AI 禁止自行修訂本文件；只能在人類要求下起草。

**版本政策**（語意化版本）：
- MAJOR：移除原則、重新定義原則，或做出向後不相容的治理變更。
- MINOR：新增原則或章節，或實質擴充既有指引。
- PATCH：釐清用語、修正錯字、不改變語意的調整。

**合規審查**：每個工作包結案時，人類 review 必須確認該工作包未違反本文件；
`make lint`（含 import-linter 的 9 條 contract）與分三層的全套測試 ＋ `make smoke`
是自動化的合規檢查點。任何為了趕進度而偏離本文件的決定，必須在 `docs/plan/13`
留下明確紀錄（範圍偏離紀錄或未結項），不得以口頭默認方式帶過。複雜度必須被證成——
無法說明必要性的抽象層與間接層，預設不加入。

**Version**: 2.0.0 | **Ratified**: 2026-09-05 | **Last Amended**: 2026-09-06
