// Architecture invariant check: the frontend's async-write discipline is
// enforced on the actually-evaluated code, not on regex guesses over source.
//
// The mechanisms under test (see app.js):
//   * customer workspace scope  — every post-await write to the shared
//     workspace state must capture a scope before awaiting and touch the state
//     only through liveCustomerCache(scope). A plain id comparison has a hole:
//     closing and reopening the same customer yields the same id, so a stale
//     response would overwrite the fresh load. Generation closes that hole.
//   * transport single-flight   — api() dedupes concurrent identical non-GET
//     requests, so duplicate submission is prevented for every current and
//     future write entry point, not only the wired-up buttons.
//   * entity-bound dialogs      — the next-step dialog writes to the customer
//     captured when it opened, never re-reading the live DOM at submit time.
//   * confirm dialogs           — a new dialog settles the superseded request
//     with "cancel" instead of orphaning its promise forever.
//
// Run standalone (needs node + jsdom):
//   node tests/support/frontend_architecture_check.cjs
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
win.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {} });
win.fetch = async () => ({ ok: true, status: 200, json: async () => ({}) });
win.eval(script);

// Top-level declarations become global bindings, so the evaluated code can be
// introspected with Function.prototype.toString().
const globalFns = new Map();
for (const name of Object.getOwnPropertyNames(win)) {
  let value;
  try { value = win[name]; } catch (e) { continue; }
  if (typeof value !== 'function') continue;
  const source = Function.prototype.toString.call(value);
  if (!source.startsWith('function ') && !source.startsWith('async function ')) continue;
  if (/\[native code\]/.test(source)) continue;
  globalFns.set(name, source);
}

const SHARED_WORKSPACE_TOKENS = [
  '_customerDetailCache',
  'upsertCustomerTimelineEntry',
  'removeCustomerTimelineEntry',
  'applyCustomerTaskSnapshot',
  'syncCustomerWorkspaceAfterMutation',
  'syncCustomerWorkspaceAfterCommunication',
  'syncCustomerWorkspaceAfterOutreach',
  'patchCustomerWorkspaceContact',
  'liveCustomerCache',
  'renderFollowTimeline',
  'renderCustomerTasks',
  'renderCustomerFactsBrief',
  'renderContacts',
  'renderCustomerFiles',
  'renderCustomerNextTask',
];
const ENTITY_WRITE_URLS = [
  "/api/customers/",
  "/api/reminders/",
  "/api/follow-history/",
  "/api/contacts/",
  "/api/outreach/",
];
// The scope owner itself: openEditModal carries its own request token and is
// the place that bumps the generation.
const SCOPE_LINT_ALLOWLIST = new Set(['openEditModal']);

let failures = 0;
function check(label, body) {
  try {
    body();
    console.log('  ok   - ' + label);
  } catch (error) {
    failures += 1;
    console.log('  FAIL - ' + label + '\n         ' + error.message);
  }
}

function assertAsyncCustomerWritesAreIdentityBound() {
  const offenders = [];
  for (const [name, source] of globalFns) {
    if (!source.startsWith('async function ')) continue;
    if (SCOPE_LINT_ALLOWLIST.has(name)) continue;
    let firstAwait = -1;
    for (const url of ENTITY_WRITE_URLS) {
      const at = source.indexOf("await api('" + url);
      if (at !== -1 && (firstAwait === -1 || at < firstAwait)) firstAwait = at;
    }
    if (firstAwait === -1) continue;
    const writesSharedState = SHARED_WORKSPACE_TOKENS.some((token) => source.slice(firstAwait).includes(token));
    if (writesSharedState && !source.includes('beginCustomerScope(')) offenders.push(name);
  }
  assert.deepEqual(offenders, [],
    '这些 async 函数在 await 之后再写共享客户工作区状态，必须先用 beginCustomerScope() 捕获身份，' +
    'await 后用 liveCustomerCache(scope) 触碰状态（不要直接比较 id：同一客户关闭后重开 id 相同）');
}

check('scope lint: every async customer write captures beginCustomerScope', assertAsyncCustomerWritesAreIdentityBound);

check('workspace scope generation is bumped on open and close', () => {
  assert.ok(globalFns.has('openEditModal'), 'openEditModal must be a top-level declaration');
  assert.ok(globalFns.has('closeModal'), 'closeModal must be a top-level function');
  assert.ok(globalFns.get('openEditModal').includes('_customerScopeGeneration++'));
  assert.ok(globalFns.get('closeModal').includes('_customerScopeGeneration++'));
});

check('api() single-flights concurrent identical mutations', () => {
  assert.ok(globalFns.has('api') && globalFns.has('apiOnce'), 'api must delegate to apiOnce');
  assert.ok(globalFns.get('api').includes('_pendingMutations'), 'api must consult the single-flight map');
  assert.ok(globalFns.get('api').includes("method === 'GET'"), 'single-flight must only gate non-GET requests');
  assert.ok(globalFns.get('api').includes('mutationKey'), 'single-flight must key on method + url + body');
});

check('the next-step dialog writes to the customer captured at open', () => {
  assert.ok(globalFns.has('openCustomerTaskModal') && globalFns.has('createCustomerTask'));
  assert.ok(globalFns.get('openCustomerTaskModal').includes('modal.dataset.customerId'),
    'openCustomerTaskModal must stamp the owning customer');
  const create = globalFns.get('createCustomerTask');
  assert.ok(create.includes('modal.dataset.customerId'),
    'createCustomerTask must write to the customer captured when the dialog opened');
});

check('confirm dialogs settle superseded requests instead of orphaning them', () => {
  for (const name of ['showAppPrompt', 'showAppConfirm']) {
    assert.ok(globalFns.has(name), name + ' must exist');
    assert.ok(globalFns.get(name).includes('if (_appDialogResolver) finishAppDialog('),
      name + ' must settle the superseded dialog with cancel');
  }
});

check('the live-customer gate is the only sanctioned post-await entry point', () => {
  assert.ok(globalFns.has('beginCustomerScope') && globalFns.has('liveCustomerCache'));
  // Every previously audited write path must now go through the gate.
  for (const name of [
    'refreshCustomerWorkspace', 'refreshCustomerTimeline', 'loadMoreCustomerTimeline',
    'completeCustomerNextTask', 'postponeCustomerNextTask', 'createCustomerTask',
    'saveContact', 'addContact', 'addBulkContacts', 'deleteContact',
    'uploadCustomerFiles', 'deleteCustomerFile', 'loadCustomerSection',
    'addFollowHistory', 'addOutreach', 'deleteOutreach', 'deleteFollowLog',
    'toggleReport', 'saveTimelineHighlight', 'saveFollowEdit', 'saveInboxReply',
    'quickUpdateCustomerLevel', 'saveCustomer', 'editCustomerWaiting',
  ]) {
    assert.ok(globalFns.has(name), name + ' must exist');
    assert.ok(globalFns.get(name).includes('liveCustomerCache(') || globalFns.get(name).includes('beginCustomerScope('),
      name + ' must gate its post-await workspace writes through liveCustomerCache(scope)');
  }
});

if (failures) {
  console.error('\nfrontend architecture invariants: ' + failures + ' check(s) failed');
  process.exit(1);
}
console.log('\nfrontend architecture invariants: OK');
process.exit(0);
