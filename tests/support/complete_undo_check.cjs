// Regression harness: completing a task must be reversible from the UI.
//
// Completing a task (客户工作台「标记完成」/「完成这次跟进」/ Today「完成并记录」/ 批量完成)
// makes the customer leave Today. The server already returns an `undo_token` on
// completion, but the SPA used to drop it, so a mistaken 标记完成 could not be
// taken back. These checks pin the behaviour that fixes that:
//   * a completion response carrying undo_token renders a 撤销 action
//   * clicking 撤销 POSTs the token to /api/undo and refreshes Today
//   * a response without undo_token still shows the plain success toast
//   * 客户工作台「标记完成」uses the same undo wiring
//
// Run standalone (needs node + jsdom):
//   node tests/support/complete_undo_check.cjs
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

win.matchMedia = win.matchMedia || function () {
  return { matches: false, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {} };
};
win.fetch = async () => ({ ok: true, status: 200, json: async () => ({}) });
win.eval(script);

const tick = () => new Promise((resolve) => win.setTimeout(resolve, 0));
const raf = () => new Promise((resolve) => win.requestAnimationFrame(() => resolve()));

let calls = [];
let completeResponse = { task_id: 77, activity_id: 5, undo_token: 'undo-abc' };
win.api = async function (url, options) {
  calls.push({ url: String(url), method: String((options && options.method) || 'GET').toUpperCase() });
  if (String(url).indexOf('/api/undo/') === 0) return { success: true };
  if (String(url).indexOf('/api/reminders/') === 0) return completeResponse;
  return {};
};

let dashboardRefreshes = 0;
win.loadDashboard = function () { dashboardRefreshes += 1; };
win.refreshCustomerTimeline = async function () {};
win.refreshCustomerWorkspace = async function () {};
win.renderFollowTimeline = function () {};

function toastActionLabel() {
  const button = doc.querySelector('.toast-action');
  return button ? button.textContent : '';
}
function clearToasts() {
  doc.querySelectorAll('.toast').forEach((toast) => toast.remove());
}

function openFilled(overrides) {
  win.fillCompleteModal(Object.assign({
    id: 5,
    customer_id: 9,
    customer_name: 'Rehearsal Acrylic Co',
    task_title: '寄样跟进'
  }, overrides || {}));
}

let failures = 0;
async function check(label, body) {
  try {
    await body();
    console.log('  ok   - ' + label);
  } catch (error) {
    failures += 1;
    console.log('  FAIL - ' + label + '\n         ' + error.message);
  }
}

(async () => {
  console.log('scenario: 完成这次跟进 可撤销');
  await check('a completion with undo_token renders a 撤销 action', async () => {
    openFilled();
    clearToasts();
    calls = [];
    completeResponse = { task_id: 77, activity_id: 5, undo_token: 'undo-abc' };
    doc.getElementById('completeResult').value = '客户确认了样品';
    doc.getElementById('completeHasNext').checked = false;
    win.submitComplete();
    await tick();
    await tick();
    assert.ok(calls.some((call) => call.url.indexOf('/api/reminders/5') === 0 && call.method === 'PUT'), 'missing completion PUT');
    assert.equal(toastActionLabel(), '撤销', 'completion toast must carry a 撤销 action');
  });

  await check('clicking 撤销 POSTs the token and refreshes Today', async () => {
    dashboardRefreshes = 0;
    win.currentPage = 'dashboard';
    doc.getElementById('customerEditModal').classList.remove('show');
    calls = [];
    const button = doc.querySelector('.toast-action');
    assert.ok(button, 'no undo button to click');
    button.click();
    await tick();
    await tick();
    assert.ok(calls.some((call) => call.url === '/api/undo/undo-abc' && call.method === 'POST'),
      'undo action must POST the token to /api/undo');
    assert.ok(dashboardRefreshes >= 1, 'undoing a completion must refresh Today');
  });

  await check('a completion without undo_token shows the plain toast and no 撤销', async () => {
    openFilled();
    clearToasts();
    completeResponse = { task_id: 77, activity_id: 5 };
    doc.getElementById('completeResult').value = '没有撤销令牌';
    doc.getElementById('completeHasNext').checked = false;
    win.submitComplete();
    await tick();
    await tick();
    const toast = doc.querySelector('.toast');
    assert.ok(toast && /已记录/.test(toast.textContent), 'missing 已记录 toast');
    assert.equal(doc.querySelector('.toast-action'), null, 'no undo action expected without a token');
  });

  console.log('scenario: 客户工作台 标记完成 可撤销');
  await check('completeCustomerNextTask surfaces the same undo action', async () => {
    const task = { id: 7, title: '寄样跟进', remind_date: '2026-10-01', is_done: 0 };
    win._customerDetailCache = { id: 42, tasks: [task], reminders: [task], recent_facts: [] };
    win._customerWorkspaceCache = {};
    doc.getElementById('editCustomerId').value = '42';
    doc.getElementById('customerEditModal').classList.add('show');
    win.currentPage = 'customers';
    clearToasts();
    calls = [];
    completeResponse = { activity_id: 500, undo_token: 'undo-task' };
    await win.completeCustomerNextTask();
    await raf();
    assert.ok(calls.some((call) => call.url === '/api/reminders/7' && call.method === 'PUT'), 'missing completion PUT');
    assert.equal(toastActionLabel(), '撤销', '客户工作台 completion must carry a 撤销 action');
  });

  if (failures) {
    console.error('\ncomplete undo regression: ' + failures + ' check(s) failed');
    process.exit(1);
  }
  console.log('\ncomplete undo regression: OK');
  process.exit(0);
})();
