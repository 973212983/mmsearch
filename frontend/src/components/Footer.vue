<script setup>
import { computed } from 'vue'
import Icon from './Icon.vue'
import { S, currentHit, isMedia, togglePin } from '../store'

const pct = computed(() => (S.index.total ? S.index.done / S.index.total : 0))
const dev = computed(() => ({ vulkan: 'GPU', cpu: 'CPU' })[S.model.device] || '')
const total = computed(() => Object.values(S.overview.stats || {}).reduce((a, b) => a + b, 0))
const playable = computed(() => isMedia(currentHit.value) && currentHit.value.playable)
const go = (v) => (S.view = S.view === v ? 'search' : v)
</script>

<template>
  <footer class="ft">
    <div class="pywebview-drag-region drag-layer" aria-hidden="true"></div>
    <button class="status" :title="S.model.state === 'error' ? S.model.error : '索引管理（Ctrl+I）'" @click="go(S.model.state === 'error' ? 'settings' : 'index')">
      <template v-if="S.model.state === 'loading' || S.model.state === 'idle'"><span class="spinner" /> 正在加载模型</template>
      <template v-else-if="S.model.state === 'error'"><span class="dot err" /> 模型启动失败</template>
      <template v-else-if="S.index.running && S.index.paused"><span class="dot pause" /> 索引已暂停（{{ S.index.paused }}） {{ S.index.done }}/{{ S.index.total || '…' }}</template>
      <template v-else-if="S.index.running">
        <svg viewBox="0 0 20 20" width="14" height="14" aria-hidden="true">
          <circle cx="10" cy="10" r="8" fill="none" stroke="var(--line-2)" stroke-width="3.5" />
          <circle cx="10" cy="10" r="8" fill="none" stroke="var(--accent)" stroke-width="3.5" stroke-linecap="round"
                  :stroke-dasharray="`${Math.max(pct, .02) * 50.3} 50.3`" transform="rotate(-90 10 10)" />
        </svg>
        正在索引 {{ S.index.done }}/{{ S.index.total || '…' }}
      </template>
      <template v-else><span class="dot ok" /> {{ dev }} 就绪 · {{ total.toLocaleString() }} 个文件</template>
    </button>

    <span class="hints">
      <template v-if="S.view === 'search' && S.results">
        <span><kbd>↑↓</kbd> 选择</span><span><kbd>↵</kbd> 打开</span>
        <span v-if="playable"><kbd>Ctrl P</kbd> 播放</span>
        <span v-else><kbd>Ctrl S</kbd> 找相似</span>
        <span><kbd>Tab</kbd> 类型</span>
      </template>
      <template v-else-if="S.view === 'search' && S.overview.folders.length"><span><kbd>Ctrl V</kbd> 以图搜</span><span><kbd>Ctrl O</kbd> 以文件搜</span></template>
      <span><kbd>Esc</kbd> {{ S.view !== 'search' ? '返回' : S.query ? '清除' : '收起' }}</span>
    </span>

    <span class="tools">
      <button class="icon-btn" :class="{ on: S.pinned }" :title="S.pinned ? '取消固定（失去焦点时自动收起）' : '固定面板：切到其他窗口时不收起，方便拖入文件'" aria-label="固定面板" @click="togglePin">
        <Icon name="pin" :size="15" />
      </button>
      <button class="icon-btn" :class="{ on: S.view === 'index' }" title="索引管理（Ctrl+I）" aria-label="索引管理" @click="go('index')"><Icon name="database" :size="15" /></button>
      <button class="icon-btn" :class="{ on: S.view === 'settings' }" title="设置（Ctrl+,）" aria-label="设置" @click="go('settings')"><Icon name="settings" :size="15" /></button>
    </span>
  </footer>
</template>

<style scoped>
.ft { position: relative; height: 40px; flex: none; display: flex; align-items: center; gap: 14px; padding: 0 8px 0 10px; border-top: 1px solid var(--line); background: color-mix(in srgb, var(--raise) 55%, var(--bg)); font-size: 12px; color: var(--text-3); }
/* 透明拖拽层：点页脚空白处拖动窗口；交互元素抬高到层之上 */
.drag-layer { position: absolute; inset: 0; }
.status, .icon-btn { position: relative; }
.tools { position: relative; display: flex; gap: 2px; padding-left: 8px; border-left: 1px solid var(--line); }
.status { display: flex; align-items: center; gap: 8px; height: 28px; padding: 0 8px; border: none; border-radius: 6px; background: transparent; color: var(--text-2); font-size: 12px; font-variant-numeric: tabular-nums; }
.status:hover { background: var(--hover); }
.dot { width: 7px; height: 7px; border-radius: 4px; }
.dot.ok { background: var(--ok); }
.dot.err { background: var(--err); }
.dot.pause { background: var(--text-3); }
.hints { margin-left: auto; display: flex; gap: 14px; white-space: nowrap; }
.hints span { display: flex; align-items: center; gap: 5px; }
</style>
