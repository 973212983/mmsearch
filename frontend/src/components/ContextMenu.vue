<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import Icon from './Icon.vue'
import { S, openHit, searchSimilar, copyFile, copyPath, closeMenu } from '../store'

// 结果的右键菜单（也可以用 Shift+F10 / 菜单键打开）；搜索框保持焦点，方向键和回车由这里接管
const el = ref(null)
const idx = ref(0)
const pos = ref({ left: 0, top: 0 })

const items = computed(() => {
  const h = S.menu?.hit
  if (!h) return []
  return [
    { label: '打开', icon: 'open', keys: '↵', run: () => openHit(h) },
    { label: '在文件夹中显示', icon: 'folder-open', keys: 'Ctrl ↵', run: () => openHit(h, true) },
    { sep: true },
    { label: '查找相似内容', icon: 'sparkles', keys: 'Ctrl S', run: () => searchSimilar(h) },
    { sep: true },
    { label: '复制文件', icon: 'copy', keys: 'Ctrl C', run: () => copyFile(h) },
    { label: '复制路径', icon: 'link', keys: 'Ctrl Shift C', run: () => copyPath(h) },
  ]
})
const actionable = computed(() => items.value.map((it, i) => (it.sep ? -1 : i)).filter((i) => i >= 0))

watch(() => S.menu, async (m) => {
  idx.value = 0
  if (!m) return
  pos.value = { left: m.x, top: m.y }
  await nextTick()
  const r = el.value?.getBoundingClientRect()
  if (!r) return
  pos.value = {
    left: Math.max(6, Math.min(m.x, innerWidth - r.width - 6)),
    top: m.y + r.height > innerHeight - 6 ? Math.max(6, m.y - r.height) : m.y,
  }
})

function run(it) {
  closeMenu()
  it.run()
}
function onKey(e) {
  if (!S.menu || ['Shift', 'Control', 'Alt', 'Meta'].includes(e.key)) return
  const n = actionable.value.length
  if (e.key === 'ArrowDown' || e.key === 'ArrowUp') idx.value = (idx.value + (e.key === 'ArrowDown' ? 1 : -1) + n) % n
  else if (e.key === 'Enter') run(items.value[actionable.value[idx.value]])
  else if (e.key === 'Escape' || e.key === 'Tab') closeMenu()
  else return closeMenu() // 其他按键（例如继续打字）照常交给搜索框
  e.preventDefault()
  e.stopPropagation()
}
const onDown = (e) => S.menu && !el.value?.contains(e.target) && closeMenu()

onMounted(() => {
  window.addEventListener('keydown', onKey, true)
  window.addEventListener('mousedown', onDown, true)
  window.addEventListener('wheel', closeMenu, { passive: true })
  window.addEventListener('resize', closeMenu)
})
onBeforeUnmount(() => {
  window.removeEventListener('keydown', onKey, true)
  window.removeEventListener('mousedown', onDown, true)
  window.removeEventListener('wheel', closeMenu)
  window.removeEventListener('resize', closeMenu)
})
</script>

<template>
  <div v-if="S.menu" ref="el" class="menu" role="menu" :style="{ left: pos.left + 'px', top: pos.top + 'px' }" @contextmenu.prevent>
    <template v-for="(it, i) in items" :key="i">
      <div v-if="it.sep" class="sep" role="separator" />
      <button v-else class="mi" :class="{ on: actionable[idx] === i }" role="menuitem"
              @mouseenter="idx = actionable.indexOf(i)" @click="run(it)">
        <Icon :name="it.icon" :size="15" />
        <span class="label">{{ it.label }}</span>
        <span class="keys">{{ it.keys }}</span>
      </button>
    </template>
  </div>
</template>

<style scoped>
.menu { position: fixed; z-index: 40; min-width: 228px; padding: 5px; border-radius: 9px; border: 1px solid var(--line-2); background: var(--raise); box-shadow: var(--shadow); animation: rise .1s var(--ease); }
.mi { display: flex; align-items: center; gap: 10px; width: 100%; height: 32px; padding: 0 10px; border: none; border-radius: 6px; background: transparent; font-size: 13px; text-align: left; }
.mi .mm-ic { color: var(--text-3); }
.mi.on { background: var(--sel); }
.mi.on .mm-ic { color: var(--accent-text); }
.label { flex: 1; }
.keys { font-size: 11.5px; color: var(--text-3); }
.sep { height: 1px; margin: 4px 6px; background: var(--line); }
</style>
