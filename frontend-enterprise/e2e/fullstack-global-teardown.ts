import { rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

export default async function fullstackGlobalTeardown() {
  if (process.env.PRESERVE_FULLSTACK_E2E === '1') return;
  await rm(join(tmpdir(), 'gongge-fullstack-e2e-current'), {
    recursive: true,
    force: true,
  });
}
