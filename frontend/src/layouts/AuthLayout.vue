<script setup lang="ts">
/**
 * 登入頁外框（03 §2）：未登入的人沒有可導航的地方，所以沒有側欄與頁首。
 *
 * 場景是「青綠山水」（2026-08-21 v4 視覺校正定案，對齊《千里江山圖》方向）：
 * - **零描邊**：山、建築、竹、石、舟全部是填色與漸層的形體，物體從霧氣與
 *   色彩中浮現——粗墨線稿（卡通 outline）是上一版被否決的做法。
 * - **空氣透視**：遠山三重逐層向天色溶解（近濃中淡遠虛），色彩全走
 *   tokens.css 的 --scene 變數，夜間模式（data-theme='dark'）整幅自動
 *   轉為月下青綠，不需要第二份場景。
 * - 動畫分層節奏（cinematic，不是 particle）：遠霧幾乎不動、中霧慢移、
 *   前霧掠過水面；波光緩變；小舟 240 秒順流；竹葉分叢異步微動（0.5–0.9 度）。
 *   位移類用 SMIL animateMotion；`prefers-reduced-motion` 時 motionOk 直接
 *   不渲染移動元素（CSS 的全域 1ms 覆蓋管不到 SMIL）。
 * - 游標是毛筆筆鋒；墨痕降到「不注意就感覺不到」——門檻 40px、同屏 14 滴、
 *   峰值透明度 0.18。輸入框/按鈕保留 text/pointer（可用性優先）。
 *
 * 場景全是行內 SVG 與 aria-hidden，對讀屏零干擾。右側直書題字在窄螢幕隱藏。
 */
import { onMounted, ref } from 'vue'

import SealMark from '@/components/ui/SealMark.vue'
import ThemeToggle from '@/components/ui/ThemeToggle.vue'

const root = ref<HTMLElement | null>(null)
const trailHost = ref<HTMLElement | null>(null)

/** 舟、漂葉這些「會走動」的元素要不要渲染。 */
const motionOk = ref(false)
let effectsEnabled = false

let lastX = 0
let lastY = 0
let hasLast = false
let rafPending = false

onMounted(() => {
  const finePointer = window.matchMedia('(pointer: fine)').matches
  const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches
  motionOk.value = !reduced
  effectsEnabled = finePointer && !reduced
})

function onPointerMove(event: PointerEvent): void {
  if (!effectsEnabled) {
    return
  }
  const element = root.value
  if (element === null) {
    return
  }
  const { clientX, clientY } = event

  // 視差：只寫 CSS 變數，位移與緩動交給圖層的 transform transition。
  if (!rafPending) {
    rafPending = true
    requestAnimationFrame(() => {
      rafPending = false
      const nx = (clientX / element.clientWidth) * 2 - 1
      const ny = (clientY / element.clientHeight) * 2 - 1
      element.style.setProperty('--par-x', nx.toFixed(3))
      element.style.setProperty('--par-y', ny.toFixed(3))
    })
  }

  // 墨痕：高門檻低透明——像宣紙極淡地吸了一點墨，注意到才會發現。
  if (!hasLast) {
    lastX = clientX
    lastY = clientY
    hasLast = true
    return
  }
  const dx = clientX - lastX
  const dy = clientY - lastY
  const distance = Math.hypot(dx, dy)
  if (distance < 40) {
    return
  }
  lastX = clientX
  lastY = clientY
  spawnBlot(clientX, clientY, dx, dy, distance)
}

function spawnBlot(x: number, y: number, dx: number, dy: number, distance: number): void {
  const host = trailHost.value
  if (host === null || host.childElementCount >= 14) {
    return
  }
  const blot = document.createElement('span')
  blot.className = 'trail-blot'
  const length = Math.min(6 + distance * 0.14, 16)
  const angle = (Math.atan2(dy, dx) * 180) / Math.PI
  blot.style.left = `${x}px`
  blot.style.top = `${y}px`
  blot.style.width = `${length}px`
  blot.style.setProperty('--blot-angle', `${angle.toFixed(1)}deg`)
  blot.addEventListener('animationend', () => {
    blot.remove()
  })
  host.appendChild(blot)
}
</script>

