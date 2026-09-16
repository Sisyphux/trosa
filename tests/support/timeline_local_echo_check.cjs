// Regression harness: customer timeline writes are visible before reconciliation.
//
// Run standalone (needs node + jsdom):
//   node tests/support/timeline_local_echo_check.cjs
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
const css = fs.readFileSync(path.join(staticDir, 'visual-v2.css'), 'utf8');
const dom = new JSDOM(html, { url: 'http://localhost/', runScripts: 'outside-only', pretendToBeVisual: true });
const win = dom.window;
const doc = win.document;
win.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {} });
win.fetch = async () => ({ ok: true, status: 200, json: async () => ({}) });
win.eval(script);

function follow(id, content, date = '2026-09-15') {
  return {
    type: 'follow', id, follow_date: date, date, content,
    result: '', next_plan: '', activity_type: 'email', direction: 'outbound', is_reported: false,
  };
}

function seed(items) {
  win._customerWorkspaceCache = {};
  win._customerTimelinePage = 1;
  win._customerDetailCache = {
    id: 42, timeline_items: items.slice(), follow_history: items.slice(), outreach_emails: [],
    recent_facts: items.map((item) => ({ type: 'follow', id: item.id, date: item.date, content: item.content })),
    timeline_pagination: { has_next: false }, reminders: [],
  };
  win.renderFollowTimeline(items, []);
}

function timelineRow(id) {
  return doc.querySelector('.tl-item[data-motion-key="follow-' + id + '"]');
}

function wait(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function run() {
  assert.ok(css.includes('.motion-list-leave'));
  assert.ok(css.includes('.motion-updated'));
  assert.ok(css.includes('@keyframes trade-os-list-leave'));
  assert.ok(css.includes('@keyframes trade-os-list-confirm'));

  seed([follow(1, '旧记录')]);
  assert.equal(timelineRow(1).textContent.includes('旧记录'), true);

  win.upsertCustomerTimelineEntry(follow(2, '刚保存的记录', '2026-09-16'));
  const added = timelineRow(2);
  assert.ok(added, 'saved record should be painted immediately');
  assert.ok(added.classList.contains('motion-list-enter'));
  assert.ok(added.classList.contains('motion-updated'));
  // A fast follow-up GET must not erase the transient confirmation state.
  win.renderFollowTimeline(win._customerDetailCache.follow_history, []);
  assert.ok(timelineRow(2).classList.contains('motion-list-enter'));
  assert.ok(timelineRow(2).classList.contains('motion-updated'));

  win.upsertCustomerTimelineEntry(follow(2, '刚编辑的记录', '2026-09-16'));
  const edited = timelineRow(2);
  assert.equal(edited.textContent.includes('刚编辑的记录'), true);
  assert.ok(edited.classList.contains('motion-updated'));

  win.removeCustomerTimelineEntry('follow', 2);
  const leaving = doc.querySelector('.tl-item[data-motion-key="follow-2"]');
  assert.ok(leaving, 'removed record remains for the exit animation');
  assert.ok(leaving.classList.contains('motion-list-leave'));
  assert.equal(leaving.hasAttribute('data-motion-leaving'), true);
  await wait(340);
  assert.equal(timelineRow(2), null, 'removed record should be cleaned up after exit');

  seed([follow(3, '最后一条')]);
  win.removeCustomerTimelineEntry('follow', 3);
  assert.ok(doc.querySelector('.tl-item[data-motion-key="follow-3"]'));
  // A fast reconciliation returning the same empty result must still let the
  // already-started exit finish instead of clearing the node immediately.
  win.renderFollowTimeline([], []);
  assert.ok(doc.querySelector('.tl-item[data-motion-key="follow-3"]'));
  await wait(380);
  assert.ok(doc.querySelector('#outreachList .empty-state'), 'empty state should follow the exit animation');

  // Exercise the actual edit handler with a durable response, not only the
  // lower-level cache helper. The reconciliation responses are intentionally
  // immediate here to catch a fast GET cancelling the confirmation state.
  seed([Object.assign(follow(4, '编辑前'), { customer_id: 42 })]);
  doc.getElementById('editCustomerId').value = '42';
  doc.getElementById('customerEditModal').classList.add('show');
  doc.getElementById('followEditId').value = '4';
  doc.getElementById('followEditDate').value = '2026-09-16';
  doc.getElementById('followEditType').value = 'email';
  doc.getElementById('followEditDirection').value = 'outbound';
  doc.getElementById('followEditContent').innerHTML = '编辑后';
  doc.getElementById('followEditResult').innerHTML = '';
  doc.getElementById('followEditNextPlan').innerHTML = '';
  win._followCache = Object.assign({}, follow(4, '编辑前'), { customer_id: 42 });
  const previousApi = win.api;
  win.api = async (url) => {
    if (url === '/api/follow-history/4') return Object.assign(follow(4, '编辑后', '2026-09-16'), { customer_id: 42 });
    if (url.includes('/timeline?')) return { items: [Object.assign(follow(4, '编辑后', '2026-09-16'), { customer_id: 42 })], pagination: { has_next: false } };
    if (url.endsWith('/summary')) return { id: 42, recent_facts: [{ type: 'follow', id: 4, date: '2026-09-16', content: '编辑后' }], reminders: [] };
    if (url.endsWith('/tasks')) return { tasks: [] };
    return {};
  };
  await win.saveFollowEdit();
  assert.ok(timelineRow(4).textContent.includes('编辑后'), 'edit handler should paint the saved response immediately');
  assert.ok(timelineRow(4).classList.contains('motion-updated'));
  win.api = previousApi;

  console.log('timeline local echo regression: OK');
}

run().catch((error) => {
  console.error('timeline local echo regression: FAIL\n', error);
  process.exitCode = 1;
});
