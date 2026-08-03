const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');

const root = path.resolve(__dirname, '..');
const tsconfig = JSON.parse(fs.readFileSync(path.join(root, 'tsconfig.json'), 'utf8'));
const packageJson = JSON.parse(fs.readFileSync(path.join(root, 'package.json'), 'utf8'));
const packageLock = JSON.parse(fs.readFileSync(path.join(root, 'package-lock.json'), 'utf8'));
const pnpmLock = fs.readFileSync(path.join(root, 'pnpm-lock.yaml'), 'utf8');

function isPatchedBabelCore(version) {
  const [major, minor, patch] = version.split('.').map(Number);
  return major > 7 || (major === 7 && (minor > 29 || (minor === 29 && patch >= 6)));
}

test('uses TypeScript 6 bundler resolution without deprecated baseUrl', () => {
  assert.equal(tsconfig.compilerOptions.module, 'ESNext');
  assert.equal(tsconfig.compilerOptions.moduleResolution, 'Bundler');
  assert.equal('baseUrl' in tsconfig.compilerOptions, false);
  assert.deepEqual(tsconfig.compilerOptions.paths, { '@/*': ['./src/*'] });
  assert.deepEqual(tsconfig.compilerOptions.types, ['node']);
  assert.match(packageJson.devDependencies.typescript, /^\^6\./);
});

test('uses the patched Vite 6.4.3 dependency line in both lock files', () => {
  assert.equal(packageJson.devDependencies.vite, '^6.4.3');
  assert.equal(packageLock.packages['node_modules/vite'].version, '6.4.3');
  assert.match(pnpmLock, /vite@6\.4\.3/);
});

test('locks every Babel core instance to a patched release', () => {
  const npmVersions = Object.entries(packageLock.packages)
    .filter(([packagePath]) => packagePath.endsWith('node_modules/@babel/core'))
    .map(([, metadata]) => metadata.version);
  const pnpmVersions = [...pnpmLock.matchAll(/@babel\/core@(\d+\.\d+\.\d+)/g)].map(
    (match) => match[1],
  );

  assert.ok(npmVersions.length > 0);
  assert.ok(pnpmVersions.length > 0);
  assert.ok(npmVersions.every(isPatchedBabelCore), `unsafe npm Babel versions: ${npmVersions}`);
  assert.ok(pnpmVersions.every(isPatchedBabelCore), `unsafe pnpm Babel versions: ${pnpmVersions}`);
});
