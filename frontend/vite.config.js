import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 构建产物直接放进 Python 包，由 pywebview 加载
export default defineConfig({
  plugins: [vue()],
  base: './',
  build: { outDir: '../mmsearch/web', emptyOutDir: true, assetsInlineLimit: 8192 },
})
