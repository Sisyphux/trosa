/*
 * Real-browser reproduction: 补记一条日期较早的沟通后，左边时间线闪一下就没了。
 *
 * 时间线按沟通日期倒序，客户页只读最新 3 条。客户已有日期靠后的记录时（正式库里
 * 有被写成 2027 年的历史记录），新记录日期落在最新 3 条之外：保存后先回显一下，
 * 随后的读回按 3 条一页把它挤掉，看起来像没保存（操作日志里却有）。
 *
 * Usage (see docs/REAL_BROWSER_REPRO.md):
 *   node tools/repro_timeline_window.cjs --url http://127.0.0.1:18190 --label before --out docs/repro/timeline-window/before
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
  const options = { url: process.env.TROSA_REPRO_URL || 'http://127.0.0.1:18190', label: 'run', out: '' };
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
  const candidates = [
    path.join(ROOT, 'browser-extension', 'node_modules', 'playwright'),
    path.join('/Users/luoxin/Desktop/Trosa', 'browser-extension', 'node_modules', 'playwright'),
  ];
  for (const candidate of candidates) {
    try { return require(candidate); } catch (error) { /* try next */ }
  }
  console.error('Playwright 未安装：请在 browser-extension 执行 npm ci');
  process.exit(EXIT_INFRA);
}

async function settle(page) {
  await page.waitForFunction(
    () => !document.documentElement.dataset.motionType && !document.activeViewTransition,
    null,
    { timeout: 5000 },
  ).catch(() => {});
  await page.waitForTimeout(500);
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

function rowTexts(page) {
  return page.evaluate(() => Array.from(document.querySelectorAll('#outreachList .cw-row')).map(
    (row) => (row.querySelector('.cw-tx') || {}).textContent || ''));
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
    await page.goto(`${options.url}/`, { waitUntil: 'load' });
    const login = await json(page, '/api/auth/login', 'POST', { user: 'kelley' });
    if (login.status !== 200) throw new Error(`login failed: ${login.status}`);

    // 1. 一个客户，已有 5 条日期靠后的记录（模拟被写成未来年份的历史记录）。
    const mark = Date.now();
    const created = await json(page, '/api/customers', 'POST', { company: `时间线复现${mark}`, name: '复现联系人', country: 'China' });
    if (created.status !== 201) throw new Error(`customer create failed: ${created.status} ${JSON.stringify(created.data)}`);
    const customerId = created.data.id;
    for (let i = 1; i <= 5; i += 1) {
      const seed = await json(page, `/api/customers/${customerId}/follow_history`, 'POST', {
        activity_content: `历史记录${i}`, activity_type: 'follow_up', direction: 'outbound',
        follow_date: `2027-0${i}-10`,
      });
      if (seed.status >= 300) throw new Error(`seed failed: ${seed.status} ${JSON.stringify(seed.data)}`);
    }
    report.measurements = { customer_id: customerId };

    // 2. 打开客户，用「记录沟通」补记一条 10 月的较早日期记录。
    await page.reload({ waitUntil: 'load' });
    await page.waitForFunction(() => typeof openEditModal === 'function', null, { timeout: 20000 });
    await page.evaluate((id) => openEditModal(id), customerId);
    await page.waitForSelector('#outreachList .cw-row', { timeout: 20000 });
    await settle(page);
    await shot('01-before-record.png');
    const initial = await rowTexts(page);

    await page.evaluate(() => openCustomerFollowComposer());
    await page.waitForSelector('#inboxReplyModal.show', { timeout: 10000 });
    await page.fill('#inboxReplyContent', '补记：10.20 询问近期需求');
    await page.fill('#inboxReplyDate', '2026-10-20');
    await page.click('#saveInboxReplyButton');
    await page.waitForSelector('#inboxReplyModal.show', { state: 'detached', timeout: 15000 }).catch(() => {});
    await page.waitForFunction(() => !document.querySelector('#inboxReplyModal.show'), null, { timeout: 15000 });

    // 3. 等回显和后台读回都结束后，这条必须还在左边。
    await page.waitForTimeout(3500);
    await shot('02-after-record.png');
    const rows = await rowTexts(page);
    const stored = await json(page, `/api/customers/${customerId}/timeline?page=1&per_page=50`, 'GET');
    const storedItems = (stored.data && stored.data.items) || [];
    check('record-saved-on-server', '服务端已经保存了这条记录',
      storedItems.some((item) => String(item.content || '').indexOf('补记：10.20') >= 0),
      `服务端共 ${storedItems.length} 条`);
    check('record-still-on-screen', '保存几秒后，时间线里仍能看到这条补记',
      rows.some((text) => text.indexOf('补记：10.20') >= 0),
      `保存前 ${initial.length} 行；保存后 ${rows.length} 行：${rows.join(' | ').slice(0, 200)}`);
  } finally {
    await context.close();
  }
}

async function main() {
  const options = parseArgs(process.argv.slice(2));
  const outDir = path.resolve(ROOT, options.out);
  fs.mkdirSync(outDir, { recursive: true });
  const report = { label: options.label, url: options.url, checks: [], screenshots: [], pageErrors: [], measurements: {} };
  let browser;
  try {
    const { chromium } = loadPlaywright();
    browser = await chromium.launch();
    await runScenario(browser, options, outDir, report);
  } catch (error) {
    report.infrastructureError = String(error && error.stack || error).slice(0, 600);
    fs.writeFileSync(path.join(outDir, 'report.json'), JSON.stringify(report, null, 2));
    console.error(report.infrastructureError);
    process.exit(EXIT_INFRA);
  } finally {
    if (browser) await browser.close();
  }
  fs.writeFileSync(path.join(outDir, 'report.json'), JSON.stringify(report, null, 2));
  let failed = 0;
  for (const item of report.checks) {
    console.log(`${item.pass ? 'PASS' : 'FAIL'}  ${item.title}  —  ${item.detail}`);
    if (!item.pass) failed += 1;
  }
  process.exit(failed ? EXIT_DEFECT : EXIT_PASS);
}

main();
