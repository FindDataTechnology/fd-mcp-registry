import { defineConfig, devices } from '@playwright/test';

/**
 * Playwright configuration for MCP Gateway Registry e2e tests.
 *
 * The app is served by nginx on port 80 (http://localhost).
 * Authentication uses basic auth with session cookies.
 *
 * Set PLAYWRIGHT_BASE_URL to point the suite at a deployment instead — the
 * unauthenticated specs (login page, i18n smoke) then run against it as-is;
 * specs that use the local admin helper still need the compose stack.
 */
export default defineConfig({
  testDir: './e2e',
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  workers: 1,
  reporter: 'html',
  timeout: 60_000,

  use: {
    baseURL: process.env.PLAYWRIGHT_BASE_URL ?? 'http://localhost',
    trace: 'on-first-retry',
    screenshot: 'only-on-failure',
    video: 'retain-on-failure',
  },

  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],
});
