const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const test = require('node:test');

const { scanRepository } = require('./check-brand.cjs');

const legacyProductName = ['Staff', 'Deck'].join('');
const legacyFileStem = ['staff', 'deck'].join('');

test('detects a legacy product identifier in active content and paths', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'gongge-brand-'));
  try {
    fs.mkdirSync(path.join(root, 'backend'), { recursive: true });
    fs.writeFileSync(
      path.join(root, 'backend', `${legacyFileStem}_worker.py`),
      `APP = "${legacyProductName}"\n`,
      'utf8',
    );

    const failures = scanRepository({ repoRoot: root });

    assert.ok(failures.some((item) => item.includes('legacy name in path')));
    assert.ok(failures.some((item) => item.includes('legacy name in content')));
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test('ignores only the original reference source tree', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'gongge-brand-'));
  try {
    const original = path.join(root, 'otherpro', legacyProductName);
    fs.mkdirSync(original, { recursive: true });
    fs.writeFileSync(path.join(original, 'README.md'), `# ${legacyProductName}\n`, 'utf8');

    assert.deepEqual(scanRepository({ repoRoot: root }), []);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test('does not allow an exception list to bypass active-project detection', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'gongge-brand-'));
  try {
    fs.writeFileSync(path.join(root, 'service.py'), `APP = "${legacyProductName}"\n`, 'utf8');

    const failures = scanRepository({
      repoRoot: root,
      allowlist: [{ path: 'service.py', match: legacyProductName }],
    });

    assert.ok(failures.some((item) => item.includes('legacy name in content')));
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test('scans hidden work records instead of excluding them', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'gongge-brand-'));
  try {
    const records = path.join(root, '.superpowers');
    fs.mkdirSync(records, { recursive: true });
    fs.writeFileSync(path.join(records, 'record.md'), legacyProductName, 'utf8');

    const failures = scanRepository({ repoRoot: root });

    assert.ok(failures.some((item) => item.startsWith('.superpowers/record.md:')));
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test('ignores broken symbolic links while scanning reference trees', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'gongge-brand-'));
  try {
    fs.mkdirSync(path.join(root, 'otherpro', 'reference'), { recursive: true });
    fs.symlinkSync(
      path.join(root, 'missing-target'),
      path.join(root, 'otherpro', 'reference', 'missing-link'),
    );

    assert.deepEqual(scanRepository({ repoRoot: root }), []);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});
