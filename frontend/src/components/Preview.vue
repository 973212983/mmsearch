<script setup>
import { computed, nextTick, onMounted, onBeforeUnmount, ref, watch } from 'vue'
import Icon from './Icon.vue'
import Waveform from './Waveform.vue'
import { S, KINDS, currentHit, openHit, searchSimilar, copyFile, copyPath, openMenu } from '../store'
import { call } from '../api'
import { fmtSize as size, fmtTime as fmt } from '../format'

const h = currentHit
const info = ref(null)
const media = ref(null)
const playing = ref(false)
const cur = ref(0)
let infoTimer = 0

const seg = computed(() => (h.value?.start != null && h.value.kind !== 'text' ? `${fmt(h.value.start)} – ${fmt(h.value.end)}` : ''))
const segLen = computed(() => Math.max(0.1, (h.value?.end ?? 0) - (h.value?.start ?? 0)))
const progress = computed(() => (playing.value || cur.value > (h.value?.start ?? 0) + 0.05
  ? Math.min(1, Math.max(0, (cur.value - (h.value?.start ?? 0)) / segLen.value)) : -1))
const total = computed(() => Object.values(S.overview.stats || {}).reduce((a, b) => a + b, 0))

watch(h, (v) => {
  playing.value = false
  cur.value = v?.start ?? 0
  info.value = null
  clearTimeout(infoTimer)
  if (!v) return
  // 方向键快速翻动时不必每条都查文件信息
  infoTimer = setTimeout(async () => {
    const path = v.path
    const r = await call('file_info', path)
    if (h.value?.path === path) info.value = r
  }, 120)
  nextTick(() => { if (media.value && v.start != null) media.value.currentTime = v.start })
}, { immediate: true })

const esc = (s) => s.replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c])
const STOP = new Set('的了和与及或是在把被就都也而这那个些有为之其于从对')
/** 查询里的高亮词。中文没有空格，取每段连续汉字中所有 ≥2 字的子串（“会议纪要”能命中“会议”“纪要”），
 *  去掉以虚词开头或结尾的（“边的”“的日”）；英文和数字按词。 */
