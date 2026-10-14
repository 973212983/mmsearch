<script setup>
import { onMounted, ref } from 'vue'
import Icon from './Icon.vue'
import Logo from './Logo.vue'
import { call } from '../api'

const ICONS = { documents: 'file', pictures: 'image', videos: 'video', music: 'music', desktop: 'monitor', downloads: 'download' }
const known = ref([])
const picked = ref(new Set())

onMounted(async () => (known.value = (await call('known_folders')) || []))

function toggle(path) {
  const s = new Set(picked.value)
  s.has(path) ? s.delete(path) : s.add(path)
  picked.value = s
}
async function pickOther() {
  const chosen = [...picked.value]
  // 选好文件夹后欢迎页就消失了，已勾选的常用位置要一起加上，不能丢；取消对话框则什么都不加
  if (await call('pick_folder') && chosen.length) call('add_folders', chosen)
}
</script>

<template>
  <div class="welcome">
    <Logo :size="48" />
    <h2>选择要搜索的文件夹</h2>
    <p>MMSearch 会读取其中的文档、图片、音频和视频（含子文件夹），之后用一句话就能找到它们。</p>

    <template v-if="known.length">
      <div class="known" role="group" aria-label="常用位置">
        <button v-for="f in known" :key="f.key" class="kf" :class="{ on: picked.has(f.path) }" :title="f.path"
                :aria-pressed="picked.has(f.path)" @click="toggle(f.path)">
          <span class="kf-ic"><Icon :name="picked.has(f.path) ? 'check' : ICONS[f.key] || 'folder'" :size="15" :stroke="picked.has(f.path) ? 2.4 : 1.8" /></span>
          {{ f.label }}
        </button>
      </div>
      <div class="go">
        <button class="btn primary" :disabled="!picked.size" @click="call('add_folders', [...picked])">
          开始索引{{ picked.size ? `（${picked.size} 个文件夹）` : '' }}
        </button>
        <button class="btn" @click="pickOther"><Icon name="folder-plus" :size="16" /> 其他文件夹…</button>
      </div>
    </template>
    <button v-else class="btn primary" @click="call('pick_folder')"><Icon name="folder-plus" :size="16" /> 选择文件夹</button>

    <span class="muted">也可以直接把文件夹拖到这里 · 所有内容只在本机处理</span>
  </div>
</template>

<style scoped>
.welcome { flex: 1; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 12px; padding: 0 120px; text-align: center; animation: fade-in .2s; }
h2 { margin: 8px 0 0; font: 600 20px var(--font-display); }
p { margin: 0 0 6px; color: var(--text-2); line-height: 1.7; }
.known { display: grid; grid-template-columns: repeat(3, 132px); gap: 8px; }
.kf { display: flex; align-items: center; gap: 10px; height: 42px; padding: 0 12px; border-radius: 9px; border: 1px solid var(--line-2); background: var(--raise); color: var(--text); font-size: 13.5px; transition: border-color .12s, background .12s; }
.kf:hover { border-color: color-mix(in srgb, var(--accent) 50%, var(--line-2)); }
.kf-ic { display: grid; place-items: center; width: 24px; height: 24px; border-radius: 6px; background: var(--raise-2); color: var(--text-2); }
.kf.on { border-color: var(--accent); background: var(--sel); }
.kf.on .kf-ic { background: var(--accent); color: var(--accent-ink); }
.go { display: flex; gap: 8px; margin-top: 6px; }
.go .btn, .welcome > .btn { height: 36px; padding: 0 18px; }
.muted { margin-top: 2px; font-size: 12px; color: var(--text-3); }
</style>
