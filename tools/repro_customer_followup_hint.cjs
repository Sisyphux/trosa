/*
 * Real-browser reproduction: "a quiet customer is never mentioned where I work"
 * (客户页看不到「该联系」那一行).
 *
 * Seeds an isolated dev database (synthetic customers only) with customers whose
 * latest record is an unanswered inbound message plus the system-made suggestion
 * for them.  Checks that Today stays exactly as before (no new block), that the
 * customer page shows ONE quiet dated sentence under 下一步 with no buttons, and
 * that any newer record or a dated next step makes it disappear by itself.
 *
 * Usage (see docs/REAL_BROWSER_REPRO.md):
 *   node tools/repro_customer_followup_hint.cjs --url http://127.0.0.1:18302 --app-root . \
 *        --db-dir <scratch db> --label after --out docs/repro/customer-followup-hint/after
 *
 * Exit codes: 0 all checks pass, 20 at least one check fails (the defect is
 * present), 21 infrastructure failure (server unreachable, browser missing).
 */
'use strict';

const fs = require('fs');
const path = require('path');
const { spawnSync } = require('child_process');

const ROOT = path.resolve(__dirname, '..');
const EXIT_PASS = 0;
const EXIT_DEFECT = 20;
const EXIT_INFRA = 21;

function parseArgs(argv) {
  const options = { url: process.env.TROSA_REPRO_URL || 'http://127.0.0.1:18302', label: 'run', out: '', appRoot: ROOT, dbDir: '', python: path.join(ROOT, '.venv', 'bin', 'python') };
  for (let i = 0; i < argv.length; i += 1) {
    const key = argv[i];
    const value = argv[i + 1];
    if (key === '--url') { options.url = value; i += 1; }
    else if (key === '--label') { options.label = value; i += 1; }
    else if (key === '--out') { options.out = value; i += 1; }
    else if (key === '--app-root') { options.appRoot = path.resolve(value); i += 1; }
    else if (key === '--db-dir') { options.dbDir = path.resolve(value); i += 1; }
    else if (key === '--python') { options.python = value; i += 1; }
    else throw new Error(`unknown argument: ${key}`);
  }
  if (!options.out) throw new Error('--out <directory> is required (evidence is saved there)');
  if (!options.dbDir) throw new Error('--db-dir <scratch database directory of the running server> is required');
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
    null, { timeout: 5000 },
  ).catch(() => {});
  await page.waitForTimeout(600);
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

function seed(options, items) {
  const args = [path.join(ROOT, 'tools', 'repro_seed_followups.py'), '--app-root', options.appRoot, '--db-dir', options.dbDir];
  const run = spawnSync(options.python, args, { input: JSON.stringify(items), encoding: 'utf8' });
  if (run.status !== 0) throw new Error(`seed failed: ${(run.stderr || run.stdout || '').slice(0, 300)}`);
}

// The app restores the member's last page a moment after boot, which can undo an
// early switch; keep asking for Today until it stays.
async function openToday(page, url, wait) {
  await page.goto(`${url}/`, { waitUntil: 'load' });
  await page.waitForFunction(() => typeof switchPage === 'function', null, { timeout: 20000 });
  for (let attempt = 0; attempt < 4; attempt += 1) {
    await page.evaluate(() => { if (currentPage !== 'dashboard') switchPage('dashboard'); });
    await page.waitForTimeout(1200);
    if (await page.evaluate(() => currentPage === 'dashboard' && !!document.querySelector('#page-dashboard.active'))) break;
  }
  await page.waitForSelector('#page-dashboard.active', { timeout: 10000 });
  if (wait) await page.waitForSelector(wait, { timeout: 8000 });
}

const COMPANIES = [
  ['Harbor Plastics', 'Ana', 'Spain', '客户问了 3mm 透明板的最新价格和交期，说下周要定。'],
  ['Nordlys Trading', 'Lars', 'Norway', '样品已寄出，对方说测试完回复。'],
  ['Sunrise Display', 'Mei', 'Singapore', '报价 200 张已发，等对方决定。'],
];
const REASON = '客户 10月20日来信，之后还没有回复';
const HINT = '#customerFollowHint';

