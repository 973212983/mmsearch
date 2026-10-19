import { reactive, computed } from 'vue'
import { call, on } from './api'
import { queryTerms } from './format'

export const KINDS = {
  text: { label: '文档', icon: 'file', color: 'var(--c-text)' },
  image: { label: '图片', icon: 'image', color: 'var(--c-image)' },
  page: { label: '扫描页', icon: 'file', color: 'var(--c-text)' },
  audio: { label: '音频', icon: 'music', color: 'var(--c-audio)' },
  video: { label: '视频', icon: 'video', color: 'var(--c-video)' },
  video_audio: { label: '视频 · 声音', icon: 'video', color: 'var(--c-video)' },
}
export const TABS = [
  { key: 'all', label: '全部' },
  { key: 'text', label: '文档' },
  { key: 'image', label: '图片' },
  { key: 'audio', label: '音频' },
  { key: 'video', label: '视频' },
]
const STRONG_MARGIN = 0.1 // 同一类型内，相关度低于（最高分 - 该值）的结果折叠为“更多结果”
const PER_GROUP = 3 // “全部”里每种类型展示的条数，更多的点分组标题进入该类型
// “试试这样搜”按索引里实际有的类型挑选
const EXAMPLES = {
  image: ['海边的日落', '白板上的手写笔记'],
  video: ['有人在讲话的视频'],
  audio: ['钢琴曲'],
  text: ['会议纪要', '读取 CSV 的代码'],
}

export const S = reactive({
  booted: false,
  boot: {},
  view: 'search', // search | index | settings
  model: { state: 'idle' },
  index: { running: false, done: 0, total: 0, current: '' },
  overview: { folders: [], stats: {}, items: 0, errors: 0 },
  settings: {},
  bgUrl: '', // 自定义背景图（由本地媒体服务提供，带随机 token）
  glassOk: true, // 系统是否真的支持磨砂（不支持时设置页提示一下）
  backdrop: null, // WebView2 透明回归时的虚拟桌面截图及其屏幕坐标
  missing: [],
  query: null, // { type: 'text'|'file'|'image'|'similar', text?, label, thumb?, kind? }
  pending: null, // 模型就绪前提交的查询 { query, fn, args }，就绪后自动执行
  results: null,
  info: null,
  tab: 'all',
  cursor: 0,
  expanded: false,
  searching: false,
  pinned: false,
  dragging: false,
  menu: null, // 右键菜单 { hit, x, y }
  toasts: [],
  recent: JSON.parse(localStorage.getItem('recent') || '[]'),
})

const examples = computed(() => {
  const st = S.overview.stats || {}
  const have = Object.keys(EXAMPLES).filter((k) => st[k]).sort((a, b) => st[b] - st[a])
  const kinds = have.length ? have : Object.keys(EXAMPLES)
  const out = []
  for (let i = 0; i < 2; i++) kinds.forEach((k) => EXAMPLES[k][i] && out.push(EXAMPLES[k][i]))
  return out.filter((t) => !S.recent.slice(0, 5).includes(t)).slice(0, 4)
})

