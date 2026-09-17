// Regression harness: a late response for customer A must never paint or mutate
// the workspace of customer B.
//
// The Customer workspace is a single overlay backed by the shared
// `_customerDetailCache`. Several post-write reads (timeline refresh, workspace
// refresh, older-page paging, task complete/postpone, contact patches, file
// uploads, follow edits) awaited the network and then wrote that shared cache
// without re-checking which customer was actually open. On a slow tunnel the
// user could close A, open B, and see A's timeline/tasks/contacts appear under
// B; an edit started on A could even be merged into B.
//
// The same file also guards the no-answer-exit rules: a contenteditable draft
// must count as an unsaved change, "保存并退出" must actually exit, and a
// double-click on a write button must not create duplicate records.
//
// Run standalone (needs node + jsdom):
//   node tests/support/customer_context_race_check.cjs
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

function wait(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

win.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {} });
win.fetch = async () => ({ ok: true, status: 200, json: async () => ({}) });
win.eval(script);

// Deferred API stub: tests decide when a request resolves, so a "switch customer
// while the request is still in flight" race is deterministic, not timing-based.
// The transport-level single-flight test needs the real api(), so keep the
// original binding around (function declarations are writable, not deletable).
const realApi = win.api;

function installDeferredApi() {
  const pending = [];
  win.api = function (url, options) {
    const deferred = {};
    const promise = new Promise((resolve, reject) => { deferred.resolve = resolve; deferred.reject = reject; });
    pending.push({ url: String(url), options: options || {}, ...deferred });
    return promise;
  };
  win.__pendingApi = pending;
  return pending;
}

function restoreRealApi() {
  if (win.__pendingApi) delete win.__pendingApi;
  win.api = realApi;
}

function settle(pattern, value) {
  const match = win.__pendingApi.filter((item) => item.url.indexOf(pattern) === 0 && !item.done)[0];
  if (!match) throw new Error('no pending request matching ' + pattern);
  match.done = true;
  match.resolve(value);
  return match;
}

function settleAll(value) {
  win.__pendingApi.forEach((item) => {
    if (!item.done) { item.done = true; item.resolve(value); }
  });
}

function follow(id, content, customerId, date) {
  return {
    type: 'follow', id: id, follow_date: date || '2026-09-16', date: date || '2026-09-16',
    content: content, result: '', next_plan: '', activity_type: 'email',
    direction: 'outbound', is_reported: false, customer_id: customerId,
  };
}

function seedCustomer(id, timelineItems, reminders) {
  win._customerWorkspaceCache = {};
  win._customerTimelinePage = 1;
  win._customerTimelineLoading = false;
  win._customerSectionLoads = {};
  win._customerDetailCache = {
    id: id,
    timeline_items: (timelineItems || []).slice(),
    follow_history: (timelineItems || []).filter((item) => item.type === 'follow'),
    outreach_emails: (timelineItems || []).filter((item) => item.type === 'outreach'),
    timeline_pagination: { has_next: false },
    reminders: (reminders || []).slice(),
    recent_facts: [],
  };
  doc.getElementById('editCustomerId').value = String(id);
  win.renderFollowTimeline(win._customerDetailCache.follow_history, win._customerDetailCache.outreach_emails);
}

