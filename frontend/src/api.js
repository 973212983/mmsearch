// 与 Python 的桥：pywebview 注入 window.pywebview.api；Python 通过 window.__mmEvent 推送事件
const ready = new Promise((resolve) => {
  if (window.pywebview?.api) resolve()
  else window.addEventListener('pywebviewready', () => resolve(), { once: true })
})

export async function call(name, ...args) {
  await ready
  return window.pywebview.api[name](...args)
}

const handlers = {}
window.__mmEvent = (kind, data) => (handlers[kind] || []).forEach((fn) => fn(data))

export function on(kind, fn) {
  ;(handlers[kind] ||= []).push(fn)
}