/** 列表行：{type:'header',label,tab?,n?,more?,action?} | {type:'hit',hit} | {type:'more',n} | {type:'recent'|'example',text} */
export const rows = computed(() => {
  if (!S.results) {
    if (!S.overview.folders.length) return []
    const out = []
    if (S.recent.length) {
      out.push({ type: 'header', label: '最近搜索', action: 'clear-recent' })
      S.recent.slice(0, 5).forEach((t) => out.push({ type: 'recent', text: t }))
    }
    out.push({ type: 'header', label: '试试这样搜' })
    examples.value.forEach((t) => out.push({ type: 'example', text: t }))
    return out
  }
  const list = S.results[S.tab] || []
  if (!list.length) return []
  const out = []
  if (S.tab === 'all') {
    // 不同类型的相似度尺度不同（文本基线高，图片分数被压缩在窄区间），不能用同一个阈值筛选，
    // 否则图片和视频会被大量文档挤进“相关度较低”。所以按类型分区，每种类型展示自己最好的几个。
    const best = list[0]
    // 分区按“去掉最佳匹配后”各自最好的结果排序，否则最佳匹配所在的类型总排第一
    const groups = TABS.slice(1)
      .map((t) => [t.key, S.results[t.key] || []])
      .map(([k, l]) => [k, l, l.filter((h) => h.path !== best.path)])
      .filter(([, l]) => l.length)
    if (groups.length > 1) {
      out.push({ type: 'header', label: '最佳匹配' }, { type: 'hit', hit: best })
      groups.sort((a, b) => (b[2][0]?.score ?? -1) - (a[2][0]?.score ?? -1))
      for (const [k, l, rest] of groups) {
        const hs = rest.slice(0, PER_GROUP)
        if (!hs.length) continue
        out.push({ type: 'header', label: KINDS[k].label, tab: k, n: l.length, more: rest.length > hs.length })
        hs.forEach((h) => out.push({ type: 'hit', hit: h }))
      }
      return out
    }
  }
  // 只有过滤条件时按修改时间列出，没有分数，不折叠
  if (list[0].score == null) {
    list.forEach((h) => out.push({ type: 'hit', hit: h }))
    return out
  }
  // 同一类型内相似度可比：与最高分差距过大的折叠起来
  const cut = list[0].score - STRONG_MARGIN
  const strong = list.filter((h, i) => h.score >= cut || i < 3)
  const weak = list.slice(strong.length)
  strong.forEach((h) => out.push({ type: 'hit', hit: h }))
  if (weak.length) {
    if (S.expanded) {
      out.push({ type: 'header', label: '相关度较低' })
      weak.forEach((h) => out.push({ type: 'hit', hit: h }))
    } else out.push({ type: 'more', n: weak.length })
  }
  return out
})
export const selectable = computed(() => rows.value.map((r, i) => (r.type === 'header' ? -1 : i)).filter((i) => i >= 0))
export const current = computed(() => rows.value[selectable.value[S.cursor]] || null)
export const currentHit = computed(() => (current.value?.type === 'hit' ? current.value.hit : null))
/** 用来高亮和挑选片段的查询词（去掉了 ext: 这类过滤条件） */
export const highlightTerms = computed(() => (S.query?.type === 'text' ? queryTerms(S.info?.text ?? S.query.text) : []))
export const isMedia = (h) => !!h && h.kind !== 'text' && h.kind !== 'image' && h.kind !== 'page'

export function move(d) {
  const n = selectable.value.length
  if (n) S.cursor = Math.max(0, Math.min(n - 1, S.cursor + d))
}
export function selectRow(rowIndex) {
  const i = selectable.value.indexOf(rowIndex)
  if (i >= 0) S.cursor = i
}

let toastId = 0
export function toast(text, level = 'info') {
  const id = ++toastId
  S.toasts.push({ id, text, level })
  setTimeout(() => (S.toasts = S.toasts.filter((t) => t.id !== id)), level === 'error' ? 5000 : 2600)
}

function remember(text) {
  S.recent = [text, ...S.recent.filter((t) => t !== text)].slice(0, 8)
  localStorage.setItem('recent', JSON.stringify(S.recent))
}
/** 对结果有了实际动作（打开、复制、找相似、收起面板）才记入“最近搜索”；边打字边搜出来的半截词不记 */
function commit() {
  if (S.query?.type === 'text' && S.results) remember(S.query.text)
}
export function clearRecent() {
  S.recent = []
  localStorage.removeItem('recent')
}

let seq = 0
async function run(query, fn, ...args) {
  const my = ++seq
  S.query = query
  S.view = 'search'
  S.menu = null
  if (fn !== 'search_similar' && S.model.state !== 'ready') {
    // 模型还在加载（或启动失败）：记下查询，就绪后自动搜索，而不是弹错误
    S.pending = { query, fn, args }
    S.results = null
    S.searching = false
    return
  }
  S.pending = null
  S.searching = true
  const r = await call(fn, ...args)
  if (my !== seq) return
  S.searching = false
  if (!r || r.error) return toast(r?.error || '搜索失败', 'error')
  if (r.query?.thumb) S.query = { ...query, thumb: r.query.thumb }
  S.results = r.results
  S.info = { enc: r.enc_ms, search: r.search_ms, total: r.total, filters: r.filters || [], text: r.text }
  S.expanded = false
  S.cursor = 0
  if (S.tab !== 'all' && !(r.results[S.tab] || []).length) S.tab = 'all'
}

