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