<template>
  <main ref="root" class="auth-layout" @pointermove="onPointerMove">
    <!-- ══ 青綠山水 ══ -->
    <svg viewBox="0 0 1440 900" class="scene" preserveAspectRatio="xMidYMax slice" aria-hidden="true">
      <defs>
        <linearGradient id="qs-sky" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" style="stop-color: var(--sky-1)"></stop>
          <stop offset="0.62" style="stop-color: var(--sky-2)"></stop>
          <stop offset="1" style="stop-color: var(--sky-3)"></stop>
        </linearGradient>
        <linearGradient id="qs-peak-far" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" style="stop-color: var(--peak-far)"></stop>
          <stop offset="1" style="stop-color: var(--peak-far); stop-opacity: 0"></stop>
        </linearGradient>
        <linearGradient id="qs-peak-mid" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" style="stop-color: var(--peak-mid)"></stop>
          <stop offset="1" style="stop-color: var(--peak-mid); stop-opacity: 0"></stop>
        </linearGradient>
        <linearGradient id="qs-peak-near" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" style="stop-color: var(--peak-near)"></stop>
          <stop offset="0.85" style="stop-color: var(--peak-near); stop-opacity: 0.25"></stop>
          <stop offset="1" style="stop-color: var(--peak-near); stop-opacity: 0"></stop>
        </linearGradient>
        <linearGradient id="qs-ridge" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" style="stop-color: var(--ridge)"></stop>
          <stop offset="0.9" style="stop-color: var(--ridge); stop-opacity: 0.2"></stop>
          <stop offset="1" style="stop-color: var(--ridge); stop-opacity: 0"></stop>
        </linearGradient>
        <linearGradient id="qs-water" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" style="stop-color: var(--water-1)"></stop>
          <stop offset="0.5" style="stop-color: var(--water-2)"></stop>
          <stop offset="1" style="stop-color: var(--water-3)"></stop>
        </linearGradient>
        <linearGradient id="qs-mist-h" x1="0" y1="0" x2="1" y2="0">
          <stop offset="0" style="stop-color: var(--mist); stop-opacity: 0"></stop>
          <stop offset="0.4" style="stop-color: var(--mist); stop-opacity: 0.85"></stop>
          <stop offset="0.6" style="stop-color: var(--mist); stop-opacity: 0.85"></stop>
          <stop offset="1" style="stop-color: var(--mist); stop-opacity: 0"></stop>
        </linearGradient>
        <linearGradient id="qs-mist-v" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" style="stop-color: var(--mist); stop-opacity: 0"></stop>
          <stop offset="0.5" style="stop-color: var(--mist); stop-opacity: 0.9"></stop>
          <stop offset="1" style="stop-color: var(--mist); stop-opacity: 0"></stop>
        </linearGradient>
        <filter id="qs-soft3"><feGaussianBlur stdDeviation="3"></feGaussianBlur></filter>
        <filter id="qs-soft6"><feGaussianBlur stdDeviation="6"></feGaussianBlur></filter>
        <filter id="qs-soft10"><feGaussianBlur stdDeviation="10"></feGaussianBlur></filter>
        <!-- 舟與漂葉的水路 -->
        <path id="qs-stream" d="M1520 706 C 1200 722, 900 742, 620 754 C 460 761, 340 766, 220 772" fill="none"></path>
      </defs>

      <!-- ── 遠：天色、晨光（夜＝月光）、群峰入霧 ── -->
      <g class="layer layer--far">
        <rect x="-60" y="-20" width="1560" height="680" fill="url(#qs-sky)"></rect>
        <ellipse cx="1020" cy="250" rx="340" ry="180" fill="var(--glow)" opacity="0.55" filter="url(#qs-soft10)"></ellipse>

        <path d="M-60 430 C 60 330, 150 280, 250 306 C 330 327, 390 368, 470 386 C 560 406, 660 398, 760 376 C 700 470, 560 520, 400 520 C 240 520, 80 500, -60 510 Z" fill="url(#qs-peak-far)" opacity="0.75" filter="url(#qs-soft6)"></path>
        <path d="M480 470 C 590 360, 700 316, 830 340 C 940 360, 1040 402, 1160 410 C 1270 417, 1370 404, 1500 386 L 1500 560 C 1200 570, 800 570, 480 560 Z" fill="url(#qs-peak-mid)" opacity="0.8" filter="url(#qs-soft3)"></path>
        <path d="M900 520 C 1000 440, 1110 410, 1230 434 C 1310 450, 1390 470, 1500 478 L 1500 620 L 900 620 Z" fill="url(#qs-peak-near)" opacity="0.85"></path>

        <rect x="-60" y="440" width="1560" height="150" fill="url(#qs-mist-v)"></rect>
        <g class="drift drift--far"><rect x="-260" y="420" width="1200" height="100" fill="url(#qs-mist-h)" opacity="0.9"></rect></g>
      </g>

      <!-- ── 中：左岸山脊與臨水亭、右岸庭院、石橋 ── -->
      <g class="layer layer--mid">
        <path d="M-60 640 C 80 540, 200 490, 330 512 C 420 528, 490 570, 540 630 C 400 650, 160 650, -60 660 Z" fill="url(#qs-ridge)" opacity="0.9"></path>
        <!-- 山脊松林（樹冠色塊） -->
        <g fill="var(--pine-1)" opacity="0.65">
          <ellipse cx="150" cy="540" rx="34" ry="12"></ellipse>
          <ellipse cx="176" cy="524" rx="26" ry="10"></ellipse>
          <ellipse cx="120" cy="556" rx="26" ry="9"></ellipse>
          <ellipse cx="420" cy="560" rx="30" ry="11"></ellipse>
          <ellipse cx="446" cy="546" rx="22" ry="8"></ellipse>
        </g>
        <g fill="var(--pine-2)" opacity="0.5">
          <ellipse cx="150" cy="548" rx="22" ry="7"></ellipse>
          <ellipse cx="428" cy="566" rx="18" ry="6"></ellipse>
        </g>

        <!-- 臨水亭：黛瓦、白身、木柱、石基 -->
        <g transform="translate(238 542)">
          <path d="M-6 30 C -10 32, -14 33, -18 33 C -8 22, 8 15, 30 13 C 52 15, 68 22, 78 33 C 74 33, 70 32, 66 30 L 60 34 L 0 34 Z" fill="var(--roof-1)"></path>
          <path d="M2 20 C 10 12, 22 8, 30 8 C 38 8, 50 12, 58 20 C 48 16, 38 14, 30 14 C 22 14, 12 16, 2 20 Z" fill="var(--roof-2)"></path>
          <rect x="27" y="2" width="6" height="7" rx="1" fill="var(--roof-1)"></rect>
          <rect x="4" y="34" width="52" height="20" fill="var(--wall-1)"></rect>
          <rect x="4" y="34" width="52" height="4" fill="var(--wall-2)"></rect>
          <rect x="8" y="34" width="3.5" height="20" fill="var(--wood)"></rect>
          <rect x="48.5" y="34" width="3.5" height="20" fill="var(--wood)"></rect>
          <rect x="28" y="34" width="3" height="20" fill="var(--wood)" opacity="0.7"></rect>
          <rect x="0" y="54" width="60" height="5" fill="var(--stone-1)"></rect>
          <rect x="-4" y="59" width="68" height="4" fill="var(--stone-4)" opacity="0.8"></rect>
        </g>

        <!-- 右岸庭院：白牆、黛瓦、木格窗（一暗一亮）、院牆與院中竹 -->
        <g transform="translate(830 500)">
          <path d="M64 40 C 60 42, 55 43, 50 43 C 62 28, 84 20, 112 19 C 140 20, 162 28, 174 43 C 169 43, 164 42, 160 40 L 152 46 L 72 46 Z" fill="var(--roof-3)"></path>
          <path d="M70 27 C 82 18, 98 14, 112 14 C 126 14, 142 18, 154 27 C 140 22, 126 20, 112 20 C 98 20, 84 22, 70 27 Z" fill="var(--roof-4)"></path>
          <rect x="72" y="46" width="80" height="34" fill="var(--wall-1)"></rect>
          <rect x="72" y="46" width="80" height="5" fill="var(--wall-2)"></rect>
          <g transform="translate(84 56)">
            <rect x="0" y="0" width="16" height="18" fill="var(--lattice-dark)"></rect>
            <path d="M5.3 0 L 5.3 18 M 10.6 0 L 10.6 18 M 0 6 L 16 6 M 0 12 L 16 12" stroke="var(--lattice-line)" stroke-width="1"></path>
          </g>
          <!-- 亮著的那扇窗（院裡有人） -->
          <ellipse cx="132" cy="65" rx="22" ry="16" fill="var(--window-lit)" opacity="0.16" filter="url(#qs-soft6)"></ellipse>
          <g transform="translate(124 56)">
            <rect x="0" y="0" width="16" height="18" fill="var(--window-lit)"></rect>
            <path d="M5.3 0 L 5.3 18 M 10.6 0 L 10.6 18 M 0 6 L 16 6 M 0 12 L 16 12" stroke="var(--window-lit-line)" stroke-width="1"></path>
          </g>
          <path d="M-14 74 C -18 76, -22 77, -26 77 C -16 64, 2 57, 26 56 C 50 57, 68 64, 78 77 C 74 77, 70 76, 66 74 L 58 80 L -6 80 Z" fill="var(--roof-1)"></path>
          <rect x="-6" y="80" width="64" height="26" fill="var(--wall-1)"></rect>
          <rect x="-6" y="80" width="64" height="4" fill="var(--wall-2)"></rect>
          <g transform="translate(6 88)">
            <rect x="0" y="0" width="13" height="14" fill="var(--lattice-dark)"></rect>
            <path d="M4.3 0 L 4.3 14 M 8.6 0 L 8.6 14 M 0 4.6 L 13 4.6 M 0 9.3 L 13 9.3" stroke="var(--lattice-line)" stroke-width="0.9"></path>
          </g>
          <rect x="34" y="86" width="12" height="20" fill="var(--wood)"></rect>
          <rect x="150" y="66" width="90" height="16" fill="var(--wall-1)"></rect>
          <path d="M150 66 L 240 66 L 240 62 C 210 58, 180 58, 150 62 Z" fill="var(--roof-1)"></path>
          <g opacity="0.85">
            <g class="sway sway--s1">
              <path d="M170 66 C 178 44, 192 32, 210 28 C 200 44, 190 56, 184 66 Z" fill="var(--bamboo-1)" opacity="0.8"></path>
            </g>
            <g class="sway sway--s2">
              <path d="M186 66 C 196 48, 210 38, 228 34 C 218 48, 206 58, 198 66 Z" fill="var(--bamboo-2)" opacity="0.7"></path>
            </g>
            <path d="M160 66 C 164 52, 172 42, 184 36 C 178 48, 170 58, 166 66 Z" fill="var(--bamboo-4)" opacity="0.6"></path>
          </g>
        </g>

        <!-- 石橋與橋上一點行旅 -->
        <g transform="translate(590 610)">
          <path d="M0 30 C 8 12, 30 0, 55 0 C 80 0, 102 12, 110 30 L 96 30 C 88 17, 73 9, 55 9 C 37 9, 22 17, 14 30 Z" fill="var(--bridge-1)"></path>
          <path d="M14 30 C 22 17, 37 9, 55 9 C 73 9, 88 17, 96 30 L 96 34 L 14 34 Z" fill="var(--bridge-2)" opacity="0.6"></path>
          <path d="M0 30 L 110 30 L 110 33 L 0 33 Z" fill="var(--bridge-3)"></path>
          <g transform="translate(48 -12)">
            <ellipse cx="4" cy="1.6" rx="2.4" ry="1.8" fill="var(--figure-1)"></ellipse>
            <path d="M1.5 3 C 0.5 7, 0.5 10, 1.8 12 L 6.6 12 C 7.6 9.5, 7.4 6, 6.4 3 C 5 2.4, 3 2.4, 1.5 3 Z" fill="var(--figure-2)"></path>
          </g>
        </g>

        <g class="drift drift--mid"><rect x="420" y="500" width="1300" height="90" fill="url(#qs-mist-h)" opacity="0.75"></rect></g>
      </g>

      <!-- ── 近：水面、倒影、小舟、竹、太湖石 ── -->
      <g class="layer layer--near">
        <path d="M-60 650 C 200 636, 500 630, 760 634 C 1020 638, 1280 648, 1500 660 L 1500 940 L -60 940 Z" fill="url(#qs-water)"></path>
        <!-- 倒影（模糊低透明，天光留白） -->
        <path d="M900 660 C 1000 700, 1110 716, 1230 704 C 1310 696, 1390 684, 1500 678 L 1500 640 L 900 640 Z" fill="var(--peak-near)" opacity="0.14" filter="url(#qs-soft6)"></path>
        <path d="M824 646 L 1074 650 L 1070 700 C 990 712, 900 712, 828 702 Z" fill="var(--roof-3)" opacity="0.1" filter="url(#qs-soft6)"></path>
        <path d="M-60 660 C 120 668, 300 676, 480 674 L 500 704 C 330 712, 120 706, -60 696 Z" fill="var(--ridge)" opacity="0.12" filter="url(#qs-soft6)"></path>
        <path d="M604 650 C 630 660, 668 663, 686 660 L 686 647 L 604 644 Z" fill="var(--bridge-3)" opacity="0.18" filter="url(#qs-soft3)"></path>
        <!-- 波光（明暗細帶緩變） -->
        <g class="shimmer shimmer--a">
          <path d="M240 700 C 420 694, 640 692, 820 696 L 820 699 C 640 695, 420 697, 240 703 Z" fill="var(--shimmer)" opacity="0.6"></path>
        </g>
        <g class="shimmer shimmer--b">
          <path d="M600 748 C 780 742, 980 741, 1150 745 L 1150 748 C 980 744, 780 745, 600 751 Z" fill="var(--shimmer)" opacity="0.45"></path>
        </g>
        <g fill="var(--water-line)" opacity="0.4">
          <path d="M180 730 C 300 726, 430 725, 540 728 L 540 730 C 430 727, 300 728, 180 732 Z"></path>
          <path d="M760 790 C 900 786, 1050 785, 1180 788 L 1180 790 C 1050 787, 900 788, 760 792 Z"></path>
          <path d="M330 820 C 470 816, 620 815, 750 818 L 750 820 C 620 817, 470 818, 330 822 Z"></path>
        </g>
        <ellipse class="ripple ripple--a" cx="880" cy="742" rx="26" ry="4" fill="none" stroke="var(--shimmer)" stroke-width="1" opacity="0"></ellipse>
        <ellipse class="ripple ripple--b" cx="480" cy="708" rx="20" ry="3.2" fill="none" stroke="var(--shimmer)" stroke-width="1" opacity="0"></ellipse>

        <!-- 一葉小舟（順流極慢）＋倒影 -->
        <g v-if="motionOk" opacity="0.85">
          <g>
            <path d="M-34 0 C -18 7, 20 7, 38 -2 C 30 5, 12 10, -8 10 C -20 10, -29 6, -34 0 Z" fill="var(--boat-1)"></path>
            <path d="M-8 -10 C -2 -14, 8 -14, 14 -10 C 12 -6, 10 -3, 9 -1 C 2 1, -5 1, -10 -1 C -10 -4, -9 -7, -8 -10 Z" fill="var(--boat-2)"></path>
            <path d="M-26 14 C -12 18, 14 18, 28 12 L 28 10 C 14 15, -12 15, -26 12 Z" fill="var(--boat-1)" opacity="0.18" filter="url(#qs-soft3)"></path>
            <animateMotion dur="240s" repeatCount="indefinite" keyPoints="0;0;1;1" keyTimes="0;0.05;0.96;1" calcMode="linear">
              <mpath href="#qs-stream"></mpath>
            </animateMotion>
          </g>
        </g>
        <!-- 兩片漂葉 -->
        <g v-if="motionOk" fill="var(--bamboo-2)">
          <g opacity="0.35">
            <path d="M-5 0 C -2 -2.6, 3 -2.6, 6 0 C 3 1.8, -2 1.8, -5 0 Z"></path>
            <animateMotion dur="60s" begin="8s" rotate="auto" repeatCount="indefinite" keyPoints="0;0;1;1" keyTimes="0;0.25;0.95;1" calcMode="linear">
              <mpath href="#qs-stream"></mpath>
            </animateMotion>
          </g>
          <g opacity="0.28">
            <path d="M-4 0 C -2 -2.2, 3 -2.2, 5 0 C 3 1.5, -2 1.5, -4 0 Z"></path>
            <animateMotion dur="80s" begin="34s" rotate="auto" repeatCount="indefinite" keyPoints="0;0;1;1" keyTimes="0;0.45;0.97;1" calcMode="linear">
              <mpath href="#qs-stream"></mpath>
            </animateMotion>
          </g>
        </g>

        <!-- 左前景竹（竿與葉皆填色形；葉分叢異步微動） -->
        <g transform="translate(60 40)">
          <path d="M18 0 C 15 130, 20 300, 16 480 L 24 480 C 27 300, 23 130, 26 0 Z" fill="var(--bamboo-1)"></path>
          <path d="M78 60 C 75 180, 80 330, 76 460 L 83 460 C 86 330, 82 180, 85 60 Z" fill="var(--bamboo-4)" opacity="0.85"></path>
          <path d="M132 120 C 130 220, 134 340, 131 440 L 137 440 C 139 340, 136 220, 138 120 Z" fill="var(--bamboo-5)" opacity="0.65"></path>
          <g class="sway sway--s1" fill="var(--bamboo-2)">
            <path d="M24 140 C 60 122, 104 112, 150 116 C 112 134, 66 146, 24 148 Z" opacity="0.9"></path>
            <path d="M22 160 C 52 176, 78 198, 94 226 C 62 212, 36 188, 22 160 Z" opacity="0.75"></path>
          </g>
          <g class="sway sway--s3" fill="var(--bamboo-2)">
            <path d="M84 260 C 116 244, 156 238, 196 242 C 162 258, 122 266, 84 266 Z" opacity="0.7"></path>
          </g>
          <g class="sway sway--s2" fill="var(--bamboo-2)">
            <path d="M20 370 C 48 356, 82 350, 116 354 C 88 368, 54 376, 20 376 Z" opacity="0.85"></path>
            <path d="M18 388 C 32 410, 40 434, 44 460 C 28 442, 20 416, 18 388 Z" opacity="0.65"></path>
          </g>
          <g class="sway sway--s3" fill="var(--bamboo-3)" opacity="0.55">
            <path d="M26 146 C 56 134, 92 128, 128 130 C 98 142, 62 150, 26 152 Z"></path>
            <path d="M22 372 C 44 362, 70 358, 96 360 C 74 370, 48 376, 22 378 Z"></path>
          </g>
        </g>

        <!-- 右前景：太湖石、芭蕉、野花 -->
        <g transform="translate(1270 760)">
          <path d="M-20 140 C -34 100, -26 66, 0 48 C 10 20, 40 8, 68 18 C 96 10, 122 26, 128 54 C 146 74, 144 104, 128 124 C 130 134, 128 140, 124 146 L -16 146 Z" fill="var(--stone-2)"></path>
          <path d="M8 60 C 20 52, 34 52, 44 60 C 38 70, 24 72, 14 68 C 10 66, 8 63, 8 60 Z" fill="var(--stone-3)"></path>
          <path d="M76 88 C 86 84, 96 86, 102 92 C 96 100, 84 100, 78 96 Z" fill="var(--stone-3)" opacity="0.8"></path>
          <path d="M-20 140 C -30 110, -24 80, -4 62 C -16 84, -20 112, -14 140 Z" fill="var(--stone-4)" opacity="0.7"></path>
          <g class="sway sway--s2">
            <path d="M-60 60 C -30 10, 20 -14, 78 -12 C 40 6, 8 32, -14 66 C -30 62, -46 60, -60 60 Z" fill="var(--banana-1)" opacity="0.9"></path>
            <path d="M-52 58 C -26 20, 10 -2, 56 -6 C 22 12, -6 36, -26 64 Z" fill="var(--banana-2)" opacity="0.6"></path>
          </g>
          <g opacity="0.8">
            <circle cx="-40" cy="120" r="3" fill="var(--flower-1)"></circle>
            <circle cx="-52" cy="128" r="2.4" fill="var(--flower-1)" opacity="0.8"></circle>
            <circle cx="-30" cy="132" r="2" fill="var(--flower-2)" opacity="0.7"></circle>
          </g>
        </g>

        <!-- 前景一縷薄霧掠過水面 -->
        <g class="drift drift--near"><rect x="-160" y="700" width="1100" height="80" fill="url(#qs-mist-h)" opacity="0.5"></rect></g>
      </g>
    </svg>

    <!-- 墨痕層 -->
    <div ref="trailHost" class="trail-host" aria-hidden="true"></div>

    <!-- 晝／夜 -->
    <div class="theme-corner"><ThemeToggle /></div>

    <div class="stage">
      <div class="slot ink-appear"><slot /></div>

      <!-- 題字直書 + 落款印 -->
      <div class="inscription" aria-hidden="true">
        <div class="column">
          <span class="line line--strong"><span>文人風骨</span><span>千年書香</span></span>
          <SealMark char="智" :size="20" />
        </div>
        <span class="line line--soft"><span>水墨江南</span><span>煙雨朦朧</span></span>
      </div>
    </div>
  </main>
