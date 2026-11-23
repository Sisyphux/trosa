/*
 * Real-browser check: 客户工作台顶栏的「导出全部沟通」把完整记录真正放进剪贴板。
 *
 * The scenario seeds a de-identified Customer with more than 50 communications,
 * same-day rows, emails with a body, a reply, a subject-only row and a bounce,
 * one open task and machine research.  It clicks the real top-bar button and
 * reads the real clipboard, then checks the pasted text — not the API status.
 *
 * Usage (see docs/REAL_BROWSER_REPRO.md):
 *   node tools/repro_customer_context_export.cjs --url http://127.0.0.1:18190 \
 *     --label before --out docs/repro/customer-context-export/before
 *
 * Exit codes: 0 all checks pass, 20 at least one check fails (the defect is
 * present), 21 infrastructure failure (server unreachable, browser or
 * clipboard unavailable).
 */
'use strict';

const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');
const EXIT_PASS = 0;
const EXIT_DEFECT = 20;
const EXIT_INFRA = 21;
const COMPANY = '脱敏亚克力客户（导出验收）';

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
  try {
    return require(path.join(ROOT, 'browser-extension', 'node_modules', 'playwright'));
  } catch (error) {
    console.error('Playwright 未安装：请在 browser-extension 执行 npm ci');
    process.exit(EXIT_INFRA);
  }
}

async function settle(page) {
  await page.waitForFunction(
    () => !document.documentElement.dataset.motionType && !document.activeViewTransition,
    null,
    { timeout: 5000 },
  ).catch(() => {});
  await page.waitForTimeout(500);
}

async function json(page, url) {
  return page.evaluate(async (url) => {
    const res = await fetch(url, { credentials: 'same-origin' });
    let data = null;
    try { data = await res.json(); } catch (e) { data = null; }
    return { status: res.status, data };
  }, url);
}