async function openCustomer(page, id) {
  await page.evaluate((cid) => openEditModal(cid), id);
  await page.waitForSelector('#customerNextTask h2', { timeout: 10000 });
  await page.waitForTimeout(500);
}

async function closeCustomer(page) {
  await page.evaluate(() => closeModal('customerEditModal'));
  await page.waitForTimeout(400);
}

async function runScenario(browser, options, outDir, report) {
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, locale: 'zh-CN' });
  const page = await context.newPage();
  page.on('pageerror', (error) => report.pageErrors.push(String(error.message).slice(0, 200)));
  const shot = async (name, target) => {
    await settle(page);
    await (target || page).screenshot({ path: path.join(outDir, name) });
    report.screenshots.push(name);
  };
  const check = (id, title, pass, detail) => report.checks.push({ id, title, pass: !!pass, detail });
  const hintText = () => page.evaluate((sel) => { const el = document.querySelector(sel); return el ? el.innerText.trim() : ''; }, HINT);

  try {
    await page.goto(`${options.url}/`, { waitUntil: 'load' });
    const login = await json(page, '/api/auth/login', 'POST', { user: 'hamid' });
    if (login.status !== 200) throw new Error(`login failed: ${login.status}`);
    const stamp = Date.now() % 100000;
    const ids = [];
    for (const [company, name, country, said] of COMPANIES) {
      const created = await json(page, '/api/customers', 'POST', { company: `${company} ${stamp}`, name, country });
      if (created.status !== 201) throw new Error(`customer create failed: ${created.status} ${JSON.stringify(created.data)}`);
      ids.push(created.data.id);
      const rec = await json(page, `/api/customers/${created.data.id}/follow_history`, 'POST', { activity_content: said, activity_type: 'customer_reply', direction: 'inbound' });
      if (rec.status >= 300) throw new Error(`record seed failed: ${rec.status}`);
    }
    seed(options, ids.map((id, i) => ({ customer_id: id, rule: 'R1', reason: REASON, priority: i + 1 })));

    // 1. Today carries nothing new: the block is not there and no sentence leaks onto it.
    await openToday(page, options.url, null);
    await page.waitForTimeout(1500);
    const todayText = await page.evaluate(() => (document.getElementById('page-dashboard') || {}).innerText || '');
    check('today-unchanged', '今天页没有新增「该联系一下了」区块，也看不到这些建议', !document_has(await page.$('#todayFollowups')) && !/该联系一下了|之后还没有回复/.test(todayText), '');
    await shot('01-today-1440.png');

    // 2. The customer page shows one quiet sentence under 下一步, with no buttons.
    await openCustomer(page, ids[0]);
    const text = await hintText();
    check('hint-shows-reason', '客户页「下一步」下面有一行带日期的理由', text.startsWith(REASON), text || '没有这一行');
    const inFocus = await page.evaluate((sel) => { const el = document.querySelector(sel); return !!el && !!el.closest('.cw-focus'); }, HINT);
    check('hint-under-next-step', '这一行在「下一步」区域里', inFocus, '');
    const controls = await page.evaluate((sel) => { const el = document.querySelector(sel); return el ? el.querySelectorAll('button, a, input, select').length : -1; }, HINT);
    check('hint-has-no-controls', '这一行没有按钮或输入框，不需要任何操作', controls === 0, `${controls} 个控件`);
    check('no-explanations', '这一行没有解释文字', !/只有你能看到|系统|建议|AI/.test(text), text);
    await shot('02-customer-hint-1440.png');

    // 3. A newer record clears it: no click on any "done" button.
    const rec = await json(page, `/api/customers/${ids[0]}/follow_history`, 'POST', { activity_content: '已回复客户报价', activity_type: 'email', direction: 'outbound' });
    check('record-saved', '给这位客户补了一条沟通记录', rec.status < 300, `HTTP ${rec.status}`);
    await page.evaluate(() => refreshCustomerWorkspace());
    await page.waitForTimeout(800);
    const cleared = await hintText();
    check('newer-record-clears-it', '记了新沟通后，这一行自己消失', cleared === '', cleared || '');
    await shot('03-customer-cleared-1440.png');
    await closeCustomer(page);

    // 4. A dated next step makes the sentence unnecessary.
    const task = await json(page, `/api/customers/${ids[1]}/tasks`, 'POST', { title: '跟进样品测试结果', due_date: localDate(3) });
    check('task-created', '给第二位客户安排了带日期的下一步', task.status < 300, `HTTP ${task.status}`);
    await openCustomer(page, ids[1]);
    check('dated-next-step-hides-it', '有带日期的下一步时不再出现这一行', (await hintText()) === '', '');
    await closeCustomer(page);

    // 5. Switching customers never leaves the previous sentence behind.
    await openCustomer(page, ids[2]);
    check('third-customer-has-own-hint', '第三位客户有自己的一行', (await hintText()).startsWith(REASON), '');
    await closeCustomer(page);
    await openCustomer(page, ids[0]);
    check('no-leftover-after-switch', '换回第一位客户时没有残留上一位的那一行', (await hintText()) === '', '');
    await closeCustomer(page);

    // 6. Phones and tablets keep it inside the screen.
    for (const [label, vp, scale] of [['ipad-820', { width: 820, height: 1180 }, 1], ['iphone-390', { width: 390, height: 844 }, 1], ['win10-1366', { width: 1366, height: 768 }, 1], ['zoom200-720', { width: 720, height: 450 }, 2]]) {
      const ctx = await browser.newContext({ viewport: vp, deviceScaleFactor: scale, locale: 'zh-CN', hasTouch: label === 'iphone-390' });
      const p2 = await ctx.newPage();
      try {
        await p2.goto(`${options.url}/`, { waitUntil: 'load' });
        await json(p2, '/api/auth/login', 'POST', { user: 'hamid' });
        await openToday(p2, options.url, null);
        await openCustomer(p2, ids[2]);
        const t2 = await p2.evaluate((sel) => { const el = document.querySelector(sel); return el ? el.innerText.trim() : ''; }, HINT);
        check(`hint-visible-${label}`, `${label}：能看到这一行`, t2.startsWith(REASON), t2);
        const overflow = await p2.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth + 1);
        const clipped = await p2.evaluate((sel) => { const el = document.querySelector(sel); if (!el) return true; const r = el.getBoundingClientRect(); return r.right > window.innerWidth + 1 || r.left < -1; }, HINT);
        check(`no-overflow-${label}`, `${label}：没有横向滚动，这一行不被裁切`, !overflow && !clipped, `${overflow ? '有横向滚动 ' : ''}${clipped ? '被裁切' : ''}`);
        await p2.screenshot({ path: path.join(outDir, `0-${label}.png`) });
        report.screenshots.push(`0-${label}.png`);
      } catch (error) {
        check(`viewport-${label}`, `${label}：能打开客户页并看到这一行`, false, String(error.message).split('\n')[0].slice(0, 160));
      }
      await ctx.close();
    }
  } catch (error) {
    const full = String(error && error.message ? error.message : error);
    const message = full.split('\n')[0];
    if (process.env.REPRO_DEBUG) console.error(full);
    if (/ERR_CONNECTION|ECONNREFUSED|net::ERR_/.test(message)) throw Object.assign(new Error(message), { infra: true });
    report.checks.push({ id: 'scenario-completed', title: '场景能完整走完', pass: false, detail: message.slice(0, 300) });
    await shot('99-failure.png').catch(() => {});
  } finally {
    await context.close();
  }
}

function document_has(handle) { return !!handle; }

async function main() {
  const options = parseArgs(process.argv.slice(2));
  const outDir = path.resolve(ROOT, options.out);
  fs.mkdirSync(outDir, { recursive: true });
  const { chromium } = loadPlaywright();
  const report = { label: options.label, url: options.url, generatedAt: new Date().toISOString(), checks: [], screenshots: [], pageErrors: [] };
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
  for (const c of report.checks) console.log(`${c.pass ? 'PASS' : 'FAIL'}  ${c.id}  —  ${c.title}\n        ${c.detail}`);
  console.log(`\n${report.checks.length - failed.length}/${report.checks.length} 项通过；证据：${path.relative(ROOT, outDir) || outDir}`);
  process.exit(failed.length ? EXIT_DEFECT : EXIT_PASS);
}

main();
