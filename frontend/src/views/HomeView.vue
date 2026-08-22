<script setup lang="ts">
/**
 * 首頁（設計稿「首頁三案」D 版定稿，2026-08-22）：
 * 整幅山水動景（霧慢移、舟緩行、波光緩變）當底，中央是題字＋一句話輸入列，
 * 下方兩張薄紙掛「最近對話」與「知識庫」，游標滑過以晨光色柔柔暈開。
 *
 * 輸入列送出＝把草稿放進 chat store 的 `pendingDraft` 再導到 /chat，由對話頁
 * 取走並走「第一句才建立對話」的既有流程——首頁不重複實作建立與串流。
 *
 * 資料載入失敗一律安靜（.catch 空）：首頁是門面，兩欄是輔助資訊，
 * 載不到就顯示空狀態，不用錯誤打斷剛進門的人。
 */
import { computed, onMounted, ref } from 'vue'
import { RouterLink, useRouter } from 'vue-router'

import { useChatStore } from '@/stores/chat'
import { useKnowledgeStore } from '@/stores/knowledge'
import { relativeDayLabel } from '@/utils/conversationGroups'

const router = useRouter()
const chat = useChatStore()
const knowledge = useKnowledgeStore()

const question = ref('')

const recentConversations = computed(() => chat.conversations.slice(0, 4))
const knowledgeBases = computed(() => knowledge.knowledgeBases.slice(0, 4))

onMounted(() => {
  void chat.fetchConversations().catch(() => {})
  void knowledge.fetchKnowledgeBases().catch(() => {})
})

function ask(): void {
  const content = question.value.trim()
  if (content === '') {
    return
  }
  chat.pendingDraft = content
  question.value = ''
  void router.push({ name: 'chat' })
}

function openConversation(conversationId: string): void {
  void router.push({ name: 'chat-conversation', params: { conversationId } })
}
</script>

