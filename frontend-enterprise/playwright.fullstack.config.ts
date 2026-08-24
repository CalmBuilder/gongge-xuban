import { defineConfig, devices } from '@playwright/test';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const runId = process.env.FULLSTACK_E2E_RUN_ID ?? String(process.pid);
const fullstackPort = process.env.FULLSTACK_E2E_PORT ?? String(20_000 + (process.pid % 20_000));
process.env.FULLSTACK_E2E_PORT = fullstackPort;
process.env.FULLSTACK_E2E_RUNTIME_DIR ??= join(tmpdir(), `gongge-fullstack-e2e-${runId}`);
const fullstackBaseUrl = `http://127.0.0.1:${fullstackPort}`;

export default defineConfig({
  testDir: './e2e',
  testMatch: '**/*.fullstack.e2e.ts',
  globalTeardown: './e2e/fullstack-global-teardown.ts',
  fullyParallel: false,
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 1 : 0,
  workers: 1,
  reporter: [['list'], ['html', { open: 'never' }]],
  use: {
    baseURL: fullstackBaseUrl,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    video: 'retain-on-failure',
  },
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],
  webServer: {
    command: '../backend/.venv/bin/python e2e/start_fullstack_server.py',
    url: `${fullstackBaseUrl}/api/health`,
    reuseExistingServer: process.env.FULLSTACK_E2E_REUSE_EXISTING_SERVER === '1',
    timeout: 180_000,
  },
});
