<script setup>
import { computed, ref } from 'vue'
import Icon from './Icon.vue'
import ModelSource from './ModelSource.vue'
import { S, saveSettings, toast, applyBackground, pickBackground, clearBackground } from '../store'
import { call } from '../api'

// 每项改动立即生效并保存（像系统设置一样），不需要“保存”按钮，按 Esc 返回也不会丢
const confirm = ref('')
const editModel = ref(false)
const modelLabel = computed(() => {
  const m = S.settings.model_source || { kind: 'official' }
  if (m.kind === 'local') return `本地文件 · ${m.model.split(/[\\/]/).pop()}`
  return m.kind === 'repo' ? `${m.repo} · ${m.model}` : `官方 · ${S.boot.official_source.model}`
})
const recording = ref(false)
const DEVICES = [['auto', '自动'], ['vulkan', 'GPU'], ['cpu', 'CPU']]
const THEMES = [['system', '跟随系统'], ['dark', '深色'], ['light', '浅色']]

function set(patch) {
  saveSettings(patch) // 主题/磨砂/背景都会在那里一并同步到 CSS 与原生窗口
}
// 拖动滑块时只改本地值（立即可见），松开才写盘
function draft(patch) {
  Object.assign(S.settings, patch)
  applyBackground()
}
const clearBg = () => clearBackground()
const pickBg = () => pickBackground()

async function setAutostart(on) {
  S.boot.autostart = on
  await saveSettings({ autostart: on })
}
async function recordKey(e) {
  if (!recording.value) return
  e.preventDefault()
  e.stopPropagation()
  if (e.key === 'Escape') return (recording.value = false)
  const mods = [e.ctrlKey && 'ctrl', e.altKey && 'alt', e.shiftKey && 'shift', e.metaKey && 'win'].filter(Boolean)
  const key = e.code === 'Space' ? 'space' : e.key.length === 1 ? e.key.toLowerCase() : /^F\d+$/.test(e.key) ? e.key.toLowerCase() : ''
  if (!mods.length || !key || ['control', 'alt', 'shift', 'meta'].includes(key)) return
  recording.value = false
  const want = [...mods, key].join('+')
  const r = await saveSettings({ hotkey: want })
  if (r?.hotkey === want) toast(`快捷键已改为 ${label(want)}`)
}
const label = (hk) => (hk || '').split('+').map((p) => (p === 'space' ? 'Space' : p.length === 1 ? p.toUpperCase() : p[0].toUpperCase() + p.slice(1))).join(' + ')

async function clear(which) {
  if (confirm.value !== which) {
    confirm.value = which
    setTimeout(() => confirm.value === which && (confirm.value = ''), 3000)
    return
  }
  confirm.value = ''
  await call('clear_index', which)
  toast(which === 'visual' ? '正在用新精度重建图片和视频' : '已清空，正在重新索引')
}
</script>

