<script setup lang="ts">
/**
 * 朱砂印章（白文印）：紅底、字鏤空、邊緣如印泥壓出的斑駁——用 feTurbulence
 * 位移濾鏡把方塊與字的邊緣揉皺，加上幾處缺角，模擬古畫落款印的手工感。
 *
 * filter id 用 useId()：SVG 的 id 是整份文件全域的，同頁多顆印章
 * （品牌章＋落款印）若共用 id，各實例的濾鏡參數就綁死在第一顆上。
 */
import { computed, useId } from 'vue'

const props = withDefaults(defineProps<{ char?: string; size?: number }>(), {
  char: '智',
  size: 28,
})

const filterId = `seal-${useId()}`
/** 一字置中；兩字直排（落款印常見形制）。超過兩字取前兩字。 */
const glyphs = computed(() => Array.from(props.char).slice(0, 2))
</script>

<template>
  <svg
    :width="props.size"
    :height="props.size"
    viewBox="0 0 100 100"
    class="seal"
    aria-hidden="true"
  >
    <defs>
      <filter :id="filterId" x="-8%" y="-8%" width="116%" height="116%">
        <feTurbulence
          type="fractalNoise"
          baseFrequency="0.09"
          numOctaves="3"
          seed="7"
          result="grain"
        ></feTurbulence>
        <feDisplacementMap in="SourceGraphic" in2="grain" scale="6"></feDisplacementMap>
      </filter>
    </defs>
    <g :filter="`url(#${filterId})`">
      <!-- 印面：不規則方，四邊都不是直線 -->
      <path
        d="M9 11 C 7 6.5, 11 3.5, 17 4.5 C 40 2.5, 65 3, 87 4.5 C 93.5 5, 96.5 8.5, 95.5 15 C 97 38, 96.5 63, 95 85 C 94.5 91.5, 90.5 95.5, 84 94.5 C 61 96.5, 36 96, 15 94.5 C 9 94, 5 90.5, 6 83.5 C 4 59, 4.5 34, 9 11 Z"
        fill="var(--cinnabar)"
      ></path>
      <!-- 斑駁缺角：印泥沒吃到的地方 -->
      <ellipse cx="7" cy="30" rx="2.6" ry="5" fill="var(--paper-1)" opacity="0.9"></ellipse>
      <ellipse cx="93" cy="68" rx="2.2" ry="4.4" fill="var(--paper-1)" opacity="0.85"></ellipse>
      <ellipse cx="72" cy="5" rx="4" ry="1.8" fill="var(--paper-1)" opacity="0.8"></ellipse>
      <ellipse cx="30" cy="95" rx="3.4" ry="1.6" fill="var(--paper-1)" opacity="0.75"></ellipse>
      <!-- 白文：字從印面鏤空 -->
      <text
        v-if="glyphs.length === 1"
        x="50"
        y="53"
        text-anchor="middle"
        dominant-baseline="central"
        font-size="66"
        font-weight="900"
        fill="var(--paper-1)"
        style="font-family: var(--font-serif)"
      >
        {{ glyphs[0] }}
      </text>
      <template v-else>
        <text
          x="50"
          y="30"
          text-anchor="middle"
          dominant-baseline="central"
          font-size="42"
          font-weight="900"
          fill="var(--paper-1)"
          style="font-family: var(--font-serif)"
        >
          {{ glyphs[0] }}
        </text>
        <text
          x="50"
          y="71"
          text-anchor="middle"
          dominant-baseline="central"
          font-size="42"
          font-weight="900"
          fill="var(--paper-1)"
          style="font-family: var(--font-serif)"
        >
          {{ glyphs[1] }}
        </text>
      </template>
    </g>
  </svg>
</template>

<style scoped>
.seal {
  display: inline-block;
  flex-shrink: 0;
  transform: rotate(-1.5deg);
}
</style>
