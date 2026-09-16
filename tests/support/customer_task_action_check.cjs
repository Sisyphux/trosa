// Regression harness: the Customer action edits the current open task instead
// of leaving the old task in Today, and the saved task is visible in the panel.
//
// Run standalone (needs node + jsdom):
//   node tests/support/customer_task_action_check.cjs
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const root = path.resolve(__dirname, '..', '..');
const staticDir = path.join(root, 'app', 'static');
const { JSDOM } = require(path.join(root, 'browser-extension', 'node_modules', 'jsdom'));
const html = fs.readFileSync(path.join(staticDir, 'index.html'), 'utf8').replace(/\{\{VERSION\}\}/g, 'test');
const script = fs.readFileSync(path.join(staticDir, 'app.js'), 'utf8');
const dom = new JSDOM(html, { url: 'http://localhost/', runScripts: 'outside-only', pretendToBeVisual: true });
const win = dom.window;
const doc = win.document;
win.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {}, addListener() {} });
win.fetch = async () => ({ ok: true, status: 200, json: async () => ({}) });
win.eval(script);

const oldTask = { id: 7, title: '旧动作', content: '旧动作', remind_date: '2026-09-20', is_done: 0 };
win._customerDetailCache = {
  id: 42, tasks: null, reminders: [oldTask], next_task: oldTask,
  next_follow_up: oldTask.remind_date, current_next_step: {},
};
win._customerWorkspaceCache = {
  42: { summary: { id: 42, next_task: oldTask, next_follow_up: oldTask.remind_date }, savedAt: Date.now() },
};
win._agentTaskProposalId = null;
win.currentPage = 'customers';
doc.getElementById('editCustomerId').value = '42';
doc.getElementById('customerEditTitle').textContent = '测试客户';

win.openCustomerTaskModal();
assert.equal(doc.getElementById('customerTaskTitle').value, '旧动作');
assert.equal(doc.getElementById('customerTaskModalTitle').textContent, '调整下一步');
assert.equal(doc.getElementById('customerTaskSubmit').textContent, '保存下一步');

doc.getElementById('customerTaskTitle').value = '新动作';
doc.getElementById('customerTaskDate').value = '2026-09-27';
let request;
win.api = async (url, options) => {
  request = { url, options };
  return {
    success: true,
    reminder: { id: 7, title: '新动作', content: '新动作', remind_date: '2026-09-27', is_done: 0 },
  };
};

win.createCustomerTask(doc.getElementById('customerTaskSubmit')).then(() => {
  assert.equal(request.url, '/api/reminders/7');
  assert.equal(request.options.method, 'PATCH');
  assert.equal(JSON.parse(request.options.body).title, '新动作');
  assert.equal(win._customerDetailCache.reminders.length, 1);
  assert.equal(win._customerDetailCache.reminders[0].title, '新动作');
  assert.equal(doc.getElementById('customerNextTask').hidden, false);
  assert.ok(doc.getElementById('customerNextTask').textContent.includes('新动作'));
  assert.equal(win._customerWorkspaceCache[42].summary.next_task.title, '新动作');
  console.log('customer task action regression: OK');
}).catch((error) => {
  console.error('customer task action regression: FAIL\n', error);
  process.exitCode = 1;
});