export const searchText = (text) => {
  text = text.trim()
  if (!text) return clearQuery()
  if (S.query?.type === 'text' && S.query.text === text && S.results) return
  return run({ type: 'text', text, label: text }, 'search_text', text)
}
export const searchFile = (path, name, kind) => run({ type: 'file', label: name, kind, path }, 'search_file', path)
export const searchImageData = (dataUrl, label) =>
  run({ type: 'image', label, thumb: dataUrl, kind: 'image' }, 'search_image_data', dataUrl)
export function searchSimilar(hit) {
  commit()
  return run({ type: 'similar', label: `与「${hit.name}」相似`, thumb: hit.thumb, kind: hit.kind }, 'search_similar', hit.id)
}

/** 以图（文件、粘贴、找相似）搜索时再输入文字：在原图的基础上补充描述（组合查询），图片查询保留在搜索框左侧 */
export function searchRefine(text) {
  if (!S.query || S.query.type === 'text') return searchText(text)
  text = text.trim()
  if ((S.query.refine || '') === text && S.results) return
  return run({ ...S.query, refine: text }, 'search_refine', text)
}

export async function pickFile() {
  const f = await call('pick_query_file')
  if (f) searchFile(f.path, f.name, f.kind)
}

export function clearQuery() {
  seq++
  S.query = null
  S.pending = null
  S.results = null
  S.info = null
  S.cursor = 0
  S.searching = false
  S.tab = 'all'
}

export function setTab(key) {
  if (key !== 'all' && S.results && !(S.results[key] || []).length) return
  S.tab = key
  S.expanded = false
  S.cursor = 0
}
export function cycleTab(d = 1) {
  const keys = TABS.map((t) => t.key).filter((k) => k === 'all' || (S.results?.[k] || []).length)
  setTab(keys[(keys.indexOf(S.tab) + d + keys.length) % keys.length])
}

export function openHit(h, reveal = false) {
  if (!h) return
  commit()
  if (reveal) call('reveal', h.path)
  else call('open', h.path, h.start, h.kind)
  if (S.settings.autohide && !S.pinned) call('hide')
}
export function copyFile(h) {
  commit()
  return call('copy_file', h.path)
}
export function copyPath(h) {
  commit()
  return navigator.clipboard.writeText(h.path).then(() => toast('已复制路径'))
}

export const openMenu = (hit, x, y) => (S.menu = { hit, x, y })
export const closeMenu = () => (S.menu = null)

/** 回车：命中项打开，“更多”展开，最近/示例执行搜索 */
export function activate(row, reveal = false) {
  if (!row) return
  if (row.type === 'hit') openHit(row.hit, reveal)
  else if (row.type === 'more') S.expanded = true
  else if (row.type === 'recent' || row.type === 'example') searchText(row.text)
}

export function togglePin() {
  S.pinned = !S.pinned
  call('set_pinned', S.pinned)
}

/** 设置即改即生效：只把改动的键发给后端，返回值为准（例如快捷键被占用时会退回原值） */
export async function saveSettings(patch) {
  Object.assign(S.settings, patch)
  applyBackground()
  const r = await call('save_settings', patch)
  if (r) applySettingsPayload(r)
  window.mm?.applyTheme?.() // 主题/磨砂会同时改 CSS 变量和原生窗口
  return r
}

