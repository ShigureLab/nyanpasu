import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './tests',
  projects: [
    { name: 'dashboard', testMatch: 'dashboard.browser.ts' },
    { name: 'auth', testMatch: 'auth.browser.ts', use: { baseURL: 'http://127.0.0.1:8767' } },
  ],
  workers: 1,
  use: {
    baseURL: 'http://127.0.0.1:8766',
    viewport: { width: 1440, height: 1000 },
    launchOptions: process.env.NYANPASU_TEST_CHROME
      ? { executablePath: process.env.NYANPASU_TEST_CHROME }
      : {},
  },
  webServer: [
    {
      command: 'uv run python -m tests.dashboard_fixture',
      url: 'http://127.0.0.1:8766/health',
      reuseExistingServer: !process.env.CI,
    },
    {
      command: 'uv run python -m tests.dashboard_fixture',
      env: { NYANPASU_TEST_TOKEN: 'browser-test-token', NYANPASU_TEST_PORT: '8767' },
      url: 'http://127.0.0.1:8767/health',
      reuseExistingServer: !process.env.CI,
    },
  ],
});
