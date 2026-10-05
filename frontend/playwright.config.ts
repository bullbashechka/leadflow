import { defineConfig } from '@playwright/test'

export default defineConfig({
  testDir: './tests/browser',
  workers: 1,
  timeout: 30_000,
  outputDir: process.env.CRM_TEST_ARTIFACTS || '/tmp/leadflow-browser-results',
  use: {
    baseURL: process.env.CRM_TEST_BASE_URL || 'http://localhost:15173',
    browserName: 'chromium',
    screenshot: 'only-on-failure',
    trace: 'off',
    launchOptions: process.env.CRM_TEST_CHROMIUM ? { executablePath: process.env.CRM_TEST_CHROMIUM } : {},
  },
  projects: [
    { name: 'desktop', use: { viewport: { width: 1440, height: 1000 } } },
    { name: 'phone', use: { viewport: { width: 375, height: 812 } } },
  ],
})
