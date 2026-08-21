const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const fixtureDir = path.join(__dirname, 'fixtures');
const fixture = (name) => fs.readFileSync(path.join(fixtureDir, name), 'utf8');

assert.match(fixture('netease-personal.html'), /data-message-id="m-001"/);
assert.match(fixture('netease-iframe.html'), /<iframe/);
assert.match(fixture('netease-enterprise.html'), /data-message-body/);
assert.match(fixture('whatsapp-single.html'), /8613800000000@c\.us/);
assert.match(fixture('whatsapp-media-quote.html'), /image-thumb/);
assert.match(fixture('whatsapp-broken.html'), /Loading conversation/);
const neteaseSource = fs.readFileSync(path.join(__dirname, '..', 'adapters', 'netease.js'), 'utf8');
assert.ok(neteaseSource.includes('qiye\\.163\\.com'));
assert.ok(neteaseSource.includes('添加邮箱|高效管理|工作邮箱'));
assert.ok(!neteaseSource.includes("'[class*=\"message\"]'"));
const whatsappSource = fs.readFileSync(path.join(__dirname, '..', 'adapters', 'whatsapp.js'), 'utf8');
assert.ok(whatsappSource.includes('s\\.whatsapp\\.net'));
const manifest = JSON.parse(fs.readFileSync(path.join(__dirname, '..', 'manifest.json'), 'utf8'));
assert.ok(manifest.permissions.includes('webNavigation'));
assert.ok(manifest.permissions.includes('scripting'));
assert.equal(manifest.version, '0.5.0');
console.log('browser-extension fixture corpus: OK');
