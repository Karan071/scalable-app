import path from 'node:path'

import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { loadEnv } from 'vite'
import { defineConfig } from 'vitest/config'

export default defineConfig(({ mode }) => {
  // BFF origin for local dev (LLD-15 §2: same-origin in production, where
  // `web`'s build output is served by the BFF itself; Vite's dev proxy
  // simulates that so cookies stay first-party while iterating).
  const env = loadEnv(mode, process.cwd(), '')
  const bffPort = env.VITE_BFF_PORT ?? '8020'

  return {
    plugins: [react(), tailwindcss()],
    resolve: {
      alias: {
        '@': path.resolve(import.meta.dirname, './src'),
      },
    },
    server: {
      proxy: {
        '/auth': `http://localhost:${bffPort}`,
        '/api': `http://localhost:${bffPort}`,
      },
    },
    test: {
      environment: 'jsdom',
      globals: true,
      setupFiles: ['./src/test/setup.ts'],
    },
  }
})
