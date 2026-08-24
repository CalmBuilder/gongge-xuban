import { rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

export default async function fullstackGlobalTeardown() {
  if (process.env.PRESERVE_FULLSTACK_E2E === '1') return;
  const runtimePath = process.env.FULLSTACK_E2E_RUNTIME_DIR
    ? process.env.FULLSTACK_E2E_RUNTIME_DIR
    : join(tmpdir(), 'gongge-fullstack-e2e-current');
  for (let attempt = 0; attempt < 3; attempt += 1) {
    try {
      await rm(runtimePath, { recursive: true, force: true, maxRetries: 3, retryDelay: 100 });
      return;
    } catch (error) {
      if (attempt === 2) throw error;
      await new Promise((resolve) => setTimeout(resolve, 150 * (attempt + 1)));
    }
  }
}
