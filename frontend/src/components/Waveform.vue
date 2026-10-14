<script setup>
import { ref, watch } from 'vue'

// 真实波形（Python 端解码音频片段算峰值），按 URL 缓存
const cache = new Map()
const props = defineProps({
  url: String,
  color: { type: String, default: 'var(--c-audio)' },
  progress: { type: Number, default: -1 }, // 0~1，已播放部分高亮
  height: { type: Number, default: 56 },
})
const peaks = ref([])

watch(() => props.url, async (u) => {
  if (!u) return (peaks.value = [])
  if (cache.has(u)) return (peaks.value = cache.get(u))
  peaks.value = []
  try {
    const p = await (await fetch(u)).json()
    cache.set(u, p)
    if (u === props.url) peaks.value = p
  } catch { /* 文件不可读时留空 */ }
}, { immediate: true })
</script>

<template>
  <svg class="wave" :viewBox="`0 0 ${Math.max(peaks.length, 1) * 6} ${height}`" preserveAspectRatio="none" :style="{ height: height + 'px' }">
    <rect v-for="(p, i) in peaks" :key="i" :x="i * 6 + 1" :width="3.4" rx="1.7"
          :y="(height - Math.max(3, p * height)) / 2" :height="Math.max(3, p * height)"
          :fill="color" :opacity="progress < 0 || i / peaks.length <= progress ? 0.95 : 0.3" />
  </svg>
</template>

<style scoped>
.wave { width: 100%; display: block; }
</style>
