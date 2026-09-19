// Regression harness: user-triggered writes share one global feedback surface and
// high-frequency CRM actions paint optimistically, then roll back on failure so the
// UI never keeps an unconfirmed change.
//
// Run standalone (needs node + jsdom):
//   node tests/support/action_feedback_check.cjs
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

// jsdom 默认不加载 <link> 样式；把真实样式表注入，才能断言 [hidden] 真的隐藏状态条。
for (const name of ['style.css', 'visual-v2.css']) {
  const style = doc.createElement('style');
  style.textContent = fs.readFileSync(path.join(staticDir, name), 'utf8');
  doc.head.appendChild(style);
}
win.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {} });
win.fetch = async () => ({ ok: true, status: 200, json: async () => ({}) });
win.eval(script);

const toasts = [];
win.showToast = (message, type) => { toasts.push({ message, type }); };
function lastToast() { return toasts[toasts.length - 1] || null; }
function wait(ms = 0) { return new Promise((resolve) => setTimeout(resolve, ms)); }

function deferred() {
  let resolve, reject;
  const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

function follow(id, content, isReported = false) {
  return {
    type: 'follow', id, follow_date: '2026-09-15', date: '2026-09-15', content,
    result: '', next_plan: '', activity_type: 'email', direction: 'outbound',
    is_reported: isReported,
  };
}

function actionStatus() { return doc.getElementById('actionStatus'); }
function actionStatusText() { return doc.getElementById('actionStatusText').textContent; }

function displayOf(el) { return win.getComputedStyle(el).display; }

// 状态条的基类显式设置了 display，必须用 [hidden] 规则补回 display:none，
// 否则进入页面时即便带 hidden 也会一直显示自带的“正在连接…/正在保存…”。
function checkStatusBarsRespectHidden() {
  const connection = doc.getElementById('connectionStatus');
  doc.body.appendChild(connection);
  doc.body.appendChild(actionStatus());
  assert.ok(actionStatus().hasAttribute('hidden'), 'action status starts hidden in the shell');
  assert.equal(displayOf(actionStatus()), 'none', 'hidden action status must not render');
  assert.equal(displayOf(connection), 'none', 'hidden connection status must not render');

  actionStatus().hidden = false;
  assert.equal(displayOf(actionStatus()), 'inline-flex', 'visible action status renders as a pill');
  actionStatus().hidden = true;
  assert.equal(displayOf(actionStatus()), 'none', 'action status hides again after finishing');
}

function checkUnifiedActionStatus() {
  win.resetActionStatus();
  win.beginActionStatus('a1', '正在保存客户…');
  assert.equal(actionStatus().hidden, false, 'pending status is visible');
  assert.ok(actionStatusText().includes('正在保存客户'), actionStatusText());

  win.beginActionStatus('a2', '正在保存联系人…');
  assert.ok(actionStatusText().includes('2 项'), 'concurrent actions aggregate: ' + actionStatusText());
  win.finishActionStatus('a1', 'success', '客户已保存');
  assert.equal(actionStatus().classList.contains('is-success'), false, 'success waits for the last pending action');
  win.finishActionStatus('a2', 'success', '联系人已保存');
  assert.equal(actionStatus().classList.contains('is-success'), true, 'success is shown once the last action settles');
  assert.equal(actionStatus().classList.contains('is-error'), false);

  // A later success must not hide an earlier failure among concurrent actions.
  win.beginActionStatus('c1', '正在保存 A…');
  win.beginActionStatus('c2', '正在保存 B…');
  win.finishActionStatus('c1', 'error', 'A 未保存');
  win.finishActionStatus('c2', 'success', 'B 已保存');
  assert.equal(actionStatus().classList.contains('is-error'), true, 'concurrent failure stays visible after a sibling succeeds');

  let retried = 0;
  win.beginActionStatus('e1', '正在删除…');
  win.finishActionStatus('e1', 'error', '删除未保存', () => { retried += 1; });
  assert.equal(actionStatus().classList.contains('is-error'), true, 'failure keeps a visible error state');
  const retry = doc.getElementById('actionStatusRetry');
  assert.equal(retry.hidden, false, 'failure offers a retry affordance');
  retry.click();
  assert.equal(retried, 1, 'retry re-runs the failed action');
  assert.equal(retry.hidden, true, 'retry resolves the error state');
}

function seedTimeline(items) {
  win._customerDetailCache = {
    id: 42, timeline_items: items.slice(), follow_history: items.slice(),
    outreach_emails: [], recent_facts: [], timeline_pagination: { has_next: false }, reminders: [],
  };
  win._customerWorkspaceCache = { 42: { summary: {}, timeline: { items: items.slice() }, savedAt: 0 } };
  win._customerTimelinePage = 2; // skip the background reconciliation read
  win.renderFollowTimeline(win._customerDetailCache.follow_history, []);
}

async function checkReportToggleOptimism() {
  win.resetActionStatus();
  seedTimeline([follow(1, '待确认沟通', false)]);
  const first = deferred();
  win.api = () => first.promise;
  const pending = win.toggleReport('follow', 1);
  assert.equal(win._customerDetailCache.timeline_items[0].is_reported, true,
    'the star flips immediately, before the server answers');
  first.resolve({ is_reported: true });
  await pending;
  assert.equal(win._customerDetailCache.timeline_items[0].is_reported, true);
  assert.equal(actionStatus().classList.contains('is-success'), true);

  // Failure must put the star back where the server still has it.
  win._customerDetailCache.timeline_items[0].is_reported = false;
  const second = deferred();
  win.api = () => second.promise;
  const failing = win.toggleReport('follow', 1);
  assert.equal(win._customerDetailCache.timeline_items[0].is_reported, true, 'optimistic flip before failure');
  second.reject(new Error('network down'));
  await failing;
  assert.equal(win._customerDetailCache.timeline_items[0].is_reported, false, 'failed toggle rolls back');
  assert.equal(actionStatus().classList.contains('is-error'), true);
}

async function checkContactDeleteRollback() {
  win.resetActionStatus();
  const contact = { id: 5, name: '张三', email: 'z@example.com', is_primary: true };
  win._customerDetailCache = {
    id: 42, contacts: [Object.assign({}, contact)], contact_count: 1,
    primary_contact: Object.assign({}, contact), timeline_items: [], recent_facts: [],
  };
  win.showAppConfirm = async () => true;
  win._customerTimelinePage = 2;
  const first = deferred();
  win.api = () => first.promise;
  const pending = win.deleteContact(5);
  await wait(0);
  assert.equal(win._customerDetailCache.contacts.length, 0, 'contact disappears immediately');
  first.reject(new Error('contact is referenced'));
  await pending;
  assert.equal(win._customerDetailCache.contacts.length, 1, 'rejected delete restores the contact');
  assert.equal(win._customerDetailCache.contacts[0].id, 5);
  assert.equal(actionStatus().classList.contains('is-error'), true);
}

async function checkContactAddFeedback() {
  win.resetActionStatus();
  win._customerDetailCache = {
    id: 42, contacts: [], contact_count: 0, primary_contact: null, timeline_items: [], recent_facts: [],
  };
  win._customerTimelinePage = 2;
  doc.getElementById('editCustomerId').value = '42';
  doc.getElementById('contactName').value = '新联系人';
  doc.getElementById('contactEmail').value = 'new@example.com';
  const first = deferred();
  win.api = () => first.promise;
  const pending = win.addContact();
  const button = doc.getElementById('addContactSubmit');
  assert.equal(button.classList.contains('is-pending'), true, 'add contact shows a pending button');
  assert.equal(button.disabled, true);
  first.resolve({ contact: { id: 9, name: '新联系人', email: 'new@example.com', is_primary: false }, merged: false });
  await pending;
  assert.equal(win._customerDetailCache.contacts.length, 1, 'saved contact is painted locally');
  assert.equal(actionStatus().classList.contains('is-success'), true);
}

async function checkBatchFailureIsNotSuccess() {
  win.resetActionStatus();
  win._inflightWrites = {};
  // selectedCustomers is a module-level `let`, so seed it through the real UI path.
  const body = doc.getElementById('customerTableBody');
  body.innerHTML = '<input type="checkbox" data-id="1" checked><input type="checkbox" data-id="2" checked>';
  win.updateSelection('existing');
  doc.getElementById('batchSetField').value = 'level';
  doc.getElementById('batchSetType').value = 'existing';
  doc.getElementById('batchSetValue').value = 'B';
  const first = deferred();
  win.api = () => first.promise;
  const pending = win.submitBatchSet();
  await wait(0);
  assert.equal(doc.getElementById('batchSetSubmit').classList.contains('is-pending'), true, 'batch shows pending');
  first.reject(new Error('validation failed'));
  await pending;
  assert.equal(actionStatus().classList.contains('is-error'), true, 'failed batch is an error');
  assert.ok(lastToast() && lastToast().message.includes('批量更新未保存'), 'failed batch is not reported as success');
  assert.equal(win._inflightWrites.batchSet, undefined, 'batch write guard is released on failure');
}

function checkNewPoolSelectionWorks() {
  const count = doc.getElementById('newPoolBatchCount');
  const checkbox = doc.createElement('input');
  checkbox.type = 'checkbox';
  checkbox.className = 'pool-cb';
  checkbox.dataset.id = '7';
  checkbox.checked = true;
  doc.body.appendChild(checkbox);
  win.updatePoolSelection();
  assert.equal(count.textContent.includes('1'), true, 'new pool selection tracks ids without a ReferenceError');
  checkbox.remove();
  win.updatePoolSelection();
}

async function run() {
  checkStatusBarsRespectHidden();
  checkUnifiedActionStatus();
  checkNewPoolSelectionWorks();
  await checkReportToggleOptimism();
  await checkContactDeleteRollback();
  await checkContactAddFeedback();
  await checkBatchFailureIsNotSuccess();
  console.log('action feedback regression: OK');
}

run().catch((error) => {
  console.error('action feedback regression: FAIL\n', error);
  process.exitCode = 1;
});
