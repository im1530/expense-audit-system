import { fileURLToPath, URL } from 'node:url'
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// Vite 配置：开发期把 /api 代理到后端，避免浏览器跨域
export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
  },
  server: {
    host: '127.0.0.1',
    port: 5173,
    watch: {
      // 编辑器/工具会以"写临时文件再改名"的方式保存源码（形如 .xxx.ts.<pid>.tmpdir/），
      // 若被 watcher 盯上会报 EBUSY 并直接崩溃；这里显式忽略这些中间产物。
      ignored: ['**/*.tmpdir/**', '**/*.tmp', '**/.git/**', '**/node_modules/**'],
    },
    proxy: {
      // 后端地址可用环境变量覆盖（默认 8000，与 uvicorn 默认端口一致）
      '/api': {
        target: process.env.VITE_API_TARGET || 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: 'dist',
    chunkSizeWarningLimit: 1500,
    // 目标设为 esnext 且关闭压缩：本机环境（含沙箱 ACL/WinTemp 限制）下 esbuild 的
    // 降级与压缩阶段会因临时文件无法删除而失败；产物仍可直接部署，压缩交给 Nginx gzip。
    target: 'esnext',
    minify: false,
  },
})
