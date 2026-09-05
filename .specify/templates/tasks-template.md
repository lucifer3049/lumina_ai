---

description: "Task list template for feature implementation"
---

# Tasks: [FEATURE NAME]

**Input**: Design documents from `/specs/[###-feature-name]/`

**Prerequisites**: plan.md (required), spec.md (required for user stories), research.md, data-model.md, contracts/

**Tests**: MANDATORY. Constitution Principle IV (驗收測試先行與四層測試, NON-NEGOTIABLE) requires acceptance tests to be written FIRST, reviewed by a human, and failing before any implementation begins. Every user story MUST have test tasks, and they MUST precede its implementation tasks. Tests are never optional in this project, regardless of what the feature specification says.

**Test layers** (Constitution Principle IV): `unit` (Service / pure logic), `integration` (Repository + RLS), `api` (permission matrix + error format), `e2e`. LLM behaviour MUST be tested through MockProvider — never against a real API. Every tenant fixture MUST be dual-tenant.

**Organization**: Tasks are grouped by user story to enable independent implementation and testing of each story.

**Work-package scope**: Constitution 開發工作流 §SDD 與工作包制度的整合 limits a single `/speckit-implement` run to ONE work package's task section. Map each user-story phase below to a work package in `docs/plan/13`, implement one, then STOP for human review. Never run the whole file in one pass.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- **[Story]**: Which user story this task belongs to (e.g., US1, US2, US3)
- Include exact file paths in descriptions

## Path Conventions

This repository has ONE layout (Constitution Principle I). Use these real paths; do not
invent `src/models/` style trees.

- **Backend**: `backend/api/` → `backend/services/` → `backend/repositories/` / `backend/ai/` / `backend/rag/` / `backend/etl/` / `backend/tool/`; `backend/core/` is shared infrastructure; Django models live in `backend/apps/<app>/models.py`; migrations in `backend/apps/<app>/migrations/`
- **Backend tests**: `backend/tests/unit/`, `backend/tests/integration/`, `backend/tests/api/`, `backend/tests/e2e/`
- **Frontend**: `frontend/src/` (views / stores / services); `frontend/src/api/generated/` is codegen output and MUST NOT be hand-edited (Constitution Principle V)

<!--
  ============================================================================
  IMPORTANT: The tasks below are SAMPLE TASKS for illustration purposes only.

  The /speckit-tasks command MUST replace these with actual tasks based on:
  - User stories from spec.md (with their priorities P1, P2, P3...)
  - Feature requirements from plan.md
  - Entities from data-model.md
  - Endpoints from contracts/

  Tasks MUST be organized by user story so each story can be:
  - Implemented independently
  - Tested independently
  - Delivered as an MVP increment

  DO NOT keep these sample tasks in the generated tasks.md file.
  ============================================================================
-->

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Project initialization and basic structure

- [ ] T001 Create project structure per implementation plan
- [ ] T002 Initialize [language] project with [framework] dependencies
- [ ] T003 [P] Configure linting and formatting tools

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Core infrastructure that MUST be complete before ANY user story can be implemented

**⚠️ CRITICAL**: No user story work can begin until this phase is complete

Examples of foundational tasks (adjust based on your project):

- [ ] T004 Setup database schema and migrations framework
- [ ] T005 [P] Implement authentication/authorization framework
- [ ] T006 [P] Setup API routing and middleware structure
- [ ] T007 Create base models/entities that all stories depend on
- [ ] T008 Configure error handling and logging infrastructure
- [ ] T009 Setup environment configuration management

**Checkpoint**: Foundation ready - user story implementation can now begin in parallel

---

## Phase 3: User Story 1 - [Title] (Priority: P1) 🎯 MVP

**Goal**: [Brief description of what this story delivers]

**Independent Test**: [How to verify this story works on its own]

### Tests for User Story 1 (MANDATORY - write first, must FAIL before implementation) ⚠️

> **NOTE: Write these tests FIRST, ensure they FAIL before implementation**

- [ ] T010 [P] [US1] API test (permission matrix + error format) for [endpoint] in backend/tests/api/test_[name].py
- [ ] T011 [P] [US1] Integration test for [user journey] in backend/tests/integration/test_[name].py

### Implementation for User Story 1

- [ ] T012 [P] [US1] Create [Entity1] model in backend/apps/[app]/models.py
- [ ] T013 [P] [US1] Create [Entity2] model in backend/apps/[app]/models.py
- [ ] T014 [US1] Implement [Service] in backend/services/[service].py (depends on T012, T013)
- [ ] T015 [US1] Implement [endpoint/feature] in backend/api/routers/[file].py
- [ ] T016 [US1] Add validation and error handling
- [ ] T017 [US1] Add logging for user story 1 operations

**Checkpoint**: At this point, User Story 1 should be fully functional and testable independently

---

## Phase 4: User Story 2 - [Title] (Priority: P2)

**Goal**: [Brief description of what this story delivers]

**Independent Test**: [How to verify this story works on its own]

### Tests for User Story 2 (MANDATORY - write first, must FAIL before implementation) ⚠️

- [ ] T018 [P] [US2] API test (permission matrix + error format) for [endpoint] in backend/tests/api/test_[name].py
- [ ] T019 [P] [US2] Integration test for [user journey] in backend/tests/integration/test_[name].py

### Implementation for User Story 2

