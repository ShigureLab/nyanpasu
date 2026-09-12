import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './tests',
  testMatch: '*.browser.ts',
  workers: 1,
  use: {
    baseURL: 'http://127.0.0.1:8766',
    viewport: { width: 1440, height: 1000 },
    launchOptions: process.env.NYANPASU_TEST_CHROME
      ? { executablePath: process.env.NYANPASU_TEST_CHROME }
      : {},
  },
  webServer: {
    command: 'uv run python -m tests.dashboard_fixture',
    url: 'http://127.0.0.1:8766/health',
    reuseExistingServer: !process.env.CI,
  },
});
