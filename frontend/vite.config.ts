import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// The build lands inside the Python package so wheels/Docker can ship the SPA.
// The dev server proxies the API + crop images to the FastAPI backend so the
// SameSite=Lax session cookie is treated as same-origin by the browser.
export default defineConfig({
  plugins: [vue()],
  build: {
    outDir: '../src/synopticon/web/dist',
    emptyOutDir: true,
  },
  server: {
    proxy: {
      '/api': { target: 'http://127.0.0.1:8686', changeOrigin: true },
      '/crops': { target: 'http://127.0.0.1:8686', changeOrigin: true },
    },
  },
})