- [ ] T020 [P] [US2] Create [Entity] model in backend/apps/[app]/models.py
- [ ] T021 [US2] Implement [Service] in backend/services/[service].py
- [ ] T022 [US2] Implement [endpoint/feature] in backend/api/routers/[file].py
- [ ] T023 [US2] Integrate with User Story 1 components (if needed)

**Checkpoint**: At this point, User Stories 1 AND 2 should both work independently

---

## Phase 5: User Story 3 - [Title] (Priority: P3)

**Goal**: [Brief description of what this story delivers]

**Independent Test**: [How to verify this story works on its own]

### Tests for User Story 3 (MANDATORY - write first, must FAIL before implementation) ⚠️

- [ ] T024 [P] [US3] API test (permission matrix + error format) for [endpoint] in backend/tests/api/test_[name].py
- [ ] T025 [P] [US3] Integration test for [user journey] in backend/tests/integration/test_[name].py

### Implementation for User Story 3

- [ ] T026 [P] [US3] Create [Entity] model in backend/apps/[app]/models.py
- [ ] T027 [US3] Implement [Service] in backend/services/[service].py
- [ ] T028 [US3] Implement [endpoint/feature] in backend/api/routers/[file].py

**Checkpoint**: All user stories should now be independently functional

---

[Add more user story phases as needed, following the same pattern]

---

## Phase N: Polish & Cross-Cutting Concerns

**Purpose**: Improvements that affect multiple user stories

- [ ] TXXX [P] Documentation updates in docs/
- [ ] TXXX Code cleanup and refactoring
- [ ] TXXX Performance optimization across all stories
- [ ] TXXX [P] Additional unit tests in backend/tests/unit/
- [ ] TXXX Security hardening
- [ ] TXXX Run quickstart.md validation

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: No dependencies - can start immediately
- **Foundational (Phase 2)**: Depends on Setup completion - BLOCKS all user stories
- **User Stories (Phase 3+)**: All depend on Foundational phase completion
  - User stories can then proceed in parallel (if staffed)
  - Or sequentially in priority order (P1 → P2 → P3)
- **Polish (Final Phase)**: Depends on all desired user stories being complete

### User Story Dependencies

- **User Story 1 (P1)**: Can start after Foundational (Phase 2) - No dependencies on other stories
- **User Story 2 (P2)**: Can start after Foundational (Phase 2) - May integrate with US1 but should be independently testable
- **User Story 3 (P3)**: Can start after Foundational (Phase 2) - May integrate with US1/US2 but should be independently testable

### Within Each User Story

- Tests MUST be written and FAIL before implementation (Constitution Principle IV)
- Models before services
- Services before endpoints
- Core implementation before integration
- Story complete before moving to next priority

### Parallel Opportunities

- All Setup tasks marked [P] can run in parallel
- All Foundational tasks marked [P] can run in parallel (within Phase 2)
- Once Foundational phase completes, all user stories can start in parallel (if team capacity allows)
- All tests for a user story marked [P] can run in parallel
- Models within a story marked [P] can run in parallel
- Different user stories can be worked on in parallel by different team members

---

## Parallel Example: User Story 1

```bash
# Launch all tests for User Story 1 together:
Task: "API test (permission matrix + error format) for [endpoint] in backend/tests/api/test_[name].py"
Task: "Integration test for [user journey] in backend/tests/integration/test_[name].py"

# Launch all models for User Story 1 together:
Task: "Create [Entity1] model in backend/apps/[app]/models.py"
Task: "Create [Entity2] model in backend/apps/[app]/models.py"
```

---

## Implementation Strategy

### MVP First (User Story 1 Only)

1. Complete Phase 1: Setup
2. Complete Phase 2: Foundational (CRITICAL - blocks all stories)
3. Complete Phase 3: User Story 1
4. **STOP and VALIDATE**: run `make lint`, the three test layers (`make test-unit && make test-integration && make test-api`), `make smoke` and `make openapi-check` — all four, per Constitution Verification (閘門 4 前置)
5. **STOP for human review.** This is the end of the work package — do not start the next user story in the same run.

### Incremental Delivery

One work package per run (Constitution 開發工作流). Each cycle is:

1. Complete Setup + Foundational → Foundation ready → human review
2. Add User Story 1 → verify → human review → human commits
3. Add User Story 2 → verify → human review → human commits
4. Each story adds value without breaking previous stories

Deployment is NOT part of a story cycle here: the application's own orchestration is
Phase 4 scope (see `docs/plan/13`). Do not add deploy/demo steps to a user story.

### Single-developer reality

This project is one human + AI, not a team. Ignore any advice about splitting stories
across developers: the `[P]` markers describe files that do not collide, so that a single
run can batch them — they do not imply parallel staffing.

---

## Notes

- [P] tasks = different files, no dependencies
- [Story] label maps task to specific user story for traceability
- Each user story should be independently completable and testable
- Verify tests fail before implementing, and have a human review them before writing implementation code
- **Never run git yourself.** Constitution 開發工作流 §Git 安全規則: after finishing the code changes, report Changed Files / Summary / Impact Analysis / a suggested commit message, then STOP. The human runs `git add` / `commit` / `push`, then `make ci-status`
- Stop at any checkpoint to validate story independently
- Avoid: vague tasks, same file conflicts, cross-story dependencies that break independence
- Tasks MUST NOT introduce requirements absent from `spec.md`, change `plan.md`, expand scope, or make unrequested "while I'm here" improvements (Constitution Principle VI)
