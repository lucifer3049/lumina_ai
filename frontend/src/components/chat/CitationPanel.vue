<script setup lang="ts">
/**
 * 來源面板（1E-3；06 §3.3、09 §3.2）。
 *
 * `snippet` 是後端在檢索當下拍的一張照片——文件之後被 re-ingest 或刪除，這則回答
 * 仍看得出當初依據了什麼。所以這裡顯示的是事件裡的片段，不是回頭去抓文件現在的內容。
 *
 * 點引用跳原文並標黃排在 2D（13 §3 缺口②），這一版只到「看得到出處與片段」。
 * 視覺：攤開的箋紙；編號是小朱印，與訊息裡的引用記號同款。
 */
import BrushDivider from '@/components/ui/BrushDivider.vue'

import type { CitationItem } from '@/utils/citations'

const props = defineProps<{
  citations: readonly CitationItem[]
  /** 被點到的那一筆（1 起算），用來highlight。 */
  activeIndex?: number | null
}>()

/** 頁碼與章節路徑二選一：PDF/docx 有頁碼，Markdown 與 xlsx 只有章節（09 §3.2）。 */
function locationOf(citation: CitationItem): string {
  if (typeof citation.page === 'number') {
    return `第 ${citation.page} 頁`
  }
  const path = citation.heading_path
  return Array.isArray(path) && path.length > 0 ? path.join(' › ') : ''
}
</script>

<template>
  <aside class="panel">
    <div class="head">
      <h2 class="title">引用來源</h2>
      <BrushDivider class="divider" />
    </div>

    <p v-if="props.citations.length === 0" class="empty">這一回合沒有引用知識庫</p>
    <ul v-else class="list">
      <li
        v-for="(citation, index) in props.citations"
        :key="citation.chunk_id ?? index"
        class="item"
        :class="{ active: props.activeIndex === index + 1 }"
      >
        <div class="item-head">
          <span class="num">{{ index + 1 }}</span>
          <span class="doc-name">{{ citation.doc_name ?? '未知文件' }}</span>
        </div>
        <p class="snippet">{{ citation.snippet ?? '' }}</p>
        <span v-if="locationOf(citation)" class="location">{{ locationOf(citation) }}</span>
      </li>
    </ul>
  </aside>
</template>

<style scoped>
.panel {
  width: 19rem;
  flex: none;
  background: color-mix(in srgb, var(--paper-2) 70%, transparent);
  border: 1px solid var(--paper-5);
  border-radius: 3px 2px 4px 2px;
  padding: 22px 20px;
  display: flex;
  flex-direction: column;
  gap: 16px;
  box-sizing: border-box;
  /* 與對話欄同高（父層 stretch），來源多時在紙內捲，不把畫面撐長 */
  max-height: 100%;
  overflow-y: auto;
}

.head {
  display: flex;
  flex-direction: column;
  gap: 9px;
}

.title {
  margin: 0;
  font-family: var(--font-serif);
  font-size: 1rem;
  font-weight: 700;
  letter-spacing: 0.1em;
  color: var(--ink-1);
}

.divider {
  width: 120px;
}

.empty {
  margin: 0;
  font-size: 0.8125rem;
  color: var(--ink-4);
}

.list {
  margin: 0;
  padding: 0;
  list-style: none;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.item {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 10px;
  border-radius: var(--radius-a);
  border-bottom: 1px solid var(--paper-4);
  transition: background-color var(--dur-med) var(--ease-ink);
}

.item:last-child {
  border-bottom: none;
}

.item.active {
  background: color-mix(in srgb, var(--paper-3) 85%, transparent);
}

.item-head {
  display: flex;
  align-items: center;
  gap: 9px;
}

.num {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 18px;
  height: 18px;
  border: 1px solid color-mix(in srgb, var(--cinnabar) 50%, transparent);
  border-radius: 2px;
  background: color-mix(in srgb, var(--cinnabar) 8%, transparent);
  color: var(--cinnabar);
  font-size: 0.6875rem;
  transform: rotate(-1deg);
}

.doc-name {
  font-family: var(--font-serif);
  font-size: 0.8125rem;
  font-weight: 600;
  color: var(--ink-1);
}

.snippet {
  margin: 0;
  font-size: 0.75rem;
  line-height: 1.85;
  color: var(--ink-2);
}

.location {
  font-size: 0.6875rem;
  color: var(--ink-4);
}
</style>