<template>
  <div class="home ink-appear">
    <!-- 山水動景：遠山三重入霧、水面舟行、波光緩變（零描邊，全 token 供色，夜間自轉月下） -->
    <svg viewBox="0 0 1280 470" class="scene" preserveAspectRatio="none" aria-hidden="true">
      <defs>
        <filter id="hm-soft"><feGaussianBlur stdDeviation="5"></feGaussianBlur></filter>
        <linearGradient id="hm-far" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" style="stop-color: var(--peak-far)"></stop>
          <stop offset="1" style="stop-color: var(--peak-far); stop-opacity: 0"></stop>
        </linearGradient>
        <linearGradient id="hm-mid" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" style="stop-color: var(--peak-mid)"></stop>
          <stop offset="1" style="stop-color: var(--peak-mid); stop-opacity: 0"></stop>
        </linearGradient>
        <linearGradient id="hm-near" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" style="stop-color: var(--peak-near)"></stop>
          <stop offset="1" style="stop-color: var(--peak-near); stop-opacity: 0.05"></stop>
        </linearGradient>
        <linearGradient id="hm-water" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" style="stop-color: var(--water-1)"></stop>
          <stop offset="1" style="stop-color: var(--water-2)"></stop>
        </linearGradient>
        <linearGradient id="hm-mist" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" style="stop-color: var(--mist); stop-opacity: 0"></stop>
          <stop offset="0.5" style="stop-color: var(--mist); stop-opacity: 0.85"></stop>
          <stop offset="1" style="stop-color: var(--mist); stop-opacity: 0.2"></stop>
        </linearGradient>
      </defs>
      <path
        d="M0 240 C 180 140, 380 120, 560 170 C 720 214, 900 210, 1080 180 C 1160 168, 1230 168, 1280 174 L 1280 470 L 0 470 Z"
        fill="url(#hm-far)"
        opacity="0.5"
        filter="url(#hm-soft)"
      ></path>
      <path
        d="M120 300 C 300 210, 520 196, 720 240 C 900 278, 1100 272, 1280 244 L 1280 470 L 120 470 Z"
        fill="url(#hm-mid)"
        opacity="0.55"
      ></path>
      <g class="mist-a"><rect x="-140" y="240" width="1560" height="130" fill="url(#hm-mist)"></rect></g>
      <path
        d="M0 372 C 220 320, 420 306, 640 336 C 860 366, 1080 362, 1280 330 L 1280 470 L 0 470 Z"
        fill="url(#hm-near)"
        opacity="0.7"
      ></path>
      <g class="mist-b"><rect x="-140" y="330" width="1560" height="110" fill="url(#hm-mist)" opacity="0.8"></rect></g>
      <rect x="0" y="404" width="1280" height="66" fill="url(#hm-water)"></rect>
      <ellipse class="shimmer" cx="470" cy="430" rx="150" ry="5" style="fill: var(--shimmer)"></ellipse>
      <ellipse class="shimmer shimmer--late" cx="900" cy="446" rx="110" ry="4" style="fill: var(--shimmer)"></ellipse>
      <rect x="200" y="424" width="90" height="1.6" style="fill: var(--water-line)" opacity="0.8"></rect>
      <rect x="640" y="452" width="120" height="1.6" style="fill: var(--water-line)" opacity="0.6"></rect>
      <rect x="1020" y="432" width="80" height="1.6" style="fill: var(--water-line)" opacity="0.7"></rect>
      <g class="boat">
        <path d="M0 428 C 10 434, 34 434, 44 428 L 36 428 C 26 431, 18 431, 8 428 Z" style="fill: var(--boat-1)"></path>
        <rect x="20" y="410" width="1.6" height="18" style="fill: var(--boat-1)"></rect>
        <path d="M21.6 410 C 28 414, 30 420, 28 426 L 21.6 426 Z" style="fill: var(--boat-1)" opacity="0.85"></path>
      </g>
    </svg>

    <div class="inner">
      <header class="hero">
        <p class="hero-title">智啟千年 · 知識如水</p>
        <p class="hero-sub">想查什麼，直接問——回答會附知識庫的出處</p>
      </header>

      <form class="ask" @submit.prevent="ask">
        <input
          v-model="question"
          class="ask-input"
          type="text"
          placeholder="問一個問題，直接開始對話…"
          aria-label="問一個問題，直接開始對話"
        />
        <button type="submit" class="ask-send" aria-label="送出並開始對話">
          <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
            <path d="M4 12 L20 4 L14 20 L11 13 Z"></path>
          </svg>
        </button>
      </form>

      <div class="desk">
        <section class="desk-panel">
          <div class="panel-head">
            <span class="panel-label">最近對話</span>
            <RouterLink class="panel-link" :to="{ name: 'chat' }">查看全部</RouterLink>
          </div>
          <p v-if="recentConversations.length === 0" class="panel-empty">還沒有對話，問一句就開始。</p>
          <button
            v-for="item in recentConversations"
            :key="item.id"
            type="button"
            class="panel-row"
            @click="openConversation(item.id)"
          >
            <span class="row-main">{{ item.title === '' ? '（未命名）' : item.title }}</span>
            <span class="row-side">{{ relativeDayLabel(item.last_message_at) }}</span>
          </button>
        </section>

        <section class="desk-panel">
          <div class="panel-head">
            <span class="panel-label">知識庫</span>
            <RouterLink class="panel-link" :to="{ name: 'knowledge' }">管理知識庫</RouterLink>
          </div>
          <p v-if="knowledgeBases.length === 0" class="panel-empty">還沒有知識庫，先上傳幾份文件吧。</p>
          <RouterLink
            v-for="kb in knowledgeBases"
            :key="kb.id"
            class="panel-row"
            :to="{ name: 'knowledge-documents', params: { kbId: kb.id } }"
          >
            <span class="row-main">
              <span class="kb-dot" aria-hidden="true"></span>
              {{ kb.name }}
            </span>
            <span class="row-side">{{ kb.document_count }} 份文件</span>
          </RouterLink>
        </section>
      </div>
    </div>
  </div>
</template>

<style scoped>
.home {
  flex: 1;
  min-height: 0;
  position: relative;
  display: flex;
  overflow: hidden;
  background: linear-gradient(180deg, var(--sky-1) 0%, var(--glow) 52%, var(--sky-3) 100%);
}

.scene {
  position: absolute;
  left: 0;
  right: 0;
  bottom: 0;
  width: 100%;
  height: min(470px, 62%);
  pointer-events: none;
}

/* 霧、舟、波光：慢到像沒在動，才是山水（reduced-motion 由 main.css 全域規則收掉） */
.mist-a {
  animation: hm-mist-drift 90s var(--ease-ink) infinite alternate;
}

.mist-b {
  animation: hm-mist-drift 130s var(--ease-ink) infinite alternate-reverse;
}

.boat {
  animation: hm-boat-sail 180s linear infinite;
}

.shimmer {
  animation: hm-shimmer 9s ease-in-out infinite;
}

.shimmer--late {
  animation-delay: 4s;
}

@keyframes hm-mist-drift {
  from {
    transform: translateX(0);
  }

  to {
    transform: translateX(70px);
  }
}

@keyframes hm-boat-sail {
  from {
    transform: translateX(-120px);
  }

  to {
    transform: translateX(calc(100vw + 140px));
  }
}

@keyframes hm-shimmer {
  0% {
    opacity: 0.25;
  }

  50% {
    opacity: 0.6;
  }

  100% {
    opacity: 0.25;
  }
}

.inner {
  flex: 1;
  min-width: 0;
  position: relative;
  z-index: 1;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  align-items: center;
  padding: 72px 24px 96px;
  box-sizing: border-box;
}