</template>

<style scoped>
.auth-layout {
  position: relative;
  min-height: 100vh;
  overflow: hidden;
  cursor: url('/cursor-brush.svg') 2 2, auto;
  background: var(--sky-3);
}

.scene {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  display: block;
  pointer-events: none;
}

/* ── 視差：遠山幾乎不動，前景稍動 ── */
.layer {
  transition: transform 2.6s var(--ease-ink);
  will-change: transform;
}

.layer--far {
  transform: translate3d(calc(var(--par-x, 0) * -4px), calc(var(--par-y, 0) * -2px), 0);
}

.layer--mid {
  transform: translate3d(calc(var(--par-x, 0) * -10px), calc(var(--par-y, 0) * -5px), 0);
}

.layer--near {
  transform: translate3d(calc(var(--par-x, 0) * -18px), calc(var(--par-y, 0) * -9px), 0);
}

/* ── 霧：遠幾乎不動、中慢移、前掠水 ── */
.drift--far {
  animation: drift-a 140s ease-in-out infinite alternate;
}

.drift--mid {
  animation: drift-b 100s ease-in-out infinite alternate;
}

.drift--near {
  animation: drift-a 70s ease-in-out infinite alternate;
}

@keyframes drift-a {
  to {
    transform: translateX(46px);
  }
}