function queryTerms(q) {
  const out = new Set()
  for (const tok of q.split(/[\s,，.。、;；:：!！?？"“”'‘’()（）]+/)) {
    for (const run of tok.match(/\p{Script=Han}+/gu) || []) {
      const r = run.slice(0, 12)
      if (r.length === 1 && run === tok && !STOP.has(r)) out.add(r)
      for (let n = r.length; n >= 2; n--)
        for (let i = 0; i + n <= r.length; i++) {
          const g = r.slice(i, i + n)
          if (!STOP.has(g[0]) && !STOP.has(g[n - 1])) out.add(g)
        }
    }
    for (const w of tok.match(/[A-Za-z0-9_][\w.\-]+/g) || []) out.add(w)
  }
  return [...out].sort((a, b) => b.length - a.length)
}
const snippetHtml = computed(() => {
  const text = h.value?.snippet || ''
  const terms = S.query?.type === 'text' ? queryTerms(S.query.text) : []
  if (!terms.length) return esc(text)
  const re = new RegExp(terms.map((t) => t.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|'), 'gi')
  let html = '', last = 0
  for (const m of text.matchAll(re)) {
    html += esc(text.slice(last, m.index)) + `<mark>${esc(m[0])}</mark>`
    last = m.index + m[0].length
  }
  return html + esc(text.slice(last))
})

function togglePlay() {
  const m = media.value
  if (!m) return
  if (m.paused) {
    if (m.currentTime < (h.value.start ?? 0) - 0.5 || m.currentTime >= (h.value.end ?? Infinity)) m.currentTime = h.value.start ?? 0
    m.play()
  } else m.pause()
}
function seekWave(e) {
  const r = e.currentTarget.getBoundingClientRect()
  if (!media.value) return
  media.value.currentTime = (h.value.start ?? 0) + ((e.clientX - r.left) / r.width) * segLen.value
  if (media.value.paused) media.value.play()
}
function onMenu(e) {
  if (window.getSelection()?.toString()) return // 选中了片段文字：保留系统的复制菜单
  e.preventDefault()
  openMenu(h.value, e.clientX, e.clientY)
}
const pause = () => media.value?.pause()
onMounted(() => window.addEventListener('mm-hidden', pause))
onBeforeUnmount(() => window.removeEventListener('mm-hidden', pause))
defineExpose({ togglePlay })
</script>

<template>
  <section class="pv" aria-label="预览" @contextmenu="h && onMenu($event)">
    <template v-if="h">
      <div class="stage" :class="h.kind">
        <img v-if="h.kind === 'image'" :key="h.full" :src="h.full" alt="" class="full" />
        <template v-else-if="h.kind === 'video' || h.kind === 'video_audio'">
          <video v-if="h.playable" ref="media" :key="h.media" :src="h.media" :poster="h.poster" controls preload="metadata"
                 @play="playing = true" @pause="playing = false" @timeupdate="cur = $event.target.currentTime" />
          <img v-else :src="h.poster" alt="" class="full" />
        </template>
        <div v-else-if="h.kind === 'audio'" class="audio">
          <div class="wave" title="点击跳转" @click="seekWave"><Waveform :url="h.wave" :height="78" :progress="progress" /></div>
          <div class="ctrl">
            <button class="playbtn" :disabled="!h.playable" :aria-label="playing ? '暂停' : '播放'" title="播放 / 暂停（Ctrl+P）" @click="togglePlay">
              <Icon :name="playing ? 'pause' : 'play'" :size="15" />
            </button>
            <div class="ctime"><b>{{ fmt(cur) }}</b><span>命中片段 {{ seg }}</span></div>
          </div>
          <audio v-if="h.playable" ref="media" :key="h.media" :src="h.media" preload="metadata"
                 @play="playing = true" @pause="playing = false" @timeupdate="cur = $event.target.currentTime" />
        </div>
        <div v-else class="paper" :class="{ code: h.code }" v-html="snippetHtml" />
      </div>

      <div class="title">
        <h2 :title="h.name">{{ h.name }}</h2>
        <button class="loc" :title="'在资源管理器中显示\n' + h.path" @click="openHit(h, true)">{{ h.dir }}</button>
      </div>

      <div class="meta">
        <span class="chip"><i :style="{ background: KINDS[h.kind]?.color }" />{{ KINDS[h.kind]?.label }} · {{ (h.ext || '—').toUpperCase() }}</span>
        <span v-if="seg" class="chip">片段 {{ seg }}</span>
        <span class="chip" title="与查询的余弦相似度。不同类型文件的分数尺度不同（图片、音频普遍低于文档），只适合在同类结果之间比较">相似度 {{ h.score.toFixed(3) }}</span>
        <template v-if="info?.exists"><span class="chip">{{ size(info.size) }}</span><span class="chip">{{ new Date(info.mtime * 1000).toLocaleDateString('zh-CN') }}</span></template>
        <span v-else-if="info" class="chip warn">文件已移动或删除</span>
      </div>

      <div class="acts">
        <button class="btn primary" @click="openHit(h)">打开 <kbd>↵</kbd></button>
        <button class="btn" @click="openHit(h, true)">所在位置 <kbd>Ctrl ↵</kbd></button>
        <button class="icon-btn" title="复制路径（Ctrl+Shift+C）" aria-label="复制路径" @click="copyPath(h)"><Icon name="link" :size="16" /></button>
        <button class="icon-btn" title="复制文件（Ctrl+C）" aria-label="复制文件" @click="copyFile(h)"><Icon name="copy" :size="16" /></button>
        <button class="icon-btn" title="找相似（Ctrl+S）" aria-label="找相似" @click="searchSimilar(h)"><Icon name="sparkles" :size="16" /></button>
      </div>
    </template>

    <div v-else class="overview">
      <div class="ov-title">{{ total.toLocaleString() }} 个文件可以搜索</div>
      <div class="ov-grid">
        <div v-for="k in ['text', 'image', 'audio', 'video']" :key="k" class="ov-cell">
          <span class="ov-ic" :style="{ color: KINDS[k].color }"><Icon :name="KINDS[k].icon" :size="16" /></span>
          <b>{{ (S.overview.stats?.[k] || 0).toLocaleString() }}</b><span>{{ KINDS[k].label }}</span>
        </div>
      </div>
      <ul class="tips">
        <li><span>用一句话描述内容，中英文都行</span></li>
        <li><span>拖入或粘贴图片，找相似的图和视频</span><kbd>Ctrl V</kbd></li>
        <li><span>选一个文件作为查询</span><kbd>Ctrl O</kbd></li>
        <li><span>在类型之间切换</span><kbd>Tab</kbd></li>
      </ul>
    </div>
  </section>
</template>

<style scoped>
.pv { height: 100%; overflow: hidden; padding: 16px 18px 14px; display: flex; flex-direction: column; gap: 12px; animation: fade-in .12s; }
.stage { flex: 1 1 auto; min-height: 110px; max-height: 250px; border-radius: 10px; overflow: hidden; background: var(--raise); display: flex; align-items: center; justify-content: center; }
.stage.image, .stage.video, .stage.video_audio { background: #0d0e10; }
.full { width: 100%; height: 100%; object-fit: contain; display: block; }
video { width: 100%; height: 100%; display: block; background: #000; }
.stage.audio { background: none; }
.audio { width: 100%; align-self: stretch; display: flex; flex-direction: column; justify-content: center; padding: 16px 18px 14px; background: color-mix(in srgb, var(--c-audio) 10%, var(--raise)); border: 1px solid color-mix(in srgb, var(--c-audio) 20%, transparent); border-radius: 10px; }
.wave { cursor: pointer; }
.ctrl { display: flex; align-items: center; gap: 12px; margin-top: 14px; }
.playbtn { width: 38px; height: 38px; padding-left: 2px; border-radius: 19px; border: none; display: grid; place-items: center; background: var(--c-audio); color: #1d2024; }
.playbtn:disabled { opacity: .4; }
.ctime { display: flex; flex-direction: column; line-height: 1.3; }
.ctime b { font-size: 14.5px; font-variant-numeric: tabular-nums; }
.ctime span { font-size: 12px; color: var(--text-3); }
.paper { width: 100%; height: 100%; overflow: auto; tab-size: 4; padding: 14px 16px; white-space: pre-wrap; word-break: break-word; font-size: 12.5px; line-height: 1.7; color: var(--text-2); user-select: text; place-self: stretch; }
.paper.code { font-family: var(--mono); font-size: 11.5px; line-height: 1.55; white-space: pre; }
.paper :deep(mark) { background: var(--mark); color: var(--text); border-radius: 2px; padding: 0 1px; }
.title { min-width: 0; flex: none; }
h2 { margin: 0; font: 600 17px/1.35 var(--font-display); word-break: break-all; user-select: text; }
.loc { display: block; max-width: 100%; margin-top: 3px; padding: 0; border: none; background: none; color: var(--text-3); font-size: 12px; text-align: left; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.loc:hover { color: var(--accent-text); }
.meta { display: flex; flex-wrap: wrap; gap: 6px; flex: none; }
.chip { display: inline-flex; align-items: center; gap: 6px; height: 24px; padding: 0 9px; border-radius: 6px; background: var(--raise); font-size: 12px; color: var(--text-2); font-variant-numeric: tabular-nums; }
.chip i { width: 7px; height: 7px; border-radius: 4px; }
.chip.warn { color: var(--err); }
.acts { display: flex; align-items: center; gap: 6px; flex: none; }
.acts .btn kbd { background: transparent; color: inherit; opacity: .7; padding: 0; }
.acts .icon-btn { width: 32px; height: 32px; }

.overview { display: flex; flex-direction: column; gap: 16px; padding-top: 6px; }
.ov-title { font: 600 16px var(--font-display); }
.ov-grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 8px; }
.ov-cell { display: flex; align-items: center; gap: 10px; padding: 11px 12px; border-radius: 9px; background: var(--raise); }
.ov-ic { display: grid; place-items: center; width: 30px; height: 30px; border-radius: 7px; background: color-mix(in srgb, currentColor 16%, transparent); }
.ov-cell b { font-size: 15px; font-variant-numeric: tabular-nums; }
.ov-cell span:last-child { font-size: 12px; color: var(--text-3); }
.tips { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 9px; font-size: 12.5px; color: var(--text-2); }
.tips li { display: flex; align-items: center; justify-content: space-between; gap: 10px; }
</style>
