import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  server: {
    strictPort: true,
    proxy: { '/api': { target: 'http://127.0.0.1:8000', changeOrigin: false } },
  },
  test: { environment: 'jsdom', restoreMocks: true, setupFiles: ['./src/test-setup.ts'], testTimeout: 15000, fileParallelism: false },
});
