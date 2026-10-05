// 加载失败态回归：日历 / 沟通记录 / 操作日志在加载失败时绝不能显示成“没有数据”，
// 并且退役的新客户池页面必须保持不可达。
//
// 通过真实 loadCalendar / loadHistory / loadLogs 数据流驱动（fetch 桩），覆盖：
//   * 401 / 403 / 500 / 断网 / 非法 JSON 各自的 data-state-kind。
//   * GET 401 返回 null 的路径也进入 auth 失败态，而不是空列表。
//   * 成功但确实为空时仍显示原来的空状态文案。
//   * 日历失败态跨翻月持久；后台刷新失败保留旧数据并提示。
//   * 操作日志失败后筛选按钮保留，重试带当前筛选值。
//   * 先发后到的响应不能覆盖后发的。
//   * switchPage('newpool') 回退到 customers。
//
// Run standalone (needs node + jsdom):
//   node tests/support/load_failure_state_check.cjs
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
win.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {} });

const respond = (payload, status) => {
  const code = status || 200;
  return { ok: code >= 200 && code < 300, status: code, json: async () => payload };
};
const statusResponse = (status) => ({ ok: false, status, json: async () => ({ error: 'HTTP ' + status }) });
const networkResponse = () => Promise.reject(new TypeError('Failed to fetch'));
const badJsonResponse = () => ({ ok: true, status: 200, json: async () => { throw new SyntaxError('Unexpected token <'); } });

let apiHandler = async () => respond({});
win.fetch = function (url) {
  const target = String(url);
  // 保持自动会话探测挂起：解析成“无会话”会退回账号界面并丢掉待测状态。
  if (target.indexOf('/api/auth/me') === 0) return new Promise(() => {});
  if (target.indexOf('/api/version') === 0) return respond({ version: 'test' });
  return apiHandler(target);
};

// 顶层 let（如 currentPage）在 eval 里不会成为 window 属性；在同一个 eval 作用域内
// 追加一个只读访问器，函数声明会泄漏到全局并保留对 let 绑定的闭包。
win.eval(script + "\n;window.__getCurrentPage = function () { return currentPage; };");
// 只验证页面自身的失败态渲染，屏蔽 401 的全局提示与重试等待。
win.showToast = function () {};
win.showLogin = function () {};
win.waitForApiRetry = function () { return Promise.resolve(); };
win.scrollTo = function () {};

const tick = () => new Promise((resolve) => setTimeout(resolve, 5));
const failureHandler = (kind) => () => {
  if (kind === 'network') return networkResponse();
  if (kind === 'parse') return badJsonResponse();
  if (kind === '401') return statusResponse(401);
  if (kind === '403') return statusResponse(403);
  return statusResponse(500);
};
const EXPECTED_KIND = { '401': 'auth', '403': 'permission', '500': 'service', network: 'network', parse: 'parse' };

function todayDateStr() {
  const d = new Date();
  return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-05';
}

function assertLoaderFailures(expectedKind) {
  const grid = doc.getElementById('calendarGrid');
  const calendarError = grid.querySelector('[data-state="error"]');
  assert.ok(calendarError, '日历应显示失败态');
  assert.equal(calendarError.getAttribute('data-state-kind'), expectedKind, '日历失败类型');
  assert.equal(grid.querySelectorAll('.calendar-day').length, 0, '日历失败时不能画出空月历');

  const history = doc.getElementById('historyTimeline');
  const historyError = history.querySelector('[data-state="error"]');
  assert.ok(historyError, '沟通记录应显示失败态');
  assert.equal(historyError.getAttribute('data-state-kind'), expectedKind, '沟通记录失败类型');
  assert.ok(!history.textContent.includes('暂无沟通记录'), '沟通记录失败不能显示成空');
  assert.ok(historyError.querySelector('button'), '沟通记录失败态需要重试按钮');

  const logs = doc.getElementById('logsList');
  const logsError = logs.querySelector('[data-state="error"]');
  assert.ok(logsError, '操作日志应显示失败态');
  assert.equal(logsError.getAttribute('data-state-kind'), expectedKind, '操作日志失败类型');
  assert.ok(!logs.textContent.includes('暂无操作日志'), '操作日志失败不能显示成空');
  assert.ok(logsError.querySelector('button'), '操作日志失败态需要重试按钮');
}