<template>
  <div class="view" @keydown.capture="recordKey">
    <div class="vh">
      <h2>设置</h2>
      <span class="hint">改动立即生效</span>
    </div>

    <div v-if="S.model.state === 'error'" class="err">
      <Icon name="alert" :size="15" />
      <div>
        <b>模型启动失败</b><p>{{ S.model.error }}</p><p>日志：{{ S.model.log }}。可以把下面的推理设备改为 CPU，或直接重试。</p>
      </div>
      <button class="btn" @click="call('retry_model')"><Icon name="refresh" :size="14" /> 重试</button>
    </div>

    <div class="group">
      <div class="row">
        <div><b>唤出快捷键</b><span>在任何地方按下即可打开搜索</span></div>
        <button class="hk" :class="{ rec: recording }" @click="recording = !recording">{{ recording ? '请按下新的组合键…' : label(S.settings.hotkey) }}</button>
      </div>
      <label class="row">
        <div><b>失去焦点时自动收起</b><span>只在搜索时生效，像 Spotlight 一样；索引管理和设置页不会收起</span></div>
        <input :checked="S.settings.autohide" type="checkbox" class="switch" @change="set({ autohide: $event.target.checked })" />
      </label>
      <label class="row">
        <div><b>开机启动</b><span>登录后在后台运行，随时用快捷键唤出</span></div>
        <input :checked="S.boot.autostart" type="checkbox" class="switch" @change="setAutostart($event.target.checked)" />
      </label>
      <div class="row">
        <div><b>外观</b></div>
        <div class="seg"><button v-for="[k, t] in THEMES" :key="k" :class="{ on: S.settings.theme === k }" @click="set({ theme: k })">{{ t }}</button></div>
      </div>
    </div>

    <div class="group">
      <label class="row">
        <div><b>半透明磨砂</b><span>整个面板变半透明，透出桌面和背后的窗口，由系统亚克力实时模糊{{ S.glassOk ? '' : '（当前系统不支持亚克力，改用截图模拟）' }}</span></div>
        <input :checked="S.settings.glass" type="checkbox" class="switch" @change="set({ glass: $event.target.checked })" />
      </label>
      <div class="row">
        <div><b>背景图片</b><span>{{ S.bgUrl ? '图片会垫在所有半透明区域下面，可用滑块调节模糊' : '未设置：半透明时透出的是桌面' }}</span></div>
        <div class="btns">
          <img v-if="S.bgUrl" class="bg-thumb" :src="S.bgUrl" alt="背景预览" />
          <button class="btn" @click="pickBg">选择图片…</button>
          <button v-if="S.bgUrl" class="btn" @click="clearBg">清除</button>
        </div>
      </div>
      <label class="row">
        <div><b>背景模糊</b><span>{{ S.settings.bg_blur }} px · 0 是清晰壁纸，越大越像磨砂</span></div>
        <input class="rng" type="range" min="0" max="40" step="1" :value="S.settings.bg_blur"
               @input="draft({ bg_blur: +$event.target.value })" @change="set({ bg_blur: +$event.target.value })" />
      </label>
      <label class="row">
        <div><b>背景暗度</b><span>{{ S.settings.bg_dim }}% · 底色遮罩，保证文字在任何背景上都清楚</span></div>
        <input class="rng" type="range" min="0" max="80" step="1" :value="S.settings.bg_dim"
               @input="draft({ bg_dim: +$event.target.value })" @change="set({ bg_dim: +$event.target.value })" />
      </label>
    </div>

    <div class="group">
      <div class="row">
        <div><b>模型</b><span class="path">{{ modelLabel }}</span></div>
        <button class="btn" @click="editModel = !editModel">{{ editModel ? '收起' : '更换…' }}</button>
      </div>
      <div v-if="editModel" class="row sub"><ModelSource @done="editModel = false" /></div>
      <div class="row">
        <div><b>推理设备</b><span>自动会优先使用显卡（Vulkan），失败时改用 CPU；切换后模型会重新加载</span></div>
        <div class="seg"><button v-for="[k, t] in DEVICES" :key="k" :class="{ on: S.settings.backend === k }" @click="S.settings.backend !== k && set({ backend: k })">{{ t }}</button></div>
      </div>
      <div class="row">
        <div><b>图片与视频精度</b><span>越高细节越多、索引越慢；只影响之后索引的文件</span></div>
        <div class="seg"><button v-for="[n, t] in S.boot.token_presets" :key="n" :class="{ on: S.settings.image_tokens === n }" @click="S.settings.image_tokens !== n && set({ image_tokens: n })">{{ t }}</button></div>
      </div>
      <label class="row">
        <div><b>启动时自动更新索引</b><span>只处理新增和修改过的文件</span></div>
        <input :checked="S.settings.auto_index" type="checkbox" class="switch" @change="set({ auto_index: $event.target.checked })" />
      </label>
      <label class="row">
        <div><b>实时更新索引</b><span>文件夹里新增、修改、删除文件后自动处理</span></div>
        <input :checked="S.settings.watch" type="checkbox" class="switch" @change="set({ watch: $event.target.checked })" />
      </label>
      <label class="row">
        <div><b>用电池或全屏时暂停索引</b><span>索引会让显卡满载：省电，也不和游戏、视频、演示抢显卡</span></div>
        <input :checked="S.settings.pause_busy !== false" type="checkbox" class="switch" @change="set({ pause_busy: $event.target.checked })" />
      </label>
      <div class="row">
        <div><b>模型下载源</b></div>
        <select :value="S.settings.hf_endpoint" @change="set({ hf_endpoint: $event.target.value })"><option v-for="[u, n] in S.boot.endpoints" :key="u" :value="u">{{ n }}</option></select>
      </div>
    </div>

    <div class="group">
      <div class="row">
        <div><b>数据目录</b><span class="path">{{ S.boot.data_dir }}</span></div>
        <button class="btn" @click="call('open_data_dir')">打开</button>
      </div>
      <div class="row">
        <div><b>重建索引</b><span>文件本身不受影响</span></div>
        <div class="btns">
          <button class="btn" @click="clear('visual')">{{ confirm === 'visual' ? '确认重建？' : '重建图片和视频' }}</button>
          <button class="btn danger" @click="clear('all')">{{ confirm === 'all' ? '确认清空？' : '清空全部' }}</button>
        </div>
      </div>
    </div>

    <p class="about">MMSearch {{ S.boot.version }} · 默认模型 EmbeddingGemma 2（Google DeepMind）· llama.cpp · 数据只保存在本机</p>
  </div>