.hero {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 14px;
  animation: ink-appear var(--dur-slow) var(--ease-ink) both;
}

.hero-title {
  margin: 0;
  font-family: var(--font-kai);
  font-size: clamp(1.5rem, 3.2vw, 2.125rem);
  letter-spacing: 0.32em;
  text-indent: 0.32em;
  color: var(--ink-1);
  text-align: center;
}

.hero-sub {
  margin: 0;
  font-size: 0.9375rem;
  letter-spacing: 0.06em;
  color: var(--ink-3);
  text-align: center;
}

.ask {
  width: min(760px, 100%);
  margin-top: 34px;
  display: flex;
  align-items: center;
  gap: 14px;
  animation: ink-appear var(--dur-slow) var(--ease-ink) 120ms both;
}

/* 墊高不透明度＋淡霧影：字不沉進山色（設計稿 D 的註記） */
.ask-input {
  flex: 1;
  min-width: 0;
  height: 58px;
  padding: 0 20px;
  box-sizing: border-box;
  border: 1px solid var(--paper-5);
  border-radius: var(--radius-c);
  background: color-mix(in srgb, var(--paper-1) 92%, transparent);
  box-shadow: var(--shadow-mist);
  font-family: var(--font-body);
  font-size: 0.9375rem;
  color: var(--ink-1);
  transition: border-color var(--dur-med) var(--ease-ink);
}

.ask-input::placeholder {
  color: var(--ink-4);
}

.ask-input:focus {
  outline: none;
  border-color: var(--ink-3);
}

/* 送出＝墨圓（同聊天頁的送出鈕語彙） */
.ask-send {
  width: 48px;
  height: 48px;
  flex-shrink: 0;
  border: none;
  background: var(--accent-deep);
  color: var(--accent-deep-ink);
  border-radius: 48% 52% 50% 46%;
  transform: rotate(-2deg);
  display: flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
  box-shadow: var(--shadow-ink);
  transition: transform var(--dur-fast) var(--ease-ink);
}

.ask-send:hover {
  transform: rotate(-2deg) scale(1.05);
}

.ask-send svg {
  transform: rotate(2deg);
}

.desk {
  width: min(920px, 100%);
  margin-top: 40px;
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 36px;
  animation: ink-appear var(--dur-slow) var(--ease-ink) 240ms both;
}

/* 兩張薄紙：平時近乎融進山水；游標滑過時晨光柔柔暈開（大半徑低透明度光暈，不是硬陰影）。
   夜間 --glow 自轉月光色，暈的就是月色。 */
.desk-panel {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 24px 30px 18px;
  box-sizing: border-box;
  background: color-mix(in srgb, var(--paper-1) 50%, transparent);
  border: 1px solid color-mix(in srgb, var(--paper-5) 45%, transparent);
  border-radius: var(--radius-c);
  transition:
    background-color var(--dur-slow) var(--ease-ink),
    border-color var(--dur-slow) var(--ease-ink),
    box-shadow var(--dur-slow) var(--ease-ink);
}

.desk-panel:hover {
  background: color-mix(in srgb, var(--paper-1) 82%, transparent);
  border-color: color-mix(in srgb, var(--glow) 90%, transparent);
  box-shadow:
    0 0 22px 6px color-mix(in srgb, var(--glow) 85%, transparent),
    0 0 52px 18px color-mix(in srgb, var(--glow) 40%, transparent),
    0 0 80px 30px color-mix(in srgb, var(--celadon) 22%, transparent);
}

.panel-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  margin-bottom: 6px;
}

.panel-label {
  font-size: 0.75rem;
  letter-spacing: 0.22em;
  color: var(--ink-4);
}

.panel-link {
  font-size: 0.8125rem;
}

.panel-empty {
  margin: 10px 0 8px;
  font-size: 0.8125rem;
  color: var(--ink-4);
}

.panel-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
  padding: 10px 2px;
  border: none;
  border-bottom: 1px solid var(--paper-4);
  background: transparent;
  font-family: var(--font-body);
  text-align: left;
  cursor: pointer;
  transition: background-color var(--dur-fast) var(--ease-ink);
}

.panel-row:last-child {
  border-bottom: none;
}

.panel-row:hover {
  background: color-mix(in srgb, var(--paper-3) 55%, transparent);
}

.row-main {
  min-width: 0;
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 0.875rem;
  color: var(--ink-1);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.row-side {
  flex-shrink: 0;
  font-size: 0.75rem;
  color: var(--ink-4);
}

.kb-dot {
  width: 6px;
  height: 6px;
  flex-shrink: 0;
  border-radius: 55% 45% 60% 40%;
  background: var(--celadon-ink);
}

@media (max-width: 900px) {
  .inner {
    padding: 44px 16px 72px;
  }

  .desk {
    grid-template-columns: minmax(0, 1fr);
    gap: 20px;
  }
}
</style>