(async () => {
  // 1) 成功但确实为空：仍是原来的空状态，而不是错误。
  apiHandler = (url) => {
    if (url.indexOf('/api/reminders/') !== -1) return respond([]);
    if (url.indexOf('/api/follow-history') !== -1) return respond([]);
    if (url.indexOf('/api/logs') !== -1) return respond([]);
    return respond({});
  };
  win.goToday();
  await tick();
  const grid = doc.getElementById('calendarGrid');
  assert.ok(!grid.querySelector('[data-state="error"]'), '空日历成功时不应出现失败态');
  assert.ok(grid.querySelectorAll('.calendar-day').length > 0, '空日历成功时仍画出整月');
  await win.loadHistory();
  assert.ok(doc.getElementById('historyTimeline').textContent.includes('暂无沟通记录'), '空沟通记录保留原文案');
  await win.loadLogs('all');
  assert.ok(doc.getElementById('logsList').textContent.includes('暂无操作日志'), '空操作日志保留原文案');

  // 2) 每类失败都映射到各自的 data-state-kind（含 GET 401 → null 的 auth 路径）。
  for (const kind of ['401', '403', '500', 'network', 'parse']) {
    apiHandler = failureHandler(kind);
    await win.loadCalendar();
    await win.loadHistory();
    await win.loadLogs('all');
    assertLoaderFailures(EXPECTED_KIND[kind]);
  }

  // 3) 日历失败态跨翻月与重画持久，并清掉旧详情。
  apiHandler = failureHandler('500');
  await win.loadCalendar();
  win.changeMonth(1);
  await tick();
  assert.ok(doc.getElementById('calendarGrid').querySelector('[data-state="error"]'), '失败态应跨 changeMonth 持久');
  win.renderCalendar();
  assert.ok(doc.getElementById('calendarGrid').querySelector('[data-state="error"]'), '失败态应跨直接重画持久');
  assert.equal(doc.getElementById('calendarDetail').innerHTML, '', '失败时应清掉上一次选中日期的旧详情');

  // 4) 后台刷新失败：保留旧数据并提示，不替换成整页错误。
  apiHandler = (url) => {
    if (url.indexOf('/api/reminders/today') !== -1) return respond([{ id: 1, remind_date: todayDateStr(), customer_name: 'A', content: 'x' }]);
    if (url.indexOf('/api/reminders/upcoming') !== -1) return respond([]);
    return respond({});
  };
  win.goToday();
  await tick();
  assert.ok(grid.textContent.includes('1 项'), '日历显示已加载的安排');
  assert.ok(!doc.getElementById('calendarRefreshNotice'), '成功加载后不应有刷新失败提示');
  apiHandler = failureHandler('500');
  await win.loadCalendar({ background: true });
  assert.ok(grid.textContent.includes('1 项'), '后台刷新失败应保留旧数据');
  assert.ok(!grid.querySelector('[data-state="error"]'), '后台刷新失败不能替换成整页错误');
  const notice = doc.getElementById('calendarRefreshNotice');
  assert.ok(notice && notice.textContent.includes('刷新失败'), '后台刷新失败应给出刷新失败提示');

  // 5) 操作日志失败：筛选按钮保留，重试带当前筛选值。
  apiHandler = failureHandler('403');
  await win.loadLogs('CREATE');
  const logs = doc.getElementById('logsList');
  const filterButtons = logs.querySelectorAll('button[onclick^="loadLogs("]');
  assert.ok(filterButtons.length >= 6, '失败后筛选按钮仍在');
  const activeFilters = Array.from(filterButtons).filter((button) => button.classList.contains('btn-primary'));
  assert.equal(activeFilters.length, 1, '失败后只有一个激活的筛选');
  assert.ok(activeFilters[0].getAttribute('onclick').includes("loadLogs('CREATE')"), '激活筛选保持当前值');
  const retry = logs.querySelector('[data-state="error"] button');
  assert.ok(retry && retry.getAttribute('onclick').includes("loadLogs('CREATE')"), '重试按钮使用当前筛选值');

  // 6) 先发后到的响应不能覆盖后发的（沟通记录）。
  const deferred = [];
  apiHandler = (url) => {
    if (url.indexOf('/api/follow-history') !== -1) return new Promise((resolve) => deferred.push(resolve));
    return respond({});
  };
  const first = win.loadHistory();
  const second = win.loadHistory();
  deferred[1](respond([{ id: 2, customer_name: 'Later', content: 'second' }]));
  await second;
  deferred[0](respond([{ id: 1, customer_name: 'Earlier', content: 'first' }]));
  await first;
  const timeline = doc.getElementById('historyTimeline').textContent;
  assert.ok(timeline.includes('Later'), '后发的响应应被显示');
  assert.ok(!timeline.includes('Earlier'), '先发后到的响应应被丢弃');

  // 7) 退役的新客户池页面不可达：回退客户列表，且不调用它的 loader。
  let newPoolLoaded = 0;
  let customersLoaded = 0;
  win.loadNewPool = function () { newPoolLoaded++; };
  win.loadCustomers = function () { customersLoaded++; };
  // currentPage 是顶层 let，通过同一 eval 作用域内的只读访问器读取。
  const currentPage = () => win.__getCurrentPage();
  win.switchPage('newpool');
  assert.equal(currentPage(), 'customers', 'newpool 应回退到 customers');
  assert.ok(!doc.getElementById('page-newpool').classList.contains('active'), '退役页面不能被激活');
  assert.ok(doc.getElementById('page-newpool').hasAttribute('hidden'), '退役页面保持 hidden');
  assert.ok(doc.getElementById('page-customers').classList.contains('active'), '客户页应被激活');
  assert.equal(newPoolLoaded, 0, '退役页面的 loader 不应被调用');
  win.switchPage('ghost-page');
  assert.equal(currentPage(), 'customers', '不存在的页面应回退到 customers');

  console.log('load failure state regression: OK');
})().catch((error) => {
  console.error(error && error.stack || error);
  process.exit(1);
});
