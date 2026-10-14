<script setup>
import { computed, nextTick, onMounted, ref } from 'vue'
import Icon from './components/Icon.vue'
import SearchBar from './components/SearchBar.vue'
import ResultList from './components/ResultList.vue'
import Preview from './components/Preview.vue'
import Footer from './components/Footer.vue'
import IndexView from './components/IndexView.vue'
import SettingsView from './components/SettingsView.vue'
import Onboarding from './components/Onboarding.vue'
import Welcome from './components/Welcome.vue'
import ContextMenu from './components/ContextMenu.vue'
import { S, TABS, rows, currentHit, bootstrap, setTab, pickFile, searchImageData, searchSimilar, copyFile, copyPath, openMenu } from './store'
import { call } from './api'

const bar = ref(null)
const preview = ref(null)
let dragDepth = 0

const counts = computed(() => Object.fromEntries(TABS.map((t) => [t.key, S.results?.[t.key]?.length || 0])))
const focusBar = () => nextTick(() => bar.value?.focus())
const modelError = computed(() => (S.model.error || '').split('\n')[0])

// 失焦自动收起只在搜索时生效：首次设置、索引管理和设置页像普通窗口一样停留，
// 方便切到资源管理器把文件夹拖进来，或者对照着其他窗口改设置
const stayOpen = () => S.view !== 'search' || !S.overview.folders.length || S.missing.length > 0

function menuForSelection() {
  const h = currentHit.value
  const r = document.querySelector('.row.sel')?.getBoundingClientRect()
  if (h && r) openMenu(h, r.left + 56, r.bottom - 6)
}

function onKey(e) {
  const ctrl = e.ctrlKey
  const k = e.key.toLowerCase()
  if (S.missing.length) return
  if (ctrl && k === 'o') { e.preventDefault(); return pickFile() }
  if (ctrl && k === ',') { e.preventDefault(); S.view = S.view === 'settings' ? 'search' : 'settings'; return }
  if (ctrl && k === 'i') { e.preventDefault(); S.view = S.view === 'index' ? 'search' : 'index'; return }
  if (ctrl && (k === 'l' || k === 'k' || k === 'f')) { e.preventDefault(); S.view = 'search'; return focusBar() }
  if (ctrl && /^[1-5]$/.test(e.key) && S.results) { e.preventDefault(); return setTab(TABS[+e.key - 1].key) }
  if (e.key === 'F5') { e.preventDefault(); return call('start_index') }
  if (e.key === 'Escape' && S.view !== 'search' && e.target.tagName !== 'INPUT') { S.view = 'search'; return focusBar() }
  const h = currentHit.value
  if (S.view !== 'search' || !h) return
  if (e.key === 'ContextMenu' || (e.shiftKey && e.key === 'F10')) { e.preventDefault(); return menuForSelection() }
  if (ctrl && k === 'p') { e.preventDefault(); return preview.value?.togglePlay() }
  if (ctrl && k === 'c' && !window.getSelection()?.toString() && (e.target.tagName !== 'INPUT' || e.target.selectionStart === e.target.selectionEnd)) {
    e.preventDefault()
    return e.shiftKey ? copyPath(h) : copyFile(h)
  }
  if (ctrl && k === 's') { e.preventDefault(); return searchSimilar(h) }
}

function onPaste(e) {
  const item = [...(e.clipboardData?.items || [])].find((i) => i.type.startsWith('image/'))
  if (!item || S.missing.length) return
  e.preventDefault()
  const r = new FileReader()
  r.onload = () => searchImageData(r.result, '剪贴板图片')
  r.readAsDataURL(item.getAsFile())
}

const hasFiles = (e) => [...(e.dataTransfer?.types || [])].includes('Files')
function onDragEnter(e) { if (hasFiles(e)) { dragDepth++; S.dragging = true } }
function onDragLeave() { if (--dragDepth <= 0) { dragDepth = 0; S.dragging = false } }
function onDragOver(e) { if (hasFiles(e)) e.preventDefault() }
function onDrop(e) { e.preventDefault(); dragDepth = 0; setTimeout(() => (S.dragging = false), 200) } // 完整路径由 Python 端处理

onMounted(async () => {
  await bootstrap()
  window.addEventListener('keydown', onKey)
  window.addEventListener('paste', onPaste)
  window.addEventListener('dragenter', onDragEnter)
  window.addEventListener('dragleave', onDragLeave)
  window.addEventListener('dragover', onDragOver)
  window.addEventListener('drop', onDrop)
  window.addEventListener('blur', () => { S.menu = null; if (!stayOpen()) call('on_blur') })
  window.addEventListener('mm-shown', focusBar)
  focusBar()
})
</script>

