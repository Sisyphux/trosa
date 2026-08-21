const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { JSDOM } = require('jsdom');

const root = path.resolve(__dirname, '..');
const html = fs.readFileSync(path.join(root, 'sidepanel.html'), 'utf8');
const script = fs.readFileSync(path.join(root, 'sidepanel.js'), 'utf8');
const dom = new JSDOM(html, { url: 'chrome-extension://trade-os/sidepanel.html', runScripts: 'outside-only' });
let readCount = 0;

dom.window.chrome = {
  storage: { local: { get(_keys, callback) { callback({ apiBase: 'http://localhost:8080' }); }, async set() {} } },
  tabs: { create() {} }, runtime: { getManifest() { return { version: 'test' }; } },
};
dom.window.fetch = async () => ({ ok: true, status: 200, async json() { return { logged_in: true }; } });
dom.window.TradeOSFrameReader = { async readPage() {
  readCount += 1;
  return { ok: true, data: {
    channel: 'whatsapp', platform: 'WhatsApp Web', phone: '+8613800000000', conversation_identity: 'Buyer',
    adapter_version: 'test', extraction_scope: 'test', warnings: [], messages: readCount === 1 ? [] : [{ fingerprint: 'retry-message', text: 'Loaded after mount', direction: 'inbound' }],
  }, diagnostics: { host: 'web.whatsapp.com', supported: true, frame_count: 1, successful_frame_count: 1, recovered_frame_count: 0, frames: [] } };
} };

(async () => {
  dom.window.eval(script);
  await new Promise((resolve) => setTimeout(resolve, 750));
  assert.equal(readCount, 2, 'successful empty reads should retry once after the page mounts');
  assert.match(dom.window.document.getElementById('app').textContent, /Loaded after mount/);
  console.log('browser-extension empty page retry: OK');
})().catch((error) => { console.error(error); process.exitCode = 1; });