/** 背景图层：把背景图/模糊/暗度/磨砂开关写到 :root（渲染全在 style.css 里） */
export function applyBackground() {
  const s = S.settings || {}
  const root = document.documentElement
  const px = (v, d) => `${Number(v ?? d)}px`
  const backdrop = !S.bgUrl && s.glass !== false ? S.backdrop : null
  const image = S.bgUrl || backdrop?.url || ''
  root.dataset.glass = s.glass === false ? 'off' : 'on'
  root.dataset.image = image ? 'on' : 'off'
  root.dataset.backdrop = backdrop?.url ? 'on' : 'off'
  root.style.setProperty('--bg-image', image ? `url("${image}")` : 'none')
  root.style.setProperty('--bg-blur', px(s.bg_blur, 18))
  root.style.setProperty('--bg-dim', String(Number(s.bg_dim ?? 42) / 100))
  if (backdrop?.url) {
    root.style.setProperty('--backdrop-width', `${backdrop.width}px`)
    root.style.setProperty('--backdrop-height', `${backdrop.height}px`)
    syncBackdropPosition(true)
  }
}

const BACKDROP_PAD = 48
let backdropX = NaN
let backdropY = NaN

/** 拖动原生窗口时，把虚拟桌面截图反向移动，保持截图内容与真实屏幕坐标对齐。 */
export function syncBackdropPosition(force = false) {
  const b = S.backdrop
  if (!b?.url || S.bgUrl || S.settings.glass === false) return
  const screenX = Math.round(window.screenX)
  const screenY = Math.round(window.screenY)
  if (!force && screenX === backdropX && screenY === backdropY) return
  backdropX = screenX
  backdropY = screenY
  const root = document.documentElement
  root.style.setProperty('--backdrop-x', `${b.left - screenX + BACKDROP_PAD}px`)
  root.style.setProperty('--backdrop-y', `${b.top - screenY + BACKDROP_PAD}px`)
}

/** save_settings / pick_background / clear_background 的回写（bg_url 和 autostart 不是普通设置项） */
function applySettingsPayload(r) {
  const { autostart, bg_url, ...settings } = r
  S.settings = settings
  if (autostart !== undefined) S.boot.autostart = autostart
  if (bg_url !== undefined) S.bgUrl = bg_url
}

/** 选一张图当背景：后端会统一转成 JPEG 存进数据目录 */
export async function pickBackground() {
  const r = await call('pick_background')
  if (!r || r.cancel) return null
  if (r.error) return toast(r.error, 'error')
  applySettingsPayload(r)
  window.mm?.applyTheme?.()
  toast('背景图片已更新')
  return r
}

export async function clearBackground() {
  const r = await call('clear_background')
  if (!r || r.error) return toast(r?.error || '取消失败', 'error')
  applySettingsPayload(r)
  window.mm?.applyTheme?.()
  toast('已恢复默认背景')
}

export async function bootstrap() {
  const b = await call('boot')
  S.boot = b
  S.settings = b.settings
  S.bgUrl = b.bg_url || ''
  S.glassOk = b.glass_ok !== false
  S.model = b.model
  S.index = b.index
  S.missing = b.missing
  S.overview = { folders: b.folders, stats: b.stats, items: b.items, errors: b.errors }
  S.booted = true
  window.mm?.applyTheme()
}

on('model', (m) => {
  S.model = m
  if (m.state === 'ready' && S.pending) {
    const { query, fn, args } = S.pending
    run(query, fn, ...args)
  }
})

on('backdrop', (data) => {
  S.backdrop = data?.url ? data : null
  applyBackground()
})
on('index', (i) => (S.index = i))
on('overview', (o) => (S.overview = o))
on('toast', (t) => toast(t.text, t.level))
on('download_done', (d) => (S.missing = d.missing))
on('shown', (d) => {
  if (d?.view) S.view = d.view
  window.dispatchEvent(new CustomEvent('mm-shown'))
})
on('hidden', () => {
  commit()
  S.menu = null
  window.dispatchEvent(new CustomEvent('mm-hidden'))
})
on('drop_file', (f) => {
  S.dragging = false
  if (!f.kind) return toast(`不支持用 .${f.name.split('.').pop()} 文件搜索`, 'error')
  searchFile(f.path, f.name, f.kind)
})
on('index_done', async (d) => {
  if (d.error) toast(`索引中断：${d.error}`, 'error')
  else if (d.failed) toast(`索引完成，${d.failed} 个文件处理失败`)
  if (S.results) {
    const r = await call('rerun')
    if (r && !r.error) S.results = r.results
  }
})