@keyframes drift-b {
  to {
    transform: translateX(-60px);
  }
}

/* 波光：只是明暗緩變，不位移 */
.shimmer--a {
  animation: shimmer 14s ease-in-out infinite;
}

.shimmer--b {
  animation: shimmer 19s ease-in-out 5s infinite;
}

@keyframes shimmer {
  0%,
  100% {
    opacity: 0.5;
  }

  50% {
    opacity: 1;
  }
}

.ripple--a {
  animation: ripple 11s var(--ease-ink) 3s infinite;
}

.ripple--b {
  animation: ripple 15s var(--ease-ink) 9s infinite;
}

@keyframes ripple {
  0%,
  100% {
    opacity: 0;
    transform: scale(0.6);
  }

  10% {
    opacity: 0.5;
  }

  28% {
    opacity: 0;
    transform: scale(1.6);
  }
}

/* 竹葉分叢異步微動（0.5–0.9 度） */
.sway {
  transform-box: fill-box;
  transform-origin: left center;
}

.sway--s1 {
  animation: sway-a 8s ease-in-out infinite alternate;
}

.sway--s2 {
  animation: sway-b 11s ease-in-out 2s infinite alternate;
}

.sway--s3 {
  animation: sway-a 9.5s ease-in-out 4.5s infinite alternate;
}

