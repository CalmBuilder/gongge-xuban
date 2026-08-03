const fs = require('node:fs');
const path = require('node:path');

const DEFAULT_REPO_ROOT = path.resolve(__dirname, '..', '..');
const TEXT_EXTENSIONS = new Set([
  '', '.cjs', '.css', '.env', '.html', '.ini', '.iss', '.js', '.json', '.md', '.mdc',
  '.mjs', '.ps1', '.py', '.sh', '.spec', '.svg', '.toml', '.ts', '.tsx', '.txt', '.yaml', '.yml',
]);
const EXCLUDED_DIRECTORY_NAMES = new Set([
  '.git', '.venv', 'build', 'dist', 'node_modules', 'venv',
]);
const ORIGINAL_REFERENCE_ROOT = ['otherpro', ['Staff', 'Deck'].join('')].join('/');
const EXCLUDED_ROOTS = new Set([
  ORIGINAL_REFERENCE_ROOT,
  'packaging/out',
  'packaging/build',
  'packaging/runtime',
  'packaging/runtime_dl',
]);

function legacyPatterns() {
  return [
    { name: 'legacy-brand', regex: new RegExp(`staff[ _-]?${'deck'}`, 'gi') },
    { name: 'legacy-workspace', regex: new RegExp(`staff[ _-]?${'twins'}`, 'gi') },
    { name: 'legacy-platform', regex: new RegExp(`ultra[ _-]?${'rag'}`, 'gi') },
    { name: 'legacy-service', regex: new RegExp(`skill[ _-]?agent[ _-]?${'loop'}`, 'gi') },
  ];
}

function normalize(relativePath) {
  return relativePath.split(path.sep).join('/').replace(/^\.\//, '');
}

function isExcluded(relativePath, directoryEntry) {
  const normalized = normalize(relativePath);
  if (EXCLUDED_ROOTS.has(normalized) || [...EXCLUDED_ROOTS].some((root) => normalized.startsWith(`${root}/`))) {
    return true;
  }
  return directoryEntry?.isDirectory() && EXCLUDED_DIRECTORY_NAMES.has(directoryEntry.name);
}

function inspectText(relativePath, absolutePath, failures) {
  const extension = path.extname(relativePath).toLowerCase();
  if (!TEXT_EXTENSIONS.has(extension)) return;
  const lines = fs.readFileSync(absolutePath, 'utf8').split(/\r?\n/);
  lines.forEach((line, lineIndex) => {
    for (const pattern of legacyPatterns()) {
      pattern.regex.lastIndex = 0;
      for (const match of line.matchAll(pattern.regex)) {
        failures.push(
          `${normalize(relativePath)}:${lineIndex + 1}: legacy name in content (${match[0]})`,
        );
      }
    }
  });
}

function scanRepository({ repoRoot = DEFAULT_REPO_ROOT } = {}) {
  const failures = [];

  function inspect(relativePath) {
    const absolutePath = path.join(repoRoot, relativePath);
    const stat = fs.lstatSync(absolutePath);
    if (stat.isSymbolicLink()) return;
    if (stat.isDirectory()) {
      for (const entry of fs.readdirSync(absolutePath, { withFileTypes: true })) {
        const childPath = path.join(relativePath, entry.name);
        if (!isExcluded(childPath, entry)) inspect(childPath);
      }
      return;
    }

    for (const pattern of legacyPatterns()) {
      pattern.regex.lastIndex = 0;
      for (const match of normalize(relativePath).matchAll(pattern.regex)) {
        failures.push(`${normalize(relativePath)}: legacy name in path (${match[0]})`);
      }
    }
    inspectText(relativePath, absolutePath, failures);
  }

  for (const entry of fs.readdirSync(repoRoot, { withFileTypes: true })) {
    if (!isExcluded(entry.name, entry)) inspect(entry.name);
  }
  return failures;
}

function runCli() {
  const failures = scanRepository({ repoRoot: DEFAULT_REPO_ROOT });
  if (failures.length) {
    console.error(`Legacy brand references found (${failures.length}):`);
    failures.forEach((failure) => console.error(`- ${failure}`));
    process.exitCode = 1;
    return;
  }
  console.log('Brand check passed: no unapproved legacy product references.');
}

module.exports = { scanRepository };

if (require.main === module) runCli();
