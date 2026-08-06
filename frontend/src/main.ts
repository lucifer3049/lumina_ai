/**
 * App 建立與 plugin 註冊順序（03 §2）。
 *
 * pinia 必須在 router 之前註冊：router guard（1A 的 auth guard）會讀 store，
 * 而 guard 可能在首次導覽時就跑——那時 pinia 還沒裝好的話會拿到
 * 「getActivePinia() was called but there was no active Pinia」，訊息指不到真正原因。
 */
import { createPinia } from 'pinia'
import { createApp } from 'vue'

import App from './App.vue'
import router from './router'

import './assets/main.css'

createApp(App).use(createPinia()).use(router).mount('#app')
