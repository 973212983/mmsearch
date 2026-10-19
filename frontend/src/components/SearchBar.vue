<script setup>
import { ref, watch } from 'vue'
import Icon from './Icon.vue'
import { S, KINDS, current, searchText, searchRefine, pickFile, clearQuery, move, activate, cycleTab } from '../store'
import { call } from '../api'

const text = ref('')
const input = ref(null)
let timer = 0

watch(() => S.query, (q) => {
  if (!q) text.value = ''
  else if (q.type === 'text' && q.text !== text.value.trim()) text.value = q.text
  else if (q.type !== 'text' && (q.refine || '') !== text.value.trim()) text.value = q.refine || ''
})

function onInput() {
  clearTimeout(timer)
  if (S.view !== 'search') S.view = 'search'
  if (hasBase()) return (timer = setTimeout(() => searchRefine(text.value), 220)) // 补充描述；清空文字回到原来的以图搜索
  if (!text.value.trim()) return clearQuery()
  timer = setTimeout(() => searchText(text.value), 220)
}

function onKey(e) {
  const ctrl = e.ctrlKey
  switch (e.key) {
    case 'ArrowDown': e.preventDefault(); return move(1)
    case 'ArrowUp': e.preventDefault(); return move(-1)
    case 'PageDown': e.preventDefault(); return move(6)
    case 'PageUp': e.preventDefault(); return move(-6)
    case 'Tab': e.preventDefault(); if (S.results) cycleTab(e.shiftKey ? -1 : 1); return
    case 'Enter':
      e.preventDefault()
      clearTimeout(timer)
      if (hasBase() && (S.query.refine || '') !== text.value.trim()) return searchRefine(text.value)
      if (!hasBase() && text.value.trim() && S.query?.text !== text.value.trim()) return searchText(text.value)
      return activate(current.value, ctrl)
    case 'Escape':
      e.preventDefault()
      if (S.view !== 'search') { S.view = 'search'; return }
      if (text.value || S.query) { text.value = ''; return clearQuery() }
      return call('hide')
    case 'Backspace':
      if (!text.value && S.query && S.query.type !== 'text') clearQuery()
  }
}

const hasBase = () => !!S.query && S.query.type !== 'text'

function focus() {
  input.value?.focus()
  input.value?.select()
}
defineExpose({ focus })
</script>

<template>
  <div class="bar">
    <div class="pywebview-drag-region drag-layer" aria-hidden="true"></div>
    <Icon name="search" :size="20" :stroke="2" class="lead" />
    <div v-if="S.query && S.query.type !== 'text'" class="chip">
      <img v-if="S.query.thumb" :src="S.query.thumb" alt="" />
      <span v-else class="chip-ic" :style="{ color: KINDS[S.query.kind]?.color }"><Icon :name="KINDS[S.query.kind]?.icon || 'file'" :size="13" /></span>
      <span class="chip-name">{{ S.query.label }}</span>
      <button class="chip-x" aria-label="清除查询" @click="clearQuery(); focus()"><Icon name="x" :size="12" :stroke="2.4" /></button>
    </div>
    <label for="q" class="sr">搜索</label>
    <input id="q" ref="input" v-model="text" spellcheck="false" autocomplete="off"
           :placeholder="S.query && S.query.type !== 'text' ? '补充描述，如「夜景」；删除左侧图片可重新搜索' : '描述你要找的内容…'"
           @input="onInput" @keydown="onKey" />
    <span v-for="f in S.info?.filters || []" :key="f" class="filter" title="过滤条件：从查询里识别出来的时间、类型或文件夹">{{ f }}</span>
    <div v-if="S.searching || (S.pending && S.model.state !== 'error')" class="spinner" />
    <span v-else-if="S.info && !S.info.filters?.length" class="ms">编码 {{ S.info.enc }} ms · 检索 {{ S.info.search }} ms</span>
    <button class="icon-btn" title="以文件搜索（Ctrl+O）&#10;也可以拖入文件，或 Ctrl+V 粘贴截图" aria-label="以文件搜索" @click="pickFile">
      <Icon name="attach" :size="17" />
    </button>
  </div>
</template>

<style scoped>
.bar { position: relative; height: 62px; flex: none; display: flex; align-items: center; gap: 12px; padding: 0 14px 0 20px; border-bottom: 1px solid var(--line); }
/* 透明拖拽层：铺满整行，点空白处即可拖动窗口；交互元素抬高到层之上 */
.drag-layer { position: absolute; inset: 0; }
input, .chip, .icon-btn { position: relative; }
.lead { color: var(--accent); }
input { flex: 1; min-width: 0; height: 100%; border: none; outline: none; background: transparent; font: 400 19px var(--font); }
input::placeholder { color: var(--text-3); }
.filter { position: relative; flex: none; font-size: 12px; padding: 3px 8px; border-radius: 6px; background: var(--sel); color: var(--accent-text); white-space: nowrap; }
.ms { font-size: 12px; color: var(--text-3); font-variant-numeric: tabular-nums; white-space: nowrap; }
.chip { display: flex; align-items: center; gap: 7px; height: 30px; padding: 0 3px 0 3px; max-width: 280px; border-radius: 7px; background: var(--sel); color: var(--accent-text); animation: rise .15s var(--ease); }
.chip img { width: 24px; height: 24px; border-radius: 5px; object-fit: cover; }
.chip-ic { display: grid; place-items: center; width: 24px; height: 24px; border-radius: 5px; background: var(--raise-2); }
.chip-name { font-size: 13px; font-weight: 500; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.chip-x { display: grid; place-items: center; width: 22px; height: 22px; border: none; border-radius: 5px; background: transparent; color: inherit; }
.chip-x:hover { background: var(--sel); }
.sr { position: absolute; left: -9999px; }
</style>
