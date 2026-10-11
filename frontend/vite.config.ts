import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';

export default defineConfig(({ command }) => ({
  plugins: [react()],
  // Build artifacts always target the backend that serves them. This overrides
  // shell/.env values even when a build uses a custom mode or NODE_ENV.
  define: command === 'build' ? { 'import.meta.env.VITE_API_BASE_URL': JSON.stringify('/') } : undefined,
  test: {
    environment: 'jsdom',
    setupFiles: ['./src/test/setup.ts'],
    include: ['src/**/*.test.{ts,tsx}'],
    restoreMocks: true,
  },
}));
