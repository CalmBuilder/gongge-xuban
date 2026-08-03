import { rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

export default async function fullstackGlobalTeardown() {
  await rm(join(tmpdir(), 'gongge-fullstack-e2e-current'), {
    recursive: true,
    force: true,
  });
}
