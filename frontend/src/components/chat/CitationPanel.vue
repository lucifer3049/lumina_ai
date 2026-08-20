<script setup lang="ts">
/**
 * 來源面板（1E-3；06 §3.3、09 §3.2）。
 *
 * `snippet` 是後端在檢索當下拍的一張照片——文件之後被 re-ingest 或刪除，這則回答
 * 仍看得出當初依據了什麼。所以這裡顯示的是事件裡的片段，不是回頭去抓文件現在的內容。
 *
 * 點引用跳原文並標黃排在 2D（13 §3 缺口②），這一版只到「看得到出處與片段」。
 */
import { NCard, NEmpty, NList, NListItem, NTag, NText } from 'naive-ui'

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
  <NCard title="來源" size="small" class="panel">
    <NEmpty v-if="props.citations.length === 0" description="這一回合沒有引用知識庫" />
    <NList v-else hoverable>
      <NListItem
        v-for="(citation, index) in props.citations"
        :key="citation.chunk_id ?? index"
        :class="{ active: props.activeIndex === index + 1 }"
      >
        <div class="head">
          <NTag size="small" round>{{ index + 1 }}</NTag>
          <NText strong>{{ citation.doc_name ?? '未知文件' }}</NText>
          <NText v-if="locationOf(citation)" depth="3">{{ locationOf(citation) }}</NText>
        </div>
        <NText depth="2" class="snippet">{{ citation.snippet ?? '' }}</NText>
      </NListItem>
    </NList>
  </NCard>
</template>

<style scoped>
.panel {
  width: 22rem;
  flex: none;
}

.head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 4px;
}

.snippet {
  display: block;
  font-size: 0.8125rem;
  line-height: 1.6;
}

.active {
  background: rgba(128, 128, 128, 0.12);
}
</style>
