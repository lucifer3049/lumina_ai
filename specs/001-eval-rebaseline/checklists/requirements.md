# Specification Quality Checklist: 換 embedding 模型後的檢索品質重新定錨

**Purpose**: Validate specification completeness and quality before proceeding to planning
**Created**: 2026-09-05
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

**第二輪驗證（2026-09-05，人類 review 後）**：16 項仍全數通過。三項待決事項的處置：

1. **SC-006 提到 lint／smoke／CI 三個專案流程名稱**，嚴格說是實作細節。保留——「評測不得
   進自動化流程」是 2B-0 的既有定案，而它唯一可驗證的形式就是指名那三條流程；抽象成
   「不得進自動化流程」會讓這條需求驗不到東西。**未變更。**

2. **新增題目的來源**：人類裁決改為 **AI 起草 + 逐題人類改寫**，明示放寬 2B-0 的
   「不得由 LLM 從段落生成」。FR-002 改寫，並拆出 FR-002a（起草不得沿用正解原文字詞作為
   主要檢索線索——那正是 2B-0 拒絕生成的理由，把關點從「誰起草」移到「誰改寫」）與
   FR-002b（新舊題可區分、既有 24 題不得改寫，讓「結論是不是新題撐起來的」事後查得出來）。
   **本包因此不再卡在人類逐題撰寫上，但仍卡在人類逐題改寫。**

3. **回退是否收進本包**：人類裁決**收進本包**。新增 User Story 4 與 FR-020～FR-026
   （三檔判定、以手寫題組的 `hybrid+rerank` 為裁決依據、公開題組具否決權、回退只換
   embedding 供應商與模型、不得需要 schema 變更）、SC-008～SC-010，並改寫 Assumptions
   首項與 Out of Scope。

**本輪新增的一項自訂假設，請在 plan 階段留意**：判定門檻取「一題的權重」（手寫 ≤2pp、
公開 >0.83pp）。這是依題數推得的預設值，**不是統計顯著性檢定**——它擋的是「兩題翻盤被
當成結論」，不保證差距真的超出雜訊。已寫進 Assumptions。
