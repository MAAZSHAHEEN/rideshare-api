import { defineConfig, devices } from '@playwright/test';

// Optional local FastAPI server serving dist/, for production-bundle validation.
const baseURL = process.env.PLAYWRIGHT_BASE_URL;

export default defineConfig({
  testDir: './e2e',
  fullyParallel: false,
  workers: 1,
  retries: 0,
  use: {
    baseURL: baseURL || 'http://127.0.0.1:5183',
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    channel: process.env.PLAYWRIGHT_CHANNEL || undefined,
  },
  projects: [{ name: 'desktop', use: { ...devices['Desktop Chrome'] } }],
  webServer: baseURL ? undefined : {
    command: 'node node_modules/vite/bin/vite.js --host 127.0.0.1 --port 5183 --strictPort',
    url: 'http://127.0.0.1:5183',
    reuseExistingServer: false,
  },
});
