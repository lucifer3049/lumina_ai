# Specification Quality Checklist: API Key 認證與管理（2C-3）

**Purpose**: Validate specification completeness and quality before proceeding to planning
**Created**: 2026-09-06
**Feature**: [spec.md](../spec.md)

## Content Quality

- [x] No implementation details (languages, frameworks, APIs)
- [x] Focused on user value and business needs
- [x] Written for non-technical stakeholders
- [x] All mandatory sections completed

## Requirement Completeness

- [x] No [NEEDS CLARIFICATION] markers remain
- [x] Requirements are testable and unambiguous
- [x] Success criteria are measurable
- [x] Success criteria are technology-agnostic (no implementation details)
- [x] All acceptance scenarios are defined
- [x] Edge cases are identified
- [x] Scope is clearly bounded
- [x] Dependencies and assumptions identified

## Feature Readiness

- [x] All functional requirements have clear acceptance criteria
- [x] User scenarios cover primary flows
- [x] Feature meets measurable outcomes defined in Success Criteria
- [x] No implementation details leak into specification

## Notes

**第二次驗證（2026-09-06）：16/16 通過。**

第一次驗證有兩項未過，成因是同一個——三個 [NEEDS CLARIFICATION] 待人類裁決。三者皆為需求層
決定，依憲章原則 VI 不由 AI 自行選定。

**裁決結果（2026-09-06，人類，三題皆選 A）**

| 題 | 裁決 | 落到 spec 的位置 |
|----|------|------------------|
| C1 端點範圍 | **全部端點，只由 scope 控管**，不維護「不准碰」清單 | FR-014／014a／014b、US2 情境 8–9 |
| C2 用量歸屬 | **指認到具體哪一把 key**，人與機器可分開統計 | FR-020a、US3 情境 4–5、SC-011 |
| C3 建立者連動 | **key 完全獨立**，建立者降權／停用／刪除皆不影響 | FR-024／024a／024b、US5 情境 4–5、SC-012 |

**三題都選了最不設限的一邊，因此補上了三條使它自洽的要求**，而不是只把選項填進空格：

- C1 的直接後果是 **key 可以發 key**。於是 FR-016 的「不得授予自己沒有的權限」必須同時適用
  於 key 形態的發放者（FR-014a），而 FR-014b 要求發放鏈可追溯——並且**明確寫下不做連鎖撤銷**
  （已進 Out of Scope）。SC-013 把「追得回來」訂為 100%，理由寫在該條上：它是端點全開之後
  唯一的補償措施，它不成立則該裁決的風險沒有著落。
- C2 的直接後果是既有用量資料**不會有這項識別**。Assumptions 明寫統計必須容得下「舊資料沒有」
  而不是把它當成「不是 key 花的」——兩者在圖表上長得一樣。
- C3 的直接後果是**離職者發出的 key 會活下去**。FR-024b 因此要求「目前有哪些 key 有效」在任
  何時候都答得出來，這是本裁決之下唯一的盤點手段。

三項後果集中寫在 Assumptions 的〈2026-09-06 三項裁決帶進來的前提〉，刻意不散落——它們日後
最可能被讀成疏忽。

**Content Quality 的一次修正**：初稿在背景、Key Entities 與 Dependencies 三處直接寫出了資料
欄位識別符，已改寫為行為描述（僅保留 `docs/plan` 的章節指引）。設計文件的章節引用刻意保留
——它們是需求的來源依據，不是實作細節。

**採用預設而未列為 clarification 的項目**（依規則上限 3 條，其餘走 Assumptions）：撤銷不可
復原、到期為選填、到期時間不得設在過去、權限範圍不得為空、配額仍以租戶為單位、管理權限只給
Owner 與 Admin。其中最後一項若 review 不同意，改動成本最低但影響面最大——它決定誰發得出 key。
