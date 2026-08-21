const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { JSDOM } = require('jsdom');

const root = path.resolve(__dirname, '..');
const html = fs.readFileSync(path.join(root, 'sidepanel.html'), 'utf8');
const script = fs.readFileSync(path.join(root, 'sidepanel.js'), 'utf8');
const dom = new JSDOM(html, { url: 'chrome-extension://trade-os/sidepanel.html', runScripts: 'outside-only' });
const wait = () => new Promise((resolve) => setTimeout(resolve, 10));
let savedPayload = null;

dom.window.chrome = {
  storage: { local: { get(_keys, callback) { callback({ apiBase: 'http://localhost:8080' }); }, async set() {} } },
  tabs: { create() {} },
  runtime: { getManifest() { return { version: 'test-version' }; } },
};
dom.window.TradeOSFrameReader = { async readPage() { return { ok: true, data: {
  channel: 'netease', platform: '网易邮箱', email: 'sales@example.com', phone: '',
  conversation_identity: 'Brad', adapter_version: 'netease-test', extraction_scope: '测试线程',
  warnings: [], messages: [{ fingerprint: 'm1', direction: 'inbound', text: 'Please quote.' }],
} }; } };

dom.window.fetch = async (url, options = {}) => {
  const pathname = new URL(url).pathname;
  let body;
  if (pathname === '/api/auth/me') body = { logged_in: true };
  else if (pathname === '/api/extension/match') body = {
    match_state: 'unique', exact_reason: '邮箱与现有联系人完全一致', domain_candidates: [], name_candidates: [],
    customers: [{ id: 1, company: 'Wrong Company' }],
    contacts: [{ id: 11, customer_id: 1, company: 'Wrong Company', name: 'Wrong Contact', email: 'sales@example.com' }],
  };
  else if (pathname === '/api/customers') body = { customers: [{ id: 2, company: 'Correct Company', country: 'AU' }] };
  else if (pathname === '/api/customers/2/contacts') body = [{ id: 22, customer_id: 2, name: 'Correct Contact', email: 'buyer@correct.example' }];
  else if (pathname === '/api/extension/communications') { savedPayload = JSON.parse(options.body); body = { new_message_count: 1, undo_token: 'undo-test' }; }
  else throw new Error(`Unexpected request: ${url}`);
  return { ok: true, status: 200, async json() { return body; } };
};

(async () => {
  dom.window.eval(script);
  await wait(); await wait();
  assert.match(dom.window.document.getElementById('match').textContent, /Wrong Company/);
  assert.match(dom.window.document.getElementById('match').textContent, /确认沟通归属/);
  assert.ok(dom.window.document.querySelector('[data-suggestion]'), 'automatic matches must be proposals, not silent assignments');
  const search = dom.window.document.getElementById('customer-search');
  search.value = 'Correct';
  search.dispatchEvent(new dom.window.Event('input', { bubbles: true }));
  await wait();
  dom.window.document.querySelector('[data-customer-result]').click();
  await wait();
  dom.window.document.querySelector('[data-contact-result]').click();
  const text = dom.window.document.getElementById('match').textContent;
  assert.match(text, /Correct Company/);
  assert.match(text, /Correct Contact/);
  assert.doesNotMatch(text, /Wrong Company/);
  assert.ok(dom.window.document.getElementById('change-assignment'));
  dom.window.document.getElementById('save').click();
  await wait();
  assert.equal(savedPayload.customer_id, 2);
  assert.equal(savedPayload.contact_id, 22);
  dom.window.document.getElementById('diagnostics').click();
  await wait();
  assert.match(dom.window.document.getElementById('app').textContent, /当前页验收/);
  assert.match(dom.window.document.getElementById('diagnostic-copy').value, /trade-os-capture-diagnostic-v1/);
  assert.doesNotMatch(dom.window.document.getElementById('diagnostic-copy').value, /sales@example\.com/);
  console.log('browser-extension manual customer/contact reassignment flow: OK');
})().catch((error) => { console.error(error); process.exitCode = 1; });
