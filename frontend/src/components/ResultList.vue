<script setup>
import { nextTick, ref, watch } from 'vue'
import Icon from './Icon.vue'
import { S, KINDS, rows, selectable, highlightTerms, setTab, selectRow, activate, clearRecent, openMenu } from '../store'
import { bestLine, fmtDate, fmtTime } from '../format'

const box = ref(null)
const sub = (h) => {
  if (h.kind === 'text') {
    // 文档显示片段里和查询最相关的一行（没有关键词命中时是开头），比文件夹名更有信息量
    return `${(h.ext || 'txt').toUpperCase()} · ${bestLine(h.snippet, highlightTerms.value)}`
  }
  const parts = [KINDS[h.kind]?.label]
  if (h.kind === 'page') parts.push(`${(h.ext || '').toUpperCase()} 第 ${h.start} 页`)
  else if (h.start != null && h.kind !== 'text') parts.push(`${fmtTime(h.start)}–${fmtTime(h.end)}`)
  parts.push(h.folder || h.dir)
  return parts.join(' · ')
}
const isSel = (i) => selectable.value[S.cursor] === i
const hover = (i) => !S.menu && !isSel(i) && selectRow(i) // 右键菜单打开时不跟随鼠标改选中项

function onMenu(e, i, hit) {
  selectRow(i)
  openMenu(hit, e.clientX, e.clientY)
}

watch(() => [S.cursor, rows.value], async () => {
  await nextTick()
  box.value?.querySelector('.row.sel')?.scrollIntoView({ block: 'nearest' })
})
</script>

<template>
  <div ref="box" class="list" role="listbox" aria-label="搜索结果">
    <template v-for="(r, i) in rows" :key="i">
      <div v-if="r.type === 'header'" class="head">
        <span>{{ r.label }}</span>
        <button v-if="r.tab && r.more" class="link" @click="setTab(r.tab)">全部 {{ r.n >= 120 ? '120+' : r.n }}<Icon name="chevron" :size="12" class="go" /></button>
        <button v-else-if="r.action === 'clear-recent'" class="link" @click="clearRecent">清除</button>
      </div>

      <div v-else-if="r.type === 'hit'" class="row" :class="{ sel: isSel(i) }" role="option" :aria-selected="isSel(i)"
           @mousemove="hover(i)" @click="selectRow(i)" @dblclick="activate(r)" @contextmenu.prevent="onMenu($event, i, r.hit)">
        <span class="ic" :class="r.hit.kind" :style="{ '--k': KINDS[r.hit.kind]?.color }">
          <img v-if="r.hit.icon" :src="r.hit.icon" alt="" loading="lazy" />
          <b v-else>{{ (r.hit.ext || '').slice(0, 4).toUpperCase() }}</b>
          <i v-if="r.hit.kind === 'video' || r.hit.kind === 'video_audio'" class="play"><Icon name="play" :size="9" /></i>
        </span>
        <span class="txt">
          <span class="name">{{ r.hit.name }}</span>
          <span class="sub">{{ sub(r.hit) }}</span>
        </span>
        <span class="date" :title="r.hit.mtime ? '修改于 ' + new Date(r.hit.mtime * 1000).toLocaleString('zh-CN') : ''">{{ fmtDate(r.hit.mtime) }}</span>
      </div>

      <div v-else-if="r.type === 'more'" class="row more" :class="{ sel: isSel(i) }" @mousemove="hover(i)" @click="activate(r)">
        <span class="ic plain"><Icon name="chevron" :size="16" /></span>
        <span class="txt"><span class="name">显示 {{ r.n }} 个相关度较低的结果</span></span>
      </div>

      <div v-else class="row" :class="{ sel: isSel(i) }" @mousemove="hover(i)" @click="activate(r)">
        <span class="ic plain"><Icon :name="r.type === 'recent' ? 'clock' : 'search'" :size="16" /></span>
        <span class="txt"><span class="name">{{ r.text }}</span></span>
        <kbd v-if="isSel(i)">↵</kbd>
      </div>
    </template>
  </div>
</template>

<style scoped>
.list { height: 100%; overflow-y: auto; padding: 6px 8px 10px; }
.head { display: flex; align-items: center; justify-content: space-between; padding: 12px 10px 5px; font-size: 11.5px; font-weight: 600; color: var(--text-3); letter-spacing: .3px; }
.link { display: flex; align-items: center; gap: 2px; border: none; background: none; padding: 0 4px; font-size: 11.5px; color: var(--text-3); border-radius: 4px; }
.link:hover { color: var(--accent-text); }
.link .go { transform: rotate(-90deg); }
.row { display: flex; align-items: center; gap: 12px; height: 52px; padding: 0 10px; border-radius: 8px; }
.row.sel { background: var(--sel); }
.ic { position: relative; width: 38px; height: 38px; flex: none; border-radius: 7px; overflow: hidden; display: grid; place-items: center; background: color-mix(in srgb, var(--k) 22%, var(--raise)); color: var(--k); }
.ic img { width: 100%; height: 100%; object-fit: cover; display: block; }
.ic b { font-size: 10px; font-weight: 700; letter-spacing: .3px; }
.ic.plain { width: 28px; height: 28px; margin: 0 5px; background: none; color: var(--text-3); }
.row:has(.ic.plain) { height: 40px; }
.play { position: absolute; right: 2px; bottom: 2px; width: 15px; height: 15px; display: grid; place-items: center; padding-left: 1px; border-radius: 4px; background: rgba(0, 0, 0, .6); color: #fff; }
.txt { flex: 1; min-width: 0; display: flex; flex-direction: column; }
.name { font-size: 14px; font-weight: 500; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.sub { font-size: 12px; color: var(--text-3); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; margin-top: 1px; }
.date { flex: none; font-size: 11.5px; color: var(--text-3); font-variant-numeric: tabular-nums; }
.row.sel .date { color: var(--text-2); }
.more .name { font-size: 13px; color: var(--text-2); font-weight: 400; }
</style>