@keyframes sway-a {
  from {
    transform: rotate(-0.5deg);
  }

  to {
    transform: rotate(0.7deg);
  }
}

@keyframes sway-b {
  from {
    transform: rotate(0.6deg);
  }

  to {
    transform: rotate(-0.9deg);
  }
}

/* ── 墨痕：近乎不可察 ── */
.trail-host {
  position: fixed;
  inset: 0;
  z-index: 40;
  pointer-events: none;
}

.trail-host :deep(.trail-blot) {
  position: absolute;
  height: 5px;
  border-radius: 60% 40% 55% 45%;
  background: radial-gradient(closest-side, color-mix(in srgb, var(--ink-2) 30%, transparent), transparent 85%);
  transform: translate(-50%, -50%) rotate(var(--blot-angle, 0deg));
  animation: trail-fade 2.2s var(--ease-ink) forwards;
}

@keyframes trail-fade {
  0% {
    opacity: 0.18;
    scale: 0.6 0.6;
  }

  35% {
    opacity: 0.12;
    scale: 1 1;
  }

  100% {
    opacity: 0;
    scale: 1.7 1.4;
  }
}

.theme-corner {
  position: absolute;
  top: 20px;
  right: 24px;
  z-index: 50;
}

/* ── 舞台 ── */
.stage {
  position: relative;
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 88px;
  padding: 1.5rem;
  box-sizing: border-box;
}

.inscription {
  display: flex;
  gap: 22px;
  align-items: flex-start;
  padding-top: 60px;
}

.column {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 16px;
}

.line {
  writing-mode: vertical-rl;
  font-family: var(--font-kai);
  letter-spacing: 0.4em;
  line-height: 2;
}

/* 題字兩句之間空一字（直書的 inline 軸是縱向） */
.line span + span {
  margin-inline-start: 1em;
}

.line--strong {
  font-size: 1.0625rem;
  color: var(--ink-3);
}

.line--soft {
  font-size: 0.9375rem;
  letter-spacing: 0.34em;
  color: var(--ink-4);
  padding-top: 34px;
}

@media (max-width: 1100px) {
  .inscription {
    display: none;
  }

  .stage {
    gap: 0;
  }
}
</style>
