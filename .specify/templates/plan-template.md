# Implementation Plan: [FEATURE]

**Branch**: `[###-feature-name]` | **Date**: [DATE] | **Spec**: [link]

**Input**: Feature specification from `/specs/[###-feature-name]/spec.md`

**Note**: This template is filled in by the `/speckit-plan` command; its definition describes the execution workflow.

## Summary

[Extract from feature spec: primary requirement + technical approach from research]

## Technical Context

<!--
  ACTION REQUIRED: Replace the content in this section with the technical details
  for the project. The structure here is presented in advisory capacity to guide
  the iteration process.
-->

**Language/Version**: [e.g., Python 3.11, Swift 5.9, Rust 1.75 or NEEDS CLARIFICATION]

**Primary Dependencies**: [e.g., FastAPI, UIKit, LLVM or NEEDS CLARIFICATION]

**Storage**: [if applicable, e.g., PostgreSQL, CoreData, files or N/A]

**Testing**: [e.g., pytest, XCTest, cargo test or NEEDS CLARIFICATION]

**Target Platform**: [e.g., Linux server, iOS 15+, WASM or NEEDS CLARIFICATION]

**Project Type**: [e.g., library/cli/web-service/mobile-app/compiler/desktop-app or NEEDS CLARIFICATION]

**Performance Goals**: [domain-specific, e.g., 1000 req/s, 10k lines/sec, 60 fps or NEEDS CLARIFICATION]

**Constraints**: [domain-specific, e.g., <200ms p95, <100MB memory, offline-capable or NEEDS CLARIFICATION]

**Scale/Scope**: [domain-specific, e.g., 10k users, 1M LOC, 50 screens or NEEDS CLARIFICATION]

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

Read `.specify/memory/constitution.md` and record a verdict per principle. A violation is
not a note — it blocks the plan until it is either removed or justified in
**Complexity Tracking** below and approved by the human.

| Principle | Verdict | Notes |
|-----------|---------|-------|
| I. 單一入口與單向分層 | PASS / VIOLATION | New code respects `api/` → `services/` → `repositories/`·`ai/`·`rag/`·`etl/`·`tool/`; no ORM in async endpoints; Controller/Celery three-line rule; the 9 import-linter contracts in `backend/pyproject.toml` still hold |
| II. 租戶隔離 Fail Fast | PASS / VIOLATION | New repositories extend `TenantScopedRepository`; Redis keys carry `t:{tenant_id}:`; no client-supplied tenant_id |
| III. AI 呼叫收斂於 Gateway | PASS / VIOLATION | LLM access only via `ai/gateway/`; prompts only via PromptBuilder |
| IV. 驗收測試先行與四層測試 | PASS / VIOLATION | Acceptance tests precede implementation; the four layers are covered; DoD traces to the spec's Acceptance Criteria |
| V. 契約與結構變更受控 | PASS / VIOLATION | Django migration only, three-step for live changes; `make openapi && make gen-api` both run; permission code + `operation_id` + audit event kept in sync |
| VI. 規格先行與分層授權 | PASS / VIOLATION | This plan changes no requirement semantics from `spec.md`; every technical choice traces to an approved requirement |

**Does this plan restate or alter any requirement in `spec.md`?** It must not. If a
requirement turns out to be infeasible or ambiguous, STOP and report it — do not resolve
it here (Constitution Principle VI).

## Project Structure

### Documentation (this feature)

```text
specs/[###-feature]/
├── plan.md              # This file (/speckit-plan command output)
├── research.md          # Phase 0 output (/speckit-plan command)
├── data-model.md        # Phase 1 output (/speckit-plan command)
├── quickstart.md        # Phase 1 output (/speckit-plan command)
├── contracts/           # Phase 1 output (/speckit-plan command)
└── tasks.md             # Phase 2 output (/speckit-tasks command - NOT created by /speckit-plan)
```

### Source Code (repository root)
<!--
  ACTION REQUIRED: Replace the placeholder tree below with the concrete layout
  for this feature. Delete unused options and expand the chosen structure with
  real paths. This repository has ONE fixed layout, reproduced below — do NOT propose
  an alternative structure. List the files this feature touches under the existing
  directories; a new top-level directory needs a Constitution amendment, not a plan.
-->

```text
backend/
├── api/            # FastAPI routers — the ONLY HTTP entry point. Three-line endpoints.
│                   # MUST NOT import repositories/ or apps/
├── services/       # Business logic. MUST NOT import apps.*.models
├── repositories/   # Django ORM access, wrapped via sync_to_async.
│                   # All tenant-scoped repositories extend TenantScopedRepository
├── ai/             # AI Gateway (ai/gateway/), providers, PromptBuilder
├── rag/            # Retrieval, hybrid search, rerank, citation
├── etl/            # Loaders, chunkers, sync sources
├── tool/           # Tool calling
├── core/           # Shared infrastructure: TenantContext, exceptions, db/redis/storage
├── common/         # MUST NOT import any other layer
├── apps/<app>/     # Django models.py (thin: fields, Meta, __str__) + migrations/
└── tests/
    ├── unit/           # Service / pure logic
    ├── integration/    # Repository + RLS
    ├── api/            # Permission matrix + error format
    └── e2e/            # Smoke: login → upload → ready → answer → citation

frontend/
└── src/
    ├── api/generated/  # OpenAPI codegen output — MUST NOT be hand-edited
    ├── services/       # Views call these; views MUST NOT fetch directly
    ├── stores/         # Pinia
    └── views/
```

**Structure Decision**: [List the concrete files this feature adds or changes, grouped by
the directories above. Flag any change that crosses a layer boundary — it must still
satisfy the 9 import-linter contracts in `backend/pyproject.toml`.]

## Complexity Tracking

> **Fill ONLY if Constitution Check has violations that must be justified**

| Violation | Why Needed | Simpler Alternative Rejected Because |
|-----------|------------|-------------------------------------|
| [e.g., 4th project] | [current need] | [why 3 projects insufficient] |
| [e.g., Repository pattern] | [specific problem] | [why direct DB access insufficient] |