async function runScenario(browser, options, outDir, report) {
  const context = await browser.newContext({
    viewport: { width: 1440, height: 900 },
    locale: 'zh-CN',
    permissions: ['clipboard-read', 'clipboard-write'],
  });
  const page = await context.newPage();
  page.on('pageerror', (error) => report.pageErrors.push(String(error.message).slice(0, 200)));
  const shot = async (name) => {
    await settle(page);
    const file = `${options.label}-${name}`;
    await page.screenshot({ path: path.join(outDir, file) });
    report.screenshots.push(file);
  };
  const check = (id, title, pass, detail) => report.checks.push({ id, title, pass: !!pass, detail });

  try {
    await page.goto(`${options.url}/`, { waitUntil: 'load' });
    const login = await page.evaluate(async () => {
      const res = await fetch('/api/auth/login', {
        method: 'POST', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ user: 'hamid' }),
      });
      return res.status;
    });
    if (login !== 200) throw new Error(`login failed: ${login}`);
    await page.reload({ waitUntil: 'load' });
    await page.waitForTimeout(1500);

    const listing = await json(page, '/api/customers?search=' + encodeURIComponent(COMPANY));
    if (listing.status !== 200 || !listing.data) throw new Error(`customer lookup failed: ${listing.status}`);
    const customer = (listing.data.customers || []).find((row) => row.company === COMPANY);
    if (!customer) throw new Error('seeded customer not found; run tools/repro_seed_context_export.py first');
    report.measurements = { customer_id: customer.id };

    const api = await json(page, `/api/customers/${customer.id}/context?mode=timeline`);
    if (api.status !== 200 || !api.data || typeof api.data.content !== 'string') {
      throw new Error(`context endpoint failed: ${api.status}`);
    }

    await page.evaluate(async (id) => { await openEditModal(id); }, customer.id);
    await page.waitForSelector('#customerEditModal.show', { timeout: 20000 });
    await page.waitForSelector('#cwExportTools button[aria-label="导出全部沟通"]', { timeout: 15000 });
    await settle(page);
    await shot('workspace-open.png');

    await page.click('#cwExportTools button[aria-label="导出全部沟通"]');
    await page.waitForTimeout(1800);
    const toast = (await page.textContent('#toastContainer').catch(() => '')) || '';
    if (toast.indexOf('复制上下文失败') >= 0) {
      throw new Error('clipboard write rejected in the browser: ' + toast.trim());
    }
    await shot('after-copy.png');

    const copied = await page.evaluate(() => navigator.clipboard.readText());
    report.measurements.copied_chars = copied.length;
    report.measurements.api_chars = api.data.content.length;

    check('copied-equals-endpoint', '剪贴板内容与接口返回的完整文本一致',
      copied === api.data.content, `${copied.length} vs ${api.data.content.length} chars`);
    check('all-records', '超过 50 条时仍包含全部记录',
      copied.indexOf('历史沟通 [H060]') >= 0 && copied.indexOf('历史沟通 [H001]') >= 0,
      'H060/H001 present');
    check('same-day-order', '同一天多条按保存顺序稳定排列',
      copied.indexOf('历史沟通 [H041]') >= 0 &&
      copied.indexOf('历史沟通 [H041]') < copied.indexOf('历史沟通 [H021]') &&
      copied.indexOf('历史沟通 [H021]') < copied.indexOf('历史沟通 [H001]'),
      'H041 → H021 → H001');
    check('email-body', '开发邮件保留已存正文，不只给主题',
      copied.indexOf('附上 3mm 透明亚克力板报价') >= 0, 'quotation body present');
    check('email-reply', '客户回信单独列出并保留原话',
      copied.indexOf('· 客户回信 · 客户发来') >= 0 && copied.indexOf('正文：可以的话寄样') >= 0,
      'reply present');
    check('subject-only', '只存主题的邮件明确写未保存正文',
      copied.indexOf('Trosa 未保存正文，只保存了主题') >= 0, 'missing-body note present');
    check('task-separate', '当前未完成待办单独列出动作与日期',
      copied.indexOf('## 当前未完成待办') >= 0 &&
      copied.indexOf('寄样并确认运费') >= 0, 'open task present');
    check('historical-next-step', '历史记录里的下一步标明是记录时写的，不冒充当前待办',
      copied.indexOf('记录时写的下一步') >= 0, 'historical label present');
    check('notes-separate', '人工备注单独列出',
      copied.indexOf('## 人工备注') >= 0 && copied.indexOf('客户要求报价含包装') >= 0, 'notes present');
    check('research-separate', '自动背调单独列出并标明未经核实',
      copied.indexOf('## 自动背调') >= 0 && copied.indexOf('未经人工核实') >= 0, 'research present');
    check('no-invented-send', '客户原话「可以的话寄样」没有被写成已寄样',
      copied.indexOf('可以的话寄样') >= 0 && copied.indexOf('已寄样') < 0, 'verbatim sample request');
    check('original-blocker', '保留我方已告知原弯管方案做不了的依据',
      copied.indexOf('原弯管方案做不了') >= 0, 'blocker present');
    report.sample = copied.slice(0, 1200);
  } finally {
    await context.close();
  }
}

(async () => {
  const options = parseArgs(process.argv.slice(2));
  const outDir = path.resolve(options.out);
  fs.mkdirSync(outDir, { recursive: true });
  const report = { label: options.label, url: options.url, checks: [], screenshots: [], pageErrors: [], measurements: {} };
  const { chromium } = loadPlaywright();
  let browser;
  try {
    browser = await chromium.launch({ headless: true });
  } catch (error) {
    console.error('无法启动 Chromium：', error.message);
    process.exit(EXIT_INFRA);
  }
  try {
    await runScenario(browser, options, outDir, report);
  } catch (error) {
    report.error = String(error.message || error);
    fs.writeFileSync(path.join(outDir, 'report.json'), JSON.stringify(report, null, 2));
    console.error('infrastructure failure:', report.error);
    await browser.close();
    process.exit(EXIT_INFRA);
  }
  await browser.close();
  fs.writeFileSync(path.join(outDir, 'report.json'), JSON.stringify(report, null, 2));
  const failed = report.checks.filter((item) => !item.pass);
  for (const item of report.checks) console.log(`  ${item.pass ? 'ok  ' : 'FAIL'} - ${item.title}${item.detail ? ' (' + item.detail + ')' : ''}`);
  if (failed.length) {
    console.error(`\ncustomer context export: ${failed.length} check(s) failed`);
    process.exit(EXIT_DEFECT);
  }
  console.log('\ncustomer context export: OK');
  process.exit(EXIT_PASS);
})();
