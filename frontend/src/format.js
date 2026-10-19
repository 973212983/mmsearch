// 时长、大小、日期的显示格式（列表与预览共用）

export function fmtTime(t) {
  t = Math.floor(t || 0)
  const m = Math.floor(t / 60), s = t % 60
  return t >= 3600 ? `${Math.floor(t / 3600)}:${String(m % 60).padStart(2, '0')}:${String(s).padStart(2, '0')}` : `${m}:${String(s).padStart(2, '0')}`
}

export function fmtSize(n) {
  const u = ['B', 'KB', 'MB', 'GB']
  let i = 0
  while (n >= 1024 && i < 3) { n /= 1024; i++ }
  return `${i ? n.toFixed(1) : n} ${u[i]}`
}

/** 列表里的修改日期：今天 / 昨天 / 3 天前 / 4月12日 / 2023/4/12 */
export function fmtDate(sec) {
  if (!sec) return ''
  const d = new Date(sec * 1000), now = new Date()
  const day = (x) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime()
  const diff = Math.round((day(now) - day(d)) / 86400000)
  if (diff === 0) return '今天'
  if (diff === 1) return '昨天'
  if (diff > 1 && diff < 7) return `${diff} 天前`
  if (d.getFullYear() === now.getFullYear()) return `${d.getMonth() + 1}月${d.getDate()}日`
  return `${d.getFullYear()}/${d.getMonth() + 1}/${d.getDate()}`
}

const STOP = new Set('的了和与及或是在把被就都也而这那个些有为之其于从对')
/** 查询里的高亮词。中文没有空格，取每段连续汉字中所有 ≥2 字的子串（“会议纪要”能命中“会议”“纪要”），
 *  去掉以虚词开头或结尾的（“边的”“的日”）；英文和数字按词。 */
export function queryTerms(q) {
  const out = new Set()
  for (const tok of (q || '').split(/[\s,，.。、;；:：!！?？"“”'‘’()（）]+/)) {
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

/** 片段里和查询最相关的一行（命中的查询词总长最大）；没有命中时取第一行非空内容。 */
export function bestLine(text, terms) {
  const lines = (text || '').split(/\n|(?<=[。！？!?；;])/).map((l) => l.replace(/^[#>*\-\s]+/, '').trim()).filter(Boolean)
  let best = lines[0] || '', top = 0
  const low = terms.map((t) => t.toLowerCase())
  for (const l of lines) {
    const s = l.toLowerCase()
    const score = low.reduce((a, t) => a + (s.includes(t) ? t.length : 0), 0)
    if (score > top) [best, top] = [l, score]
  }
  return best
}
