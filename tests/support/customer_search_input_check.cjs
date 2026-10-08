// Regression harness: typing in the Customer search box.
//
// 1. A pinyin/kana IME fills the box with half-typed syllables ("a si ta") while
//    composing; that must not fire a search, only the committed text may.
// 2. Enter with a query opens the best match (first row of the ranked list);
//    Enter with an empty box only moves focus into the list.
// 3. Highlighting ignores stray single letters when the query has real words.
//
// Run standalone (needs node + jsdom):
//   node tests/support/customer_search_input_check.cjs
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const root = path.resolve(__dirname, '..', '..');
const staticDir = path.join(root, 'app', 'static');

function loadJsdom() {
  const candidates = [path.join(root, 'browser-extension', 'node_modules', 'jsdom'), 'jsdom'];
  for (const candidate of candidates) {
    try {
      return require(candidate);
    } catch (error) {
      if (error.code !== 'MODULE_NOT_FOUND') throw error;
    }
  }
  console.error('jsdom 未安装：请在 browser-extension 目录执行 npm install');
  process.exit(2);
}

const { JSDOM } = loadJsdom();
const html = fs.readFileSync(path.join(staticDir, 'index.html'), 'utf8').replace(/\{\{VERSION\}\}/g, 'test');
const script = fs.readFileSync(path.join(staticDir, 'app.js'), 'utf8');
const dom = new JSDOM(html, { url: 'http://localhost/', runScripts: 'outside-only', pretendToBeVisual: true });
const win = dom.window;
const doc = win.document;
const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

win.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {} });
win.fetch = async () => ({ ok: true, status: 200, json: async () => ({}) });
win.eval(script);

(async () => {
  // loadLedger/ledgerOpen are global function bindings the input handlers call by name.
  win.eval(`
    var __searches = [];
    var __opened = [];
    loadLedger = function() {
      __searches.push(getCustomerSearchQuery());
      LD.query = getCustomerSearchQuery();
      LD.state = 'ok';
      LD.order = [901, 902];
      return Promise.resolve();
    };
    ledgerOpen = function(id) { __opened.push(id); };
  `);
  win.ledgerInit();
  const input = doc.getElementById('ledgerSearch');
  const fire = (type, init) => input.dispatchEvent(new (type.startsWith('composition') ? win.CompositionEvent : win.InputEvent)(type, Object.assign({ bubbles: true }, init)));
  const searches = () => JSON.parse(win.eval('JSON.stringify(__searches)'));
  const opened = () => JSON.parse(win.eval('JSON.stringify(__opened)'));

  // IME: nothing while composing, one search on commit.
  fire('compositionstart');
  for (const value of ['a', 'a s', 'a si', 'a si ta']) {
    input.value = value;
    fire('input', { isComposing: true });
    await wait(260);
  }
  assert.deepEqual(searches(), [], 'half-typed IME syllables must not trigger a search');
  input.value = '阿斯塔';
  fire('compositionend');
  await wait(260);
  assert.deepEqual(searches(), ['阿斯塔'], 'the committed text is searched exactly once');

  // Plain typing is still debounced into one search.
  for (const value of ['o', 'oc', 'ocean']) {
    input.value = value;
    fire('input');
    await wait(40);
  }
  await wait(300);
  assert.deepEqual(searches(), ['阿斯塔', 'ocean']);

  // Enter while the IME is still choosing candidates does nothing.
  const key = (init) => input.dispatchEvent(new win.KeyboardEvent('keydown', Object.assign({ bubbles: true, cancelable: true }, init)));
  key({ key: 'Enter', isComposing: true });
  key({ key: 'Enter', keyCode: 229 });
  await wait(30);
  assert.deepEqual(opened(), []);

  // Enter with a query opens the top (most relevant) customer.
  key({ key: 'Enter' });
  await wait(30);
  assert.deepEqual(opened(), [901], 'Enter opens the best match');

  // Enter with an empty box only enters the list.
  input.value = '';
  key({ key: 'Enter' });
  await wait(30);
  assert.deepEqual(opened(), [901]);

  // Highlighting: lone letters are noise when the query has real words.
  assert.equal(win.highlightSearchText('Delta Silver GmbH', 'a si ta'),
    'Del<mark class="search-hit">ta</mark> <mark class="search-hit">Si</mark>lver GmbH');
  assert.equal(win.highlightSearchText('Alpha', 'a'), '<mark class="search-hit">A</mark>lph<mark class="search-hit">a</mark>');
  console.log('customer search input regression: OK');
  process.exit(0);
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
