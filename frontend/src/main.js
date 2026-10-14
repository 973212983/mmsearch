import { createApp } from 'vue'
import './style.css'
import App from './App.vue'
import * as store from './store'
import { call } from './api'

// 主题：跟随系统 / 浅色 / 深色
const mq = window.matchMedia('(prefers-color-scheme: dark)')
export function applyTheme() {
  const t = store.S.settings.theme || 'system'
  document.documentElement.dataset.theme = t === 'system' ? (mq.matches ? 'dark' : 'light') : t
  store.applyBackground() // 背景图/模糊/暗度/磨砂开关
  if (store.S.booted) call('apply_native') // 原生窗口：磨砂 + 深色边框（系统主题变化时也要重设）
}
mq.addEventListener('change', applyTheme)
window.mm = { ...store, applyTheme } // 便于调试与自动化测试

// 禁用浏览器默认右键菜单与缩放手势，让它更像原生应用
window.addEventListener('contextmenu', (e) => {
  if (!['INPUT', 'TEXTAREA'].includes(e.target.tagName) && !window.getSelection()?.toString()) e.preventDefault()
})
window.addEventListener('wheel', (e) => e.ctrlKey && e.preventDefault(), { passive: false })

// 原生拖拽不会触发网页 resize/move 事件；逐帧读取窗口屏幕坐标，让磨砂底图实时跟随。
function trackBackdrop() {
  store.syncBackdropPosition()
  requestAnimationFrame(trackBackdrop)
}
requestAnimationFrame(trackBackdrop)

applyTheme()
createApp(App).mount('#app')
