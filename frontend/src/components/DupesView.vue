<script setup>
import { onMounted, ref, watch } from 'vue'
import Icon from './Icon.vue'
import { S } from '../store'
import { call } from '../api'
import { fmtSize, fmtDate } from '../format'

const LEVELS = [
  { key: 'same', label: '几乎相同', tip: '同一张图的副本、缩放或重新压缩过的版本' },
  { key: 'similar', label: '相似', tip: '连拍、同一场景或同一界面的不同截图' },
]
const level = ref('same')
const groups = ref(null)
const loading = ref(false)

async function load() {
  loading.value = true
  const r = await call('find_similar_images', level.value)
  loading.value = false
  groups.value = r?.groups || []
}
onMounted(load)
watch(level, load)

const files = (g) => g.length
const waste = (g) => g.slice(1).reduce((a, h) => a + (h.size || 0), 0) // 只留一张时可以腾出的空间
</script>

<template>
  <div class="view">
    <div class="vh">
      <div>
        <h2><button class="back" aria-label="返回索引管理" @click="S.view = 'index'"><Icon name="chevron" :size="16" /></button>相似和重复的图片</h2>
        <p>按图片内容分组。单击在资源管理器中显示，双击打开；这里不会删除任何文件。</p>
      </div>
      <div class="seg" role="tablist">
        <button v-for="l in LEVELS" :key="l.key" :class="{ on: level === l.key }" :title="l.tip" role="tab" :aria-selected="level === l.key" @click="level = l.key">{{ l.label }}</button>
      </div>
    </div>

    <div v-if="loading || !groups" class="none"><span class="spinner lg" /></div>
    <div v-else-if="!groups.length" class="none">
      <Icon name="image" :size="22" />
      <b>没有找到{{ LEVELS.find((l) => l.key === level).label }}的图片</b>
      <span v-if="S.index.running">索引还没完成，完成后再来看看</span>
    </div>
    <template v-else>
      <p class="sum">{{ groups.length }} 组，共 {{ groups.reduce((a, g) => a + files(g), 0) }} 张图片</p>
      <div v-for="(g, i) in groups" :key="i" class="group">
        <div class="gh">{{ files(g) }} 张<span v-if="waste(g)"> · 只留一张可腾出 {{ fmtSize(waste(g)) }}</span></div>
        <div class="tiles">
          <button v-for="h in g" :key="h.id" class="tile" :title="h.path" @click="call('reveal', h.path)" @dblclick="call('open', h.path)">
            <img :src="h.thumb" alt="" loading="lazy" />
            <span class="nm">{{ h.name }}</span>
            <span class="mt">{{ h.folder }} · {{ h.size ? fmtSize(h.size) : '' }} · {{ fmtDate(h.mtime) }}</span>
          </button>
        </div>
      </div>
    </template>
  </div>
</template>

<style scoped>
.view { height: 100%; overflow-y: auto; padding: 20px 24px 24px; animation: fade-in .12s; }
.vh { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; margin-bottom: 14px; }
h2 { display: flex; align-items: center; gap: 6px; margin: 0; font: 600 18px var(--font-display); }
.back { display: grid; place-items: center; width: 26px; height: 26px; border: none; border-radius: 6px; background: transparent; color: var(--text-2); transform: rotate(90deg); }
.back:hover { background: var(--raise-2); }
.vh p { margin: 4px 0 0; font-size: 12.5px; color: var(--text-3); }
.seg { display: flex; flex: none; padding: 2px; border-radius: 8px; background: var(--raise-2); }
.seg button { height: 28px; padding: 0 12px; border: none; border-radius: 6px; background: transparent; color: var(--text-2); font-size: 12.5px; }
.seg button.on { background: var(--raise); color: var(--text); font-weight: 600; box-shadow: 0 1px 2px rgba(0, 0, 0, .12); }
.none { display: flex; flex-direction: column; align-items: center; gap: 8px; padding: 60px 0; color: var(--text-3); font-size: 12.5px; }
.none b { color: var(--text-2); font-size: 14px; }
.sum { margin: 0 0 10px; font-size: 12px; color: var(--text-3); }
.group { margin-bottom: 14px; padding: 10px 12px 12px; border-radius: 10px; background: var(--raise); }
.gh { margin-bottom: 8px; font-size: 12px; font-weight: 600; color: var(--text-2); }
.gh span { font-weight: 400; color: var(--text-3); }
.tiles { display: grid; grid-template-columns: repeat(auto-fill, minmax(132px, 1fr)); gap: 8px; }
.tile { display: flex; flex-direction: column; gap: 3px; padding: 6px; border: 1px solid transparent; border-radius: 8px; background: transparent; text-align: left; min-width: 0; }
.tile:hover { border-color: var(--line-2); background: var(--raise-2); }
.tile img { width: 100%; aspect-ratio: 4 / 3; object-fit: contain; border-radius: 5px; background: #0d0e10; }
.nm { font-size: 12px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.mt { font-size: 11px; color: var(--text-3); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
</style>
