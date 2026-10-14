<script setup>
import { onMounted, ref, watch } from 'vue'
import Icon from './Icon.vue'
import { S, KINDS, toast } from '../store'
import { call } from '../api'

const detail = ref({ folders: [], failed: [] })
const known = ref([])
const showFailed = ref(false)
const confirming = ref('')
const removing = ref('') // 索引进行中移除要先停下当前文件，可能要几秒

const load = async () => {
  detail.value = await call('folders_detail')
  known.value = ((await call('known_folders')) || []).filter((f) => !f.added)
}
onMounted(load)
watch(() => S.overview, load)

async function remove(path) {
  if (confirming.value !== path) {
    confirming.value = path
    setTimeout(() => confirming.value === path && (confirming.value = ''), 3000)
    return
  }
  confirming.value = ''
  removing.value = path
  await call('remove_folder', path)
  removing.value = ''
  toast('已移出索引，文件本身不受影响')
}
const sum = (c) => Object.values(c).reduce((a, b) => a + b, 0)
// 文件夹名最能区分彼此，单独显示；长路径只截断上级部分
const parts = (p) => p.replace(/[\\/]+$/, '').split(/[\\/]/)
const base = (p) => parts(p).pop() || p
const parent = (p) => parts(p).slice(0, -1).join('\\')
</script>

<template>
  <div class="view">
    <div class="vh">
      <div>
        <h2>索引管理</h2>
        <p>这些文件夹（含子文件夹）里的文档、图片、音频和视频可以被搜索。</p>
      </div>
      <button v-if="S.index.running" class="btn" @click="call('stop_index')"><Icon name="stop" :size="12" /> 停止索引</button>
      <span v-else :title="S.model.state !== 'ready' ? '模型就绪后可用' : '扫描新增、修改和删除的文件（F5）'">
        <button class="btn primary" :disabled="S.model.state !== 'ready' || !detail.folders.length" @click="call('start_index')">
          <Icon name="refresh" :size="14" /> 立即更新
        </button>
      </span>
    </div>

    <div v-if="S.index.running" class="progress">
      <div class="track"><div class="fill" :style="{ width: (S.index.total ? S.index.done / S.index.total * 100 : 3) + '%' }" /></div>
      <span>{{ S.index.done }}/{{ S.index.total || '…' }} · {{ S.index.current }}</span>
    </div>

    <div class="folders">
      <div v-for="f in detail.folders" :key="f.path" class="folder" :class="{ removing: removing === f.path }">
        <span class="f-ic" :class="{ missing: !f.exists }"><Icon name="folder" :size="18" /></span>
        <div class="f-body">
          <div class="f-path" :title="f.path"><b>{{ base(f.path) }}</b><span v-if="parent(f.path)">{{ parent(f.path) }}</span></div>
          <div class="f-counts">
            <span v-if="removing === f.path"><span class="spinner" /> 正在移出…</span>
            <span v-else-if="!f.exists" class="warn"><Icon name="alert" :size="12" /> 文件夹不存在（移动硬盘未连接？）</span>
            <template v-else-if="sum(f.counts)">
              <span v-for="k in ['text', 'image', 'audio', 'video']" v-show="f.counts[k]" :key="k"><i :style="{ background: KINDS[k].color }" />{{ KINDS[k].label }} {{ f.counts[k] }}</span>
            </template>
            <span v-else>{{ S.index.running ? '正在索引…' : '等待索引' }}</span>
          </div>
        </div>
        <button class="icon-btn" title="打开文件夹" aria-label="打开文件夹" @click="call('reveal', f.path)"><Icon name="open" :size="15" /></button>
        <button class="icon-btn rm" :class="{ confirm: confirming === f.path }" :title="confirming === f.path ? '再点一次确认' : '移出索引'" aria-label="移出索引" @click="remove(f.path)">
          <Icon :name="confirming === f.path ? 'check' : 'trash'" :size="15" />
        </button>
      </div>
      <button class="add" @click="call('pick_folder')"><Icon name="plus" :size="16" /> 添加文件夹</button>
      <div v-if="known.length" class="quick">
        <span>常用位置</span>
        <button v-for="k in known" :key="k.key" class="qk" :title="k.path" @click="call('add_folders', [k.path])"><Icon name="plus" :size="13" /> {{ k.label }}</button>
      </div>
    </div>

    <div v-if="detail.failed.length" class="failed">
      <button class="fh" @click="showFailed = !showFailed">
        <Icon name="alert" :size="14" /> {{ detail.failed.length }} 个文件处理失败
        <Icon name="chevron" :size="14" class="chev" :style="{ transform: showFailed ? 'rotate(180deg)' : '' }" />
      </button>
      <template v-if="showFailed">
        <div class="fl">
          <div v-for="x in detail.failed" :key="x.path" class="fx"><span :title="x.path">{{ x.path }}</span><span :title="x.error">{{ x.error }}</span></div>
        </div>
        <button class="btn" :disabled="S.index.running" @click="call('retry_failed')">重试这些文件</button>
      </template>
    </div>
  </div>
