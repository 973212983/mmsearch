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
