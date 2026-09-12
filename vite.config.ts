import { defineConfig } from 'vite-plus';

export default defineConfig({
  root: 'frontend/dashboard',
  base: '/dashboard/assets/',
  server: {
    proxy: {
      '/api': 'http://127.0.0.1:8765',
    },
  },
  build: {
    outDir: '../../src/nyanpasu/dashboard_static',
    assetsDir: '.',
    emptyOutDir: true,
  },
  test: {
    environment: 'happy-dom',
    include: ['src/**/*.test.ts'],
  },
  lint: {
    ignorePatterns: ['src/nyanpasu/dashboard_static/**'],
  },
  fmt: {
    semi: true,
    singleQuote: true,
  },
});
