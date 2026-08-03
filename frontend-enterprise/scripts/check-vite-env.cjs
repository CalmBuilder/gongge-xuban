const fs = require('fs');
const path = require('path');

const projectRoot = path.resolve(__dirname, '..');
const viteConfig = fs.readFileSync(path.join(projectRoot, 'vite.config.ts'), 'utf8');
const envExample = fs.readFileSync(path.join(projectRoot, '.env.example'), 'utf8');

let failed = false;

function check(label, condition) {
  if (condition) {
    console.log(`  ✓ ${label}`);
  } else {
    console.error(`  ✗ ${label}`);
    failed = true;
  }
}

console.log('vite.config.ts checks:');
check('imports loadEnv from vite', /import\s*\{[^}]*loadEnv[^}]*\}\s*from\s*'vite'/.test(viteConfig));
check('uses defineConfig with callback', /defineConfig\s*\(\s*\(\s*\{?\s*mode\s*\}?\s*\)/.test(viteConfig));
check('calls loadEnv()', /loadEnv\s*\(/.test(viteConfig));
check(
  'uses env.VITE_PROXY_TARGET (not process.env)',
  /env\.VITE_PROXY_TARGET/.test(viteConfig) && !/process\.env\.VITE_PROXY_TARGET/.test(viteConfig),
);
check('default target is loopback:8000', /127\.0\.0\.1:8000/.test(viteConfig));
check('contains no hardcoded IPv4 deployment target', !/https?:\/\/(?!127\.0\.0\.1\b|0\.0\.0\.0\b)\d{1,3}(?:\.\d{1,3}){3}/.test(viteConfig));

console.log('\n.env.example checks:');
check('VITE_PROXY_TARGET present', /VITE_PROXY_TARGET/.test(envExample));
check('default port is loopback:8000', /127\.0\.0\.1:8000/.test(envExample));

if (failed) {
  console.error('\nSome checks failed.');
  process.exit(1);
}

console.log('\nAll checks passed.');