</template>

<style scoped>
.view { height: 100%; overflow-y: auto; padding: 20px 24px 24px; animation: fade-in .12s; }
.vh { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; margin-bottom: 16px; }
h2 { margin: 0; font: 600 18px var(--font-display); }
.vh p { margin: 4px 0 0; font-size: 12.5px; color: var(--text-3); }
.progress { display: flex; align-items: center; gap: 12px; margin-bottom: 14px; font-size: 12px; color: var(--text-3); }
.progress span { max-width: 360px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.track { flex: 1; height: 4px; border-radius: 2px; background: var(--raise-2); overflow: hidden; }
.fill { height: 100%; background: var(--accent); transition: width .3s var(--ease); }
.folders { display: flex; flex-direction: column; gap: 6px; }
.folder { display: flex; align-items: center; gap: 12px; padding: 10px 8px 10px 12px; border-radius: 9px; background: var(--raise); }
.f-ic { display: grid; place-items: center; width: 36px; height: 36px; flex: none; border-radius: 8px; background: var(--raise-2); color: var(--text-2); }
.f-ic.missing { filter: grayscale(1); opacity: .5; }
.folder.removing { opacity: .55; pointer-events: none; }
.f-counts .spinner { width: 11px; height: 11px; border-width: 1.5px; }
.f-body { flex: 1; min-width: 0; }
.f-path { font-size: 13.5px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.f-path b { font-weight: 600; }
.f-path span { margin-left: 8px; font-size: 12px; color: var(--text-3); }
.f-counts { display: flex; flex-wrap: wrap; gap: 12px; margin-top: 3px; font-size: 12px; color: var(--text-3); }
.f-counts span { display: inline-flex; align-items: center; gap: 5px; }
.f-counts i { width: 6px; height: 6px; border-radius: 3px; }
.warn { color: var(--warn); }
.rm:hover { color: var(--err); }
.rm.confirm { background: var(--err); color: #fff; }
.add { display: flex; align-items: center; justify-content: center; gap: 7px; height: 46px; border-radius: 9px; border: 1.5px dashed var(--line-2); background: transparent; color: var(--text-2); }
.add:hover { border-color: var(--accent); color: var(--accent-text); }
.quick { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; margin-top: 4px; font-size: 12px; color: var(--text-3); }
.quick > span { margin-right: 4px; }
.qk { display: inline-flex; align-items: center; gap: 4px; height: 26px; padding: 0 10px 0 8px; border-radius: 13px; border: 1px solid var(--line-2); background: transparent; color: var(--text-2); font-size: 12px; }
.qk:hover { border-color: var(--accent); color: var(--accent-text); }
.failed { margin-top: 16px; display: flex; flex-direction: column; gap: 8px; align-items: flex-start; }
.fh { display: flex; align-items: center; gap: 7px; width: 100%; height: 36px; padding: 0 12px; border: none; border-radius: 8px; background: color-mix(in srgb, var(--warn) 12%, transparent); color: var(--warn); font-weight: 600; font-size: 12.5px; }
.chev { margin-left: auto; transition: transform .2s; }
.fl { width: 100%; max-height: 150px; overflow: auto; border-radius: 8px; background: var(--raise); }
.fx { display: grid; grid-template-columns: 3fr 2fr; gap: 12px; padding: 6px 12px; font-size: 12px; border-bottom: 1px solid var(--line); }
.fx span { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.fx span:last-child { color: var(--text-3); }
</style>
