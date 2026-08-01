/// <reference types="vitest/config" />
import { defineConfig } from 'vite';

export default defineConfig({
  base: './',
  build: {
    outDir: '../src/chronotrace/_ui',
    emptyOutDir: true,
  },
  server: {
    proxy: {
      '/api': 'http://127.0.0.1:8000',
      '/openapi.json': 'http://127.0.0.1:8000',
      '/docs': 'http://127.0.0.1:8000',
      '/api/sessions/': {
        target: 'http://127.0.0.1:8000',
        ws: true,
      }
    }
  },
  test: {
    // Unit tests only. The Playwright e2e spec (tests/e2e) runs under Playwright, not Vitest --
    // it imports @playwright/test, which Vitest cannot execute.
    include: ['src/**/*.test.ts'],
  }
});