</template>

<style scoped>
.view { height: 100%; overflow-y: auto; padding: 20px 24px 24px; animation: fade-in .12s; }
.vh { display: flex; align-items: baseline; justify-content: space-between; margin-bottom: 14px; }
h2 { margin: 0; font: 600 18px var(--font-display); }
.hint { font-size: 12px; color: var(--text-3); }
.group { border-radius: 10px; background: var(--raise); margin-bottom: 12px; }
.row { display: flex; align-items: center; justify-content: space-between; gap: 16px; min-height: 54px; padding: 8px 14px; }
.row + .row { border-top: 1px solid var(--line); }
.row > div:first-child { display: flex; flex-direction: column; min-width: 0; }
.row b { font-size: 13.5px; font-weight: 500; }
.row span { font-size: 12px; color: var(--text-3); }
.path { user-select: text; word-break: break-all; }
.row.sub { padding: 12px 14px 14px; }
.btns { display: flex; gap: 6px; flex: none; align-items: center; }
.bg-thumb { width: 34px; height: 34px; border-radius: 6px; object-fit: cover; border: 1px solid var(--line-2); flex: none; }
.rng { appearance: none; -webkit-appearance: none; width: 150px; height: 20px; margin: 0; background: transparent; flex: none; }
.rng::-webkit-slider-runnable-track { height: 4px; border-radius: 2px; background: var(--raise-2); }
.rng::-webkit-slider-thumb { appearance: none; -webkit-appearance: none; width: 14px; height: 14px; margin-top: -5px; border: none; border-radius: 50%; background: var(--accent); }
.seg { display: inline-flex; gap: 2px; padding: 3px; border-radius: 8px; background: var(--bg); flex: none; }
.seg button { height: 26px; padding: 0 12px; border: none; border-radius: 6px; background: transparent; color: var(--text-2); font-size: 12.5px; }
.seg button.on { background: var(--raise-2); color: var(--text); font-weight: 600; }
.hk { min-width: 120px; height: 30px; padding: 0 12px; border-radius: 7px; border: 1px solid var(--line-2); background: var(--bg); font: 600 12.5px var(--font); }
.hk.rec { border-color: var(--accent); color: var(--accent-text); }
select { height: 30px; padding: 0 8px; border-radius: 7px; border: 1px solid var(--line-2); background: var(--bg); }
.switch { appearance: none; width: 38px; height: 21px; border-radius: 11px; background: var(--raise-2); position: relative; cursor: pointer; transition: background .2s; flex: none; margin: 0; }
.switch::after { content: ''; position: absolute; top: 3px; left: 3px; width: 15px; height: 15px; border-radius: 50%; background: var(--text-2); transition: transform .2s var(--ease), background .2s; }
.switch:checked { background: var(--accent); }
.switch:checked::after { transform: translateX(17px); background: var(--accent-ink); }
.err { display: flex; align-items: flex-start; gap: 10px; padding: 12px 14px; margin-bottom: 12px; border-radius: 10px; background: color-mix(in srgb, var(--err) 12%, transparent); color: var(--err); }
.err > div { flex: 1; min-width: 0; }
.err p { margin: 3px 0 0; font-size: 12px; color: var(--text-2); white-space: pre-wrap; user-select: text; }
.err .btn { flex: none; color: var(--text); }
.about { margin: 4px 0 0; font-size: 11.5px; color: var(--text-3); }
</style>