function cacheIds() {
  return (win._customerDetailCache.timeline_items || []).map((item) => item.type + '-' + item.id);
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

async function run() {
  console.log('scenario: a response for A must not overwrite B');

  await check('refreshCustomerTimeline ignores a late response after switching to B', async () => {
    seedCustomer(1, [follow(11, 'A-旧', 1)]);
    installDeferredApi();
    const request = win.refreshCustomerTimeline();
    // The user closes A and opens B before the read returns.
    seedCustomer(2, [follow(22, 'B-自己的记录', 2)]);
    settle('/api/customers/1/timeline', { items: [follow(11, 'A-新', 1)], pagination: { has_next: false } });
    await request;
    assert.equal(win._customerDetailCache.id, 2);
    assert.deepEqual(cacheIds(), ['follow-22'], 'B timeline must not receive A data');
  });

  await check('refreshCustomerWorkspace ignores a late response after switching to B', async () => {
    seedCustomer(1, [follow(11, 'A', 1)], [{ id: 71, title: 'A 的下一步', remind_date: '2026-10-01' }]);
    installDeferredApi();
    const request = win.refreshCustomerWorkspace();
    seedCustomer(2, [follow(22, 'B', 2)], [{ id: 92, title: 'B 的下一步', remind_date: '2026-11-01' }]);
    settle('/api/customers/1/summary', { id: 1, name: '客户A', next_task: { id: 71, title: 'A 的下一步' }, recent_facts: [], reminders: [] });
    settle('/api/customers/1/tasks', { tasks: [{ id: 71, title: 'A 的下一步', remind_date: '2026-10-01' }] });
    await request;
    assert.equal(win._customerDetailCache.id, 2);
    assert.equal(win._customerDetailCache.name, undefined, 'B cache must not be overwritten with A summary');
    assert.deepEqual(win._customerDetailCache.reminders.map((t) => t.id), [92], 'B reminders must survive');
  });

  await check('loadMoreCustomerTimeline keeps older A pages out of B', async () => {
    seedCustomer(1, [follow(11, 'A-首页', 1)]);
    win._customerDetailCache.timeline_pagination = { has_next: true };
    installDeferredApi();
    const request = win.loadMoreCustomerTimeline();
    seedCustomer(2, [follow(22, 'B-首页', 2)]);
    settle('/api/customers/1/timeline?page=2', { items: [follow(12, 'A-更早', 1)], pagination: { has_next: false } });
    await request;
    assert.deepEqual(cacheIds(), ['follow-22'], 'B timeline must not gain A older pages');
    assert.equal(win._customerTimelinePage, 1, 'paging pointer must not advance for B');
  });

  await check('completeCustomerNextTask does not inject A activity into B', async () => {
    seedCustomer(1, [follow(11, 'A', 1)], [{ id: 71, title: 'A 的下一步', remind_date: '2026-10-01' }]);
    installDeferredApi();
    const request = win.completeCustomerNextTask();
    seedCustomer(2, [follow(22, 'B', 2)], [{ id: 92, title: 'B 的下一步', remind_date: '2026-11-01' }]);
    settle('/api/reminders/71', { activity_id: 500 });
    await request;
    assert.deepEqual(cacheIds(), ['follow-22'], 'B timeline must not show A task-completion');
    assert.equal(win._customerDetailCache.id, 2);
  });

  await check('postponeCustomerNextTask does not move A task into B', async () => {
    seedCustomer(1, [follow(11, 'A', 1)], [{ id: 71, title: 'A 的下一步', remind_date: '2026-10-01' }]);
    installDeferredApi();
    const request = win.postponeCustomerNextTask(1);
    seedCustomer(2, [follow(22, 'B', 2)], [{ id: 92, title: 'B 的下一步', remind_date: '2026-11-01' }]);
    settle('/api/reminders/71/reschedule', { reminder: { id: 71, title: 'A 的下一步', remind_date: '2026-10-02' } });
    await request;
    assert.deepEqual(win._customerDetailCache.reminders.map((t) => t.id), [92], 'A task must not enter B reminders');
  });

  await check('saveFollowEdit belongs to the record owner, not the open customer', async () => {
    seedCustomer(1, [follow(41, 'A 的记录', 1)]);
    win._followCache = follow(41, 'A 的记录', 1);
    doc.getElementById('followEditId').value = '41';
    doc.getElementById('followEditDate').value = '2026-09-16';
    doc.getElementById('followEditType').value = 'email';
    doc.getElementById('followEditDirection').value = 'outbound';
    doc.getElementById('followEditContent').innerHTML = 'A 编辑后';
    doc.getElementById('followEditResult').innerHTML = '';
    doc.getElementById('followEditNextPlan').innerHTML = '';
    doc.getElementById('customerEditModal').classList.add('show');
    installDeferredApi();
    const request = win.saveFollowEdit();
    seedCustomer(2, [follow(22, 'B 的记录', 2)]);
    doc.getElementById('customerEditModal').classList.add('show');
    settle('/api/follow-history/41', follow(41, 'A 编辑后', 1));
    await request;
    assert.deepEqual(cacheIds(), ['follow-22'], 'A edited record must not appear in B timeline');
  });

  await check('uploadCustomerFiles does not prepend A files into B', async () => {
    seedCustomer(1, []);
    win._customerDetailCache.files = [{ id: 1, filename: 'b-exists.txt' }];
    doc.getElementById('editCustomerId').value = '1';
    installDeferredApi();
    // The upload path uses raw fetch, not api(), so exercise the cache guard
    // through the same id capture by faking the fetch.
    const formFiles = { length: 1, 0: { name: 'a.txt', size: 10 }, item: (i) => (i === 0 ? { name: 'a.txt', size: 10 } : null) };
    doc.getElementById('customerFileInput'); // present in DOM
    const input = doc.getElementById('customerFileInput');
    Object.defineProperty(input, 'files', { value: formFiles, configurable: true });
    const previousFetch = win.fetch;
    const previousFormData = win.FormData;
    win.FormData = function () { this.append = function () {}; };
    let uploadResolve;
    win.fetch = () => new Promise((resolve) => { uploadResolve = resolve; });
    const request = win.uploadCustomerFiles();
    seedCustomer(2, []);
    win._customerDetailCache.files = [{ id: 2, filename: 'b-only.txt' }];
    uploadResolve({ ok: true, status: 200, json: async () => ({ created: [{ id: 900, filename: 'a.txt' }], rejected: [] }) });
    await request;
    win.fetch = previousFetch;
    win.FormData = previousFormData;
    assert.deepEqual(win._customerDetailCache.files.map((f) => f.filename), ['b-only.txt'], 'A files must not enter B');
  });

  console.log('scenario: unsaved changes, exit, and duplicate writes');

  await check('a contenteditable draft counts as an unsaved change', async () => {
    win.markModalClean('followEditModal');
    doc.getElementById('followEditContent').innerHTML = '只在富文本里写了内容';
    assert.equal(win.customerModalIsDirty('followEditModal'), true, 'contenteditable edit must be protected');
    doc.getElementById('followEditContent').innerHTML = '';
    win.markModalClean('followEditModal');
  });

  await check('saveCustomerWorkspaceAndExit actually closes the workspace', async () => {
    seedCustomer(7, [follow(70, '记录', 7)]);
    doc.getElementById('editName').value = '客户7';
    win.openModal('customerEditModal');
    win.api = async (url) => {
      const text = String(url);
      if (text.indexOf('/api/reminders/today') === 0 || text.indexOf('/api/reminders/upcoming') === 0) return [];
      if (text.indexOf('/api/my-weekly-logs') === 0) return [];
      return { ok: true };
    };
    const result = await win.saveCustomerWorkspaceAndExit();
    assert.equal(result, true);
    assert.equal(doc.getElementById('customerEditModal').classList.contains('show'), false, '保存并退出 must close the modal');
  });

  await check('double-clicking 只保存记录 sends one write', async () => {
    const writes = [];
    win.api = async (url, options) => {
      const text = String(url);
      if (text.indexOf('/api/reminders/today') === 0 || text.indexOf('/api/reminders/upcoming') === 0) return [];
      if (text.indexOf('/api/reminders/') === 0 && options && options.method === 'PUT') {
        writes.push(url);
        await wait(5);
        return { attention: '' };
      }
      return {};
    };
    doc.getElementById('completeReminderId').value = '5';
    doc.getElementById('completeResult').value = '客户确认';
    doc.getElementById('completeHasNext').checked = false;
    win.currentPage = 'dashboard';
    await Promise.all([win.submitComplete(), win.submitComplete()]);
    assert.equal(writes.length, 1, 'one user intent must produce one write, got ' + writes.length);
  });

  await check('double-clicking 添加客户 sends one write', async () => {
    const writes = [];
    win.api = async (url, options) => {
      const text = String(url);
      if (text.indexOf('/api/reminders/today') === 0 || text.indexOf('/api/reminders/upcoming') === 0) return [];
      if (text.indexOf('/api/my-weekly-logs') === 0) return [];
      if (text === '/api/customers' && options && options.method === 'POST') {
        writes.push(url);
        await wait(5);
        return { id: 999 };
      }
      return {};
    };
    doc.getElementById('newCustomerName').value = '重复提交客户';
    win.currentPage = 'dashboard';
    await Promise.all([win.submitNewCustomer(), win.submitNewCustomer()]);
    assert.equal(writes.length, 1, 'one user intent must produce one write, got ' + writes.length);
  });

  await check('double-clicking 创建下一步 sends one write', async () => {
    seedCustomer(1, [follow(11, 'A', 1)], []);
    const writes = [];
    win.api = async (url, options) => {
      const text = String(url);
      if (/^\/api\/customers\/1\/tasks$/.test(text) && options && options.method === 'POST') {
        writes.push(text);
        await wait(5);
        return { task: { id: 88, title: '新下一步', remind_date: '2026-10-10', is_done: 0 } };
      }
      return {};
    };
    doc.getElementById('customerTaskTitle').value = '新下一步';
    doc.getElementById('customerTaskDate').value = '2026-10-10';
    // No button argument, exactly like the unsaved-changes 保存并退出 path.
    await Promise.all([win.createCustomerTask(), win.createCustomerTask()]);
    assert.equal(writes.length, 1, 'one user intent must produce one next-step write, got ' + writes.length);
  });

  console.log('scenario: the mechanisms behind the fixes');

  await check('a stale response after closing and reopening the SAME customer is dropped', async () => {
    seedCustomer(1, [follow(11, 'A-旧', 1)]);
    win.openModal('customerEditModal');
    installDeferredApi();
    const request = win.refreshCustomerTimeline();
    // 重开同一客户：id 不变，但 generation 变了 —— 旧响应必须被丢弃。
    win.closeModal('customerEditModal', true);
    win._customerDetailCache = { id: 1, timeline_items: [follow(12, 'A-重新加载', 1)], follow_history: [follow(12, 'A-重新加载', 1)], outreach_emails: [], timeline_pagination: { has_next: false }, reminders: [], recent_facts: [] };
    settle('/api/customers/1/timeline', { items: [follow(11, 'A-旧响应', 1)], pagination: { has_next: false } });
    await request;
    assert.deepEqual(cacheIds(), ['follow-12'], 'fresh load must not be overwritten by the previous session response');
    restoreRealApi();
  });

  await check('api() single-flights two concurrent identical POSTs into one request', async () => {
    restoreRealApi();
    const posts = [];
    let resolvePost;
    const previousFetch = win.fetch;
    win.fetch = (url, options) => {
      if (String(url) === '/api/customers/1/follow_history') {
        posts.push(String(url));
        return new Promise((resolve) => { resolvePost = resolve; });
      }
      return previousFetch(url, options);
    };
    const first = win.api('/api/customers/1/follow_history', { method: 'POST', body: JSON.stringify({ content: '一次' }) });
    const second = win.api('/api/customers/1/follow_history', { method: 'POST', body: JSON.stringify({ content: '一次' }) });
    resolvePost({ ok: true, status: 200, json: async () => ({ id: 1 }) });
    const results = await Promise.all([first, second]);
    win.fetch = previousFetch;
    assert.equal(posts.length, 1, 'identical concurrent mutations must share one request');
    assert.deepEqual(results[0], { id: 1 });
    assert.deepEqual(results[1], { id: 1 }, 'both callers must settle with the shared result');
    // 不同内容的写请求不受影响。
    const different = [];
    win.fetch = (url, options) => {
      if (String(url) === '/api/customers/1/follow_history') { posts.push(String(url)); different.push(String(options.body)); return Promise.resolve({ ok: true, status: 200, json: async () => ({ id: 2 }) }); }
      return previousFetch(url, options);
    };
    await Promise.all([
      win.api('/api/customers/1/follow_history', { method: 'POST', body: JSON.stringify({ content: '两条不同' }) }),
      win.api('/api/customers/1/follow_history', { method: 'POST', body: JSON.stringify({ content: '另一条' }) }),
    ]);
    win.fetch = previousFetch;
    assert.equal(different.length, 2, 'different payloads must not be deduped');
  });

  await check('undoing a delete cannot restore A record into B workspace', async () => {
    seedCustomer(2, [follow(22, 'B 的记录', 2)]);
    doc.getElementById('customerEditModal').classList.add('show');
    installDeferredApi();
    win.showFollowUndoToast(41, follow(41, 'A 的被删记录', 1));
    const undoButton = Array.from(doc.querySelectorAll('#toastContainer button')).pop();
    undoButton.click();
    settle('/api/follow-history/41/restore', { id: 41 });
    await wait(10);
    assert.deepEqual(cacheIds(), ['follow-22'], 'undo must not inject the deleted A record into B timeline');
    restoreRealApi();
  });

  await check('a slow openCompleteModal response cannot clobber a newer fill', async () => {
    installDeferredApi();
    win.openCompleteModal(5);
    win.openCompleteModal(7);
    const take = (url) => {
      const item = win.__pendingApi.filter((entry) => entry.url === url && !entry.done)[0];
      if (!item) throw new Error('no pending request for ' + url);
      item.done = true;
      return item;
    };
    // 两次打开的 today 读取都为空 → 两次都走 upcoming；最后到的是第一次打开的响应。
    take('/api/reminders/today').resolve([]);
    await wait(5);
    take('/api/reminders/today').resolve([]);
    await wait(5);
    take('/api/reminders/upcoming').resolve([{ id: 5, task_title: '任务五', customer_id: 1 }]);
    await wait(5);
    take('/api/reminders/upcoming').resolve([{ id: 7, task_title: '任务七', customer_id: 1 }]);
    await wait(10);
    assert.equal(doc.getElementById('completeReminderId').value, '7', 'the newest open must own the modal');
    assert.equal(doc.getElementById('completeContent').textContent, '任务七', 'a stale response must not refill the form');
    restoreRealApi();
  });

  await check('an AI analysis for customer A cannot land in customer B workspace', async () => {
    seedCustomer(1, [follow(11, 'A', 1)]);
    doc.getElementById('editName').value = '客户A';
    const composer = doc.getElementById('followHistoryContent');
    // Real browsers report contenteditable=true; jsdom does not implement it.
    Object.defineProperty(composer, 'isContentEditable', { value: true, configurable: true });
    composer.innerHTML = 'A 的沟通内容';
    installDeferredApi();
    const request = win.analyzeCommunication('history');
    seedCustomer(2, [follow(22, 'B', 2)]);
    settle('/api/inbox/analyze-reply', { analysis: { summary: 'A 的整理结果', direction: 'outbound', intent: 'x', key_facts: [], needs: [] } });
    await request;
    assert.ok(!win._communicationAnalyses.history, 'stale analysis must not enter the shared slot');
    const panelText = doc.getElementById('followHistoryAnalysis').innerHTML;
    assert.ok(!panelText.includes('A 的整理结果'), 'stale analysis must not render into B composer');
    restoreRealApi();
  });

  if (failures) {
    console.error('\ncustomer context race regression: ' + failures + ' check(s) failed');
    process.exit(1);
  }
  console.log('\ncustomer context race regression: OK');
}

run().then(() => process.exit(process.exitCode || 0)).catch((error) => {
  console.error('customer context race regression: harness error\n', error);
  process.exit(1);
});