<template>
  <div v-if="S.booted" class="panel">
    <Onboarding v-if="S.missing.length" />
    <template v-else>
      <SearchBar ref="bar" />

      <nav v-if="S.view === 'search' && S.results" class="tabs pywebview-drag-region" aria-label="类型">
        <button v-for="(t, i) in TABS" :key="t.key" class="tab" :class="{ on: S.tab === t.key }"
                :disabled="t.key !== 'all' && !counts[t.key]" :title="`Ctrl+${i + 1}`" @click="setTab(t.key); focusBar()">
          {{ t.label }}<span>{{ counts[t.key] >= 120 ? '120+' : counts[t.key] }}</span>
        </button>
        <span v-if="S.index.running" class="tab-note" title="索引完成后结果会自动刷新"><span class="spinner" /> 索引尚未完成，结果可能不全</span>
      </nav>

      <main class="body">
        <template v-if="S.view === 'search'">
          <Welcome v-if="!S.overview.folders.length" />
          <template v-else>
            <div class="left">
              <div v-if="S.pending" class="none">
                <template v-if="S.model.state === 'error'">
                  <Icon name="alert" :size="22" class="err" />
                  <b>模型没有启动，暂时无法搜索</b>
                  <span class="why">{{ modelError }}</span>
                  <button class="btn" @click="S.view = 'settings'">查看原因</button>
                </template>
                <template v-else>
                  <span class="spinner lg" />
                  <b>正在加载模型…</b>
                  <span>就绪后会自动搜索「{{ S.pending.query.label }}」</span>
                </template>
              </div>
              <ResultList v-else-if="rows.length" />
              <div v-else-if="!S.searching" class="none">
                <Icon name="search" :size="22" />
                <b>{{ S.info?.total ? '没有找到相关结果' : '索引还是空的' }}</b>
                <span>{{ S.info?.total ? '换个说法试试，描述得具体一些通常更准' : '索引完成一部分后就能搜到结果' }}</span>
              </div>
            </div>
            <Preview ref="preview" class="right" />
          </template>
        </template>
        <IndexView v-else-if="S.view === 'index'" class="full" />
        <SettingsView v-else class="full" />
      </main>

      <Footer />
    </template>

    <ContextMenu />

    <div v-if="S.dragging" class="drop">
      <Icon name="upload" :size="28" />
      <b>松开以搜索相似内容</b>
      <span>拖入文件夹则添加到索引</span>
    </div>

    <TransitionGroup name="toast" tag="div" class="toasts">
      <div v-for="t in S.toasts" :key="t.id" class="toast" :class="t.level">
        <Icon :name="t.level === 'error' ? 'alert' : 'check'" :size="14" /> {{ t.text }}
      </div>
    </TransitionGroup>
  </div>
</template>

<style scoped>
.panel { position: relative; height: 100%; display: flex; flex-direction: column; border: 1px solid var(--line-2); }
.tabs { height: 40px; flex: none; display: flex; align-items: center; gap: 2px; padding: 0 12px; border-bottom: 1px solid var(--line); }
.tab { display: flex; align-items: center; gap: 6px; height: 28px; padding: 0 10px; border: none; border-radius: 6px; background: transparent; color: var(--text-2); font-size: 13px; }
.tab span { font-size: 11.5px; color: var(--text-3); font-variant-numeric: tabular-nums; }
.tab:hover:not(:disabled) { background: var(--hover); }
.tab:disabled { opacity: .35; cursor: default; }
.tab.on { background: var(--sel); color: var(--accent-text); }
.tab.on span { color: inherit; opacity: .75; }
.tab-note { margin-left: auto; display: flex; align-items: center; gap: 7px; font-size: 11.5px; color: var(--text-3); }
.tab-note .spinner { width: 11px; height: 11px; border-width: 1.5px; }
.body { flex: 1; min-height: 0; display: flex; }
.left { width: 430px; flex: none; border-right: 1px solid var(--line); min-height: 0; }
.right { flex: 1; min-width: 0; }
.full { flex: 1; }
.none { height: 100%; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 8px; color: var(--text-3); text-align: center; padding: 0 30px; }
.none b { color: var(--text); font-size: 14.5px; font-weight: 600; margin-top: 6px; }
.none span { font-size: 12.5px; }
.none .err { color: var(--err); }
.none .why { max-width: 100%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.none .btn { margin-top: 6px; }
.spinner.lg { width: 20px; height: 20px; border-width: 2.5px; }
.drop { position: absolute; inset: 8px; z-index: 20; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 8px; border: 2px dashed var(--accent); border-radius: 12px; background: color-mix(in srgb, var(--bg) 88%, var(--accent)); color: var(--accent-text); animation: fade-in .12s; }
.drop b { font-size: 17px; color: var(--text); }
.drop span { font-size: 12.5px; color: var(--text-3); }
.toasts { position: absolute; left: 50%; bottom: 52px; transform: translateX(-50%); z-index: 30; display: flex; flex-direction: column; align-items: center; gap: 6px; pointer-events: none; }
.toast { display: flex; align-items: center; gap: 8px; padding: 8px 14px; border-radius: 8px; background: var(--raise-2); color: var(--text); font-size: 12.5px; box-shadow: var(--shadow); max-width: 600px; }
.toast .mm-ic { color: var(--ok); }
.toast.error .mm-ic { color: var(--err); }
.toast-enter-active, .toast-leave-active { transition: all .2s var(--ease); }
.toast-enter-from, .toast-leave-to { opacity: 0; transform: translateY(6px); }
</style>
