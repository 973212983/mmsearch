<script setup>
import { computed, reactive, ref } from 'vue'
import Icon from './Icon.vue'
import { S, toast } from '../store'
import { call } from '../api'

// 模型来源：官方仓库 / 其他 Hugging Face 仓库（同系列的其他量化、微调、镜像）/ 本地 GGUF 文件
const emit = defineEmits(['done'])
const KINDS = [['official', '官方模型'], ['repo', '其他仓库'], ['local', '本地文件']]

const official = S.boot.official_source
const cur = S.settings.model_source || { kind: 'official' }
const kind = ref(cur.kind)
// 其他仓库默认沿用官方文件名：同系列的仓库大多这样命名，用户只要改仓库名
const repo = reactive(cur.kind === 'repo' ? { ...cur } : { repo: '', model: official.model, mmproj: official.mmproj })
const local = reactive(cur.kind === 'local' ? { ...cur } : { model: '', mmproj: '' })
const error = ref('')
const busy = ref(false)
const confirming = ref(false)

const want = computed(() =>
  kind.value === 'official' ? { kind: 'official' }
    : kind.value === 'repo' ? { kind: 'repo', repo: repo.repo.trim(), model: repo.model.trim(), mmproj: repo.mmproj.trim() }
      : { kind: 'local', model: local.model, mmproj: local.mmproj })
const key = (s) => [s.kind, s.repo || '', s.model, s.mmproj].join('\n')
const changed = computed(() => key(want.value) !== key(cur))
const filled = computed(() => !!(want.value.model && want.value.mmproj && (want.value.kind !== 'repo' || want.value.repo)))
const hasIndex = computed(() => (S.overview.items || 0) > 0)

async function pick(role) {
  const p = await call('pick_model_file')
  if (p) local[role] = p
}

async function apply() {
  error.value = ''
  if (hasIndex.value && !confirming.value) {
    confirming.value = true
    setTimeout(() => (confirming.value = false), 4000)
    return
  }
  confirming.value = false
  busy.value = true
  const r = await call('set_model_source', want.value)
  busy.value = false
  if (!r || r.error) return (error.value = r?.error || '切换失败')
  S.settings.model_source = r.source
  S.missing = r.missing
  if (!r.missing.length) toast(hasIndex.value ? '已切换模型，加载完成后会自动重建索引' : '已切换模型，正在加载')
  emit('done')
}
const base = (p) => (p ? p.split(/[\\/]/).pop() : '')
</script>

<template>
  <div class="ms">
    <div class="seg">
      <button v-for="[k, t] in KINDS" :key="k" :class="{ on: kind === k }" @click="kind = k; error = ''">{{ t }}</button>
    </div>

    <p v-if="kind === 'official'" class="note">{{ official.repo }} · {{ official.model }} + {{ official.mmproj }}</p>

    <template v-else-if="kind === 'repo'">
      <p class="note">Hugging Face 上 EmbeddingGemma 2 系列的其他 GGUF（其他量化、微调或镜像仓库），使用上面选择的下载源。</p>
      <label class="f"><span>仓库</span><input v-model="repo.repo" placeholder="作者/仓库名" spellcheck="false" /></label>
      <label class="f"><span>模型文件</span><input v-model="repo.model" placeholder="xxx.gguf（可带仓库内子目录）" spellcheck="false" /></label>
      <label class="f"><span>mmproj 文件</span><input v-model="repo.mmproj" placeholder="mmproj-xxx.gguf" spellcheck="false" /></label>
    </template>

    <template v-else>
      <p class="note">直接使用本机已有的 GGUF 文件（不会复制，请不要移动它们）。需要文本模型和对应的 mmproj（视觉/音频编码器）两个文件。</p>
      <div v-for="[role, t] in [['model', '模型文件'], ['mmproj', 'mmproj 文件']]" :key="role" class="f">
        <span>{{ t }}</span>
        <div class="pick" :title="local[role]"><Icon name="file" :size="13" />{{ base(local[role]) || '未选择' }}</div>
        <button class="btn" @click="pick(role)">选择…</button>
      </div>
    </template>

    <div v-if="error" class="err"><Icon name="alert" :size="13" /> {{ error }}</div>
    <div class="acts">
      <span v-if="hasIndex && changed" class="warn">换成不同的模型后会重建全部索引</span>
      <button class="btn primary" :disabled="!changed || !filled || busy" @click="apply">{{ confirming ? '确认更换？' : '使用这个模型' }}</button>
    </div>
  </div>
</template>

<style scoped>
.ms { display: flex; flex-direction: column; gap: 10px; width: 100%; text-align: left; }
.seg { display: inline-flex; align-self: flex-start; gap: 2px; padding: 3px; border-radius: 8px; background: var(--bg); }
.seg button { height: 26px; padding: 0 12px; border: none; border-radius: 6px; background: transparent; color: var(--text-2); font-size: 12.5px; }
.seg button.on { background: var(--raise-2); color: var(--text); font-weight: 600; }
.note { margin: 0; font-size: 12px; line-height: 1.6; color: var(--text-3); word-break: break-all; }
.f { display: flex; align-items: center; gap: 10px; }
.f > span { width: 76px; flex: none; font-size: 12.5px; color: var(--text-2); }
.f input { flex: 1; min-width: 0; height: 30px; padding: 0 10px; border-radius: 7px; border: 1px solid var(--line-2); background: var(--bg); outline: none; font: 12.5px var(--mono); user-select: text; }
.f input:focus { border-color: var(--accent); }
.pick { flex: 1; min-width: 0; display: flex; align-items: center; gap: 6px; height: 30px; padding: 0 10px; border-radius: 7px; background: var(--bg); color: var(--text-2); font: 12.5px var(--mono); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.err { display: flex; align-items: center; gap: 6px; font-size: 12.5px; color: var(--err); }
.acts { display: flex; align-items: center; justify-content: flex-end; gap: 12px; }
.warn { font-size: 12px; color: var(--warn); }
</style>
