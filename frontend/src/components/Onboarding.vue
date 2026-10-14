<script setup>
import { computed, ref } from 'vue'
import Icon from './Icon.vue'
import Logo from './Logo.vue'
import { S } from '../store'
import { call, on } from '../api'

const endpoint = ref(S.settings.hf_endpoint)
const state = ref('idle') // idle | running | paused | error
const prog = ref({ done: 0, total: 0, speed: 0 })
const error = ref('')
const totalGB = computed(() => (S.missing.reduce((a, n) => a + (S.boot.model_sizes?.[n] || 0), 0) / 1024 ** 3).toFixed(1))
const pct = computed(() => (prog.value.total ? (prog.value.done / prog.value.total) * 100 : 0))
const eta = computed(() => {
  const { done, total, speed } = prog.value
  if (!speed) return ''
  const s = (total - done) / speed
  return s > 60 ? `约 ${Math.ceil(s / 60)} 分钟` : `约 ${Math.ceil(s)} 秒`
})
const mb = (n) => (n / 1024 ** 2).toFixed(0)

on('download', (d) => (prog.value = d))
on('download_failed', (d) => {
  state.value = d.paused ? 'paused' : 'error'
  error.value = d.error
})
function start() {
  if (state.value === 'running') return call('cancel_download')
  error.value = ''
  state.value = 'running'
  call('start_download', endpoint.value)
}
</script>

<template>
  <div class="ob">
    <div class="drag pywebview-drag-region" />
    <Logo :size="52" />
    <h1>开始之前，下载一次 AI 模型</h1>
    <p>MMSearch 在本机理解文本、图片、音频和视频，需要约 {{ totalGB }} GB 的模型文件。<br />下载后完全离线运行，你的文件不会离开这台电脑。</p>

    <div class="src">
      <span>下载源</span>
      <div class="seg">
        <button v-for="[u, n] in S.boot.endpoints" :key="u" :class="{ on: endpoint === u }" :disabled="state === 'running'" @click="endpoint = u">{{ n }}</button>
      </div>
    </div>

    <div class="progress" :class="{ idle: state === 'idle' }">
      <div class="track"><div class="fill" :style="{ width: pct + '%' }" /></div>
      <div class="pinfo">
        <span>{{ prog.total ? `${mb(prog.done)} / ${mb(prog.total)} MB` : state === 'running' ? '正在连接…' : '' }}</span>
        <span v-if="state === 'running' && prog.speed">{{ (prog.speed / 1024 ** 2).toFixed(1) }} MB/s · 剩余{{ eta }}</span>
        <span v-else-if="state === 'paused'">已暂停，已下载的部分会保留</span>
      </div>
      <div v-if="state === 'error'" class="err"><Icon name="alert" :size="13" /> 下载失败：{{ error }}。可以换个下载源重试。</div>
    </div>

    <button class="btn primary go" @click="start">
      <Icon :name="state === 'running' ? 'pause' : 'download'" :size="16" />
      {{ state === 'running' ? '暂停' : state === 'idle' ? '下载并开始' : '继续下载' }}
    </button>
    <button class="quit" @click="call('quit')">退出</button>
  </div>
</template>

<style scoped>
.ob { position: relative; height: 100%; display: flex; flex-direction: column; align-items: center; justify-content: center; padding: 0 120px; text-align: center; animation: fade-in .2s; }
.drag { position: absolute; inset: 0 0 auto; height: 48px; }
h1 { margin: 20px 0 8px; font: 600 22px var(--font-display); }
p { margin: 0; color: var(--text-2); font-size: 13.5px; line-height: 1.75; }
.src { display: flex; align-items: center; gap: 14px; margin-top: 26px; font-size: 12.5px; color: var(--text-3); }
.seg { display: flex; gap: 2px; padding: 3px; border-radius: 8px; background: var(--raise); }
.seg button { height: 28px; padding: 0 12px; border: none; border-radius: 6px; background: transparent; color: var(--text-2); font-size: 12.5px; }
.seg button.on { background: var(--raise-2); color: var(--text); font-weight: 600; }
.progress { width: 100%; max-width: 480px; margin-top: 22px; }
.progress.idle { visibility: hidden; }
.track { height: 6px; border-radius: 3px; background: var(--raise-2); overflow: hidden; }
.fill { height: 100%; border-radius: 3px; background: var(--accent); transition: width .3s var(--ease); }
.pinfo { display: flex; justify-content: space-between; margin-top: 7px; font-size: 12px; color: var(--text-3); font-variant-numeric: tabular-nums; }
.err { display: flex; align-items: center; gap: 6px; margin-top: 8px; font-size: 12.5px; color: var(--err); }
.go { height: 38px; padding: 0 22px; margin-top: 20px; font-size: 14px; }
.quit { margin-top: 10px; border: none; background: none; color: var(--text-3); font-size: 12.5px; }
.quit:hover { color: var(--text); }
</style>
