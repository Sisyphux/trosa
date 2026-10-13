/*
 * Real-browser reproduction: "on Today I can only finish the todo and write a
 * record; there is no convenient way to set the next step" (今天页无法顺手安排下一步).
 *
 * Runs in the Playwright Chromium pinned by browser-extension/package-lock.json.
 * The scenario seeds one customer with a todo due today in an isolated dev
 * database, opens Today, presses 记录沟通, and checks that the same dialog can
 * set the next step (action + date) and that saving creates that open todo.
 *
 * Usage (see docs/REAL_BROWSER_REPRO.md):
 *   node tools/repro_today_next_step.cjs --url http://127.0.0.1:18191 --label after --out docs/repro/today-next-step/after
 *
 * Exit codes: 0 all checks pass, 20 at least one check fails (the defect is
 * present), 21 infrastructure failure (server unreachable, browser missing).
 */
'use strict';

const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');
const EXIT_PASS = 0;
const EXIT_DEFECT = 20;
const EXIT_INFRA = 21;

function parseArgs(argv) {
  const options = { url: process.env.TROSA_REPRO_URL || 'http://127.0.0.1:18191', label: 'run', out: '' };
  for (let i = 0; i < argv.length; i += 1) {
    const key = argv[i];
    const value = argv[i + 1];
    if (key === '--url') { options.url = value; i += 1; }
    else if (key === '--label') { options.label = value; i += 1; }
    else if (key === '--out') { options.out = value; i += 1; }
    else throw new Error(`unknown argument: ${key}`);
  }
  if (!options.out) throw new Error('--out <directory> is required (evidence is saved there)');
  return options;
}

function loadPlaywright() {
  try {
    return require(path.join(ROOT, 'browser-extension', 'node_modules', 'playwright'));
  } catch (error) {
    console.error('Playwright 未安装：请在 browser-extension 执行 npm ci');
    process.exit(EXIT_INFRA);
  }
}

function localDate(offsetDays) {
  const d = new Date();
  d.setDate(d.getDate() + offsetDays);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}

async function settle(page) {
  await page.waitForFunction(
    () => !document.documentElement.dataset.motionType && !document.activeViewTransition,
    null,
    { timeout: 5000 },
  ).catch(() => {});
  await page.waitForTimeout(500);
}

// A loopback visitor counts as an office viewer and boots into the weekly board;
// the signed-in member then goes to Today the same way the index does.
async function openToday(page, url) {
  await page.goto(`${url}/`, { waitUntil: 'load' });
  await page.waitForFunction(() => typeof switchPage === 'function', null, { timeout: 20000 });
  await page.evaluate(() => { if (currentPage !== 'dashboard') switchPage('dashboard'); });
  await page.waitForSelector('#page-dashboard.active', { timeout: 20000 });
  await page.waitForSelector('#todayFocus .room-act.primary', { timeout: 15000 });
}

async function json(page, url, method, body) {
  return page.evaluate(async ({ url, method, body }) => {
    const res = await fetch(url, {
      method, credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: body ? JSON.stringify(body) : undefined,
    });
    let data = null;
    try { data = await res.json(); } catch (e) { data = null; }
    return { status: res.status, data };
  }, { url, method, body });
}

