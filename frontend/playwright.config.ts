import { defineConfig, devices } from '@playwright/test';

// Optional local FastAPI server serving dist/, for production-bundle validation.
const baseURL = process.env.PLAYWRIGHT_BASE_URL;
// Overridable so parallel local runs (or other worktrees) don't collide on one port.
const devPort = Number(process.env.PLAYWRIGHT_DEV_PORT || 5183);

export default defineConfig({
  testDir: './e2e',
  fullyParallel: false,
  workers: 1,
  retries: 0,
  forbidOnly: Boolean(process.env.CI),
  reporter: process.env.CI ? [['list'], ['html', { open: 'never' }]] : 'list',
  use: {
    baseURL: baseURL || `http://127.0.0.1:${devPort}`,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    channel: process.env.PLAYWRIGHT_CHANNEL || undefined,
    // Local sandboxes without Playwright's bundled browser can point at a system Chromium.
    launchOptions: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH } : undefined,
  },
  projects: [{ name: 'desktop', use: { ...devices['Desktop Chrome'] } }],
  webServer: baseURL ? undefined : {
    command: `node node_modules/vite/bin/vite.js --host 127.0.0.1 --port ${devPort} --strictPort`,
    url: `http://127.0.0.1:${devPort}`,
    reuseExistingServer: false,
  },
});
