/**
 * 型別的單一 import 入口（03 §2）。
 *
 * 應用程式一律從這裡取型別，不直接 import `@/api/generated/*`——那個路徑是產物，
 * codegen 的輸出檔名與結構會隨工具版本變（openapi-typescript 換版時改過一次），
 * 全專案散落著它的路徑時，升版會變成一次大範圍改動。
 */
import type { components } from '@/api/generated/schema'

export type { components, operations, paths } from '@/api/generated/schema'

/** RFC 9457 錯誤回應（09 §1.3）。`api/client.ts` 的正規化以此為契約。 */
export type ProblemDetail = components['schemas']['ProblemDetail']

/** 422 的欄位級明細。 */
export type FieldError = components['schemas']['FieldError']

/** 登入／refresh 的回應（09 §2.1）。只有 access token，refresh 走 httpOnly cookie。 */
export type TokenPairOut = components['schemas']['TokenPairOut']

/** 登入者本人（`GET /users/me`）。 */
export type UserOut = components['schemas']['UserOut']

/** 知識庫（`GET /knowledge-bases`；09 §2.3）。 */
export type KnowledgeBaseOut = components['schemas']['KnowledgeBaseOut']

/** 建立知識庫的輸入。 */
export type KnowledgeBaseCreateIn = components['schemas']['KnowledgeBaseCreateIn']

/** 部分更新：`null` 代表「這次沒給」，不是「設為空」（後端 schema 的原話）。 */
export type KnowledgeBaseUpdateIn = components['schemas']['KnowledgeBaseUpdateIn']

export type KnowledgeBaseListOut = components['schemas']['KnowledgeBaseListOut']

/** 文件（含 ETL 狀態與失敗原因；08 §2、§6）。刻意沒有 storage_key。 */
export type DocumentOut = components['schemas']['DocumentOut']

export type DocumentListOut = components['schemas']['DocumentListOut']