async function runScenario(browser, options, outDir, report) {
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, locale: 'zh-CN' });
  const page = await context.newPage();
  page.on('pageerror', (error) => report.pageErrors.push(String(error.message).slice(0, 200)));
  const shot = async (name) => {
    await settle(page);
    await page.screenshot({ path: path.join(outDir, name) });
    report.screenshots.push(name);
  };
  const check = (id, title, pass, detail) => report.checks.push({ id, title, pass: !!pass, detail });

  try {
    // 1. Seed: sign in on the isolated dev server, one customer, one todo due today.
    await page.goto(`${options.url}/`, { waitUntil: 'load' });
    const login = await json(page, '/api/auth/login', 'POST', { user: 'hamid' });
    if (login.status !== 200) throw new Error(`login failed: ${login.status}`);
    const company = `Repro Next Step ${Date.now()}`;
    const created = await json(page, '/api/customers', 'POST', { company, name: 'Ana', country: 'Spain' });
    if (created.status !== 201) throw new Error(`customer create failed: ${created.status} ${JSON.stringify(created.data)}`);
    const customerId = created.data.id;
    // A customer with no real two-way contact belongs to Sela and stays off Today,
    // so the seed starts from a customer reply, as a real handed-over customer does.
    const reply = await json(page, `/api/customers/${customerId}/follow_history`, 'POST', {
      activity_content: '客户回信：样品收到了，下周给测试结果。', activity_type: 'customer_reply', direction: 'inbound',
    });
    if (reply.status >= 300) throw new Error(`reply seed failed: ${reply.status} ${JSON.stringify(reply.data)}`);
    const task = await json(page, `/api/customers/${customerId}/tasks`, 'POST', { title: '确认样品反馈', due_date: localDate(0) });
    if (task.status !== 201) throw new Error(`task create failed: ${task.status} ${JSON.stringify(task.data)}`);

    // 2. Today shows the customer; press 记录沟通.
    await openToday(page, options.url);
    await shot('01-today-1440.png');
    await page.click('#todayFocus .room-act.primary');
    await page.waitForSelector('#inboxReplyModal.show', { timeout: 8000 });
    await shot('02-record-dialog-1440.png');

    // 3. The dialog offers the next step: an action field and a date field, both visible.
    const fields = await page.evaluate(() => {
      const shown = (el) => !!el && el.getClientRects().length > 0 && el.getBoundingClientRect().height > 0;
      return {
        task: shown(document.getElementById('inboxReplyNextTask')),
        date: shown(document.getElementById('inboxReplyNextDate')),
      };
    });
    check('dialog-offers-next-step',
      '今天页「记录沟通」对话框里能直接填写下一步（动作 + 日期）',
      fields.task && fields.date,
      `动作输入框：${fields.task ? '可见' : '没有'}；日期：${fields.date ? '可见' : '没有'}`);
    if (!(fields.task && fields.date)) return;

    // 4. Fill record + next step (date via the 7-day chip) and save.
    const nextTitle = '发送报价单并确认交期';
    await page.fill('#inboxReplyContent', '电话沟通，客户样品测试通过，要正式报价。');
    await page.fill('#inboxReplyNextTask', nextTitle);
    await page.click('#communicationConfirmNext .task-date-choices button:first-child');
    const chosenDate = await page.inputValue('#inboxReplyNextDate');
    check('quick-date-chip-fills-date', '点「7 天」后日期自动填好', chosenDate === localDate(7), `日期：${chosenDate || '空'}`);
    const label = (await page.textContent('#saveInboxReplyButton')).trim();
    check('save-label-says-next-step', '填写下一步后，保存按钮说明会一起安排下一步', /下一步/.test(label), `按钮：${label}`);
    await shot('03-dialog-filled-1440.png');
    await page.click('#saveInboxReplyButton');
    await page.waitForFunction(() => !document.querySelector('#inboxReplyModal.show'), null, { timeout: 10000 });
    await page.waitForTimeout(800);
    await shot('04-after-save-1440.png');

    // 5. The customer now has exactly that open next step; today's todo is done.
    const tasks = await json(page, `/api/customers/${customerId}/tasks`, 'GET');
    const open = ((tasks.data && tasks.data.tasks) || []).filter((t) => !t.is_done);
    const next = open.find((t) => (t.title || t.content) === nextTitle);
    check('next-step-saved',
      '保存后客户有这条下一步，日期正确',
      !!next && String(next.remind_date || '').slice(0, 10) === localDate(7),
      next ? `${next.title || next.content} · ${String(next.remind_date).slice(0, 10)}` : `未找到；未完成待办：${open.map((t) => t.title || t.content).join('、') || '无'}`);
    check('today-todo-completed', '今天的待办同时完成', !open.some((t) => (t.title || t.content) === '确认样品反馈'),
      open.map((t) => t.title || t.content).join('、') || '无其他未完成待办');

    // 6. Leaving the next step empty still saves only the record (a record may have no next step).
    const task2 = await json(page, `/api/customers/${customerId}/tasks`, 'POST', { title: '回访', due_date: localDate(0) });
    await openToday(page, options.url);
    await page.click('#todayFocus .room-act.primary');
    await page.waitForSelector('#inboxReplyModal.show', { timeout: 8000 });
    const reset = await page.evaluate(() => ({
      task: document.getElementById('inboxReplyNextTask').value,
      date: document.getElementById('inboxReplyNextDate').value,
    }));
    check('next-step-fields-reset', '再次打开时下一步输入框是空的', !reset.task && !reset.date, JSON.stringify(reset));
    await page.fill('#inboxReplyContent', '发了消息，暂无回复。');
    await page.click('#saveInboxReplyButton');
    await page.waitForFunction(() => !document.querySelector('#inboxReplyModal.show'), null, { timeout: 10000 });
    const tasks2 = await json(page, `/api/customers/${customerId}/tasks`, 'GET');
    const open2 = ((tasks2.data && tasks2.data.tasks) || []).filter((t) => !t.is_done);
    check('record-without-next-step',
      '不填下一步也能只保存记录，不多出待办',
      task2.status === 201 && open2.length === 1 && (open2[0].title || open2[0].content) === nextTitle,
      open2.map((t) => t.title || t.content).join('、') || '无');
  } catch (error) {
    const message = String(error && error.message ? error.message : error).split('\n')[0];
    if (/ERR_CONNECTION|ECONNREFUSED|net::ERR_/.test(message)) throw Object.assign(new Error(message), { infra: true });
    report.checks.push({ id: 'scenario-completed', title: '场景能完整走完', pass: false, detail: message.slice(0, 300) });
    await shot('99-failure.png').catch(() => {});
  } finally {
    await context.close();
  }
}

async function main() {
  const options = parseArgs(process.argv.slice(2));
  const outDir = path.resolve(ROOT, options.out);
  fs.mkdirSync(outDir, { recursive: true });
  const { chromium } = loadPlaywright();
  const report = {
    label: options.label, url: options.url, generatedAt: new Date().toISOString(),
    checks: [], screenshots: [], pageErrors: [],
  };
  let browser;
  try {
    browser = await chromium.launch({ headless: true });
    report.browser = `chromium ${browser.version()}`;
    await runScenario(browser, options, outDir, report);
  } catch (error) {
    if (error.infra) {
      console.error(`基础设施失败：${error.message}（请确认服务在 ${options.url} 运行）`);
      process.exit(EXIT_INFRA);
    }
    console.error(`浏览器不可用或场景异常：${error.message}`);
    process.exit(EXIT_INFRA);
  } finally {
    if (browser) await browser.close();
  }

  fs.writeFileSync(path.join(outDir, 'report.json'), `${JSON.stringify(report, null, 2)}\n`, 'utf8');
  const failed = report.checks.filter((c) => !c.pass);
  for (const c of report.checks) {
    console.log(`${c.pass ? 'PASS' : 'FAIL'}  ${c.id}  —  ${c.title}\n        ${c.detail}`);
  }
  console.log(`\n${report.checks.length - failed.length}/${report.checks.length} 项通过；证据：${path.relative(ROOT, outDir) || outDir}`);
  process.exit(failed.length ? EXIT_DEFECT : EXIT_PASS);
}

main();
