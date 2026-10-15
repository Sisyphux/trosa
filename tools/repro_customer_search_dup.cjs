/*
 * Real-browser reproduction: 同一家公司有两条记录时，搜索只显示一行，而且不说还有
 * 另一条 —— 正式库里就出现过「显示的正是没有沟通记录的空壳，带外联事实的那条被藏住」。
 *
 * The scenario seeds a same-name pair (one empty shell, one holding a customer
 * reply) plus a unique control company in an isolated dev database, searches the
 * customer ledger, and checks that the shown row holds the communication and
 * says how many same-name records it stands for.
 *
 * Usage (see docs/REAL_BROWSER_REPRO.md):
 *   node tools/repro_customer_search_dup.cjs --url http://127.0.0.1:18190 --label before --out docs/repro/customer-search-dup/before
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

async function ledgerRows(page) {
  return page.evaluate(() => Array.from(document.querySelectorAll('#ledgerHost .ld-ent')).map((row) => ({
    company: (row.querySelector('.ld-co') || {}).textContent || '',
    snippet: (row.querySelector('.ld-sn') || {}).textContent || '',
    when: (row.querySelector('.ld-wh') || {}).textContent || '',
    dup: (row.querySelector('.ld-dup') || {}).textContent || '',
    label: row.getAttribute('aria-label') || '',
  })));
}

async function searchLedger(page, query) {
  await page.evaluate(() => switchPage('customers'));
  await page.waitForSelector('#page-customers.active', { timeout: 20000 });
  await page.waitForSelector('#ledgerSearch', { timeout: 15000 });
  await page.fill('#ledgerSearch', query);
  await page.waitForTimeout(2000);
  await settle(page);
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
    // 1. Seed in the isolated dev database: a same-name pair (empty shell first,
    //    the record holding the reply second) and one unique control company.
    await page.goto(`${options.url}/`, { waitUntil: 'load' });
    const login = await json(page, '/api/auth/login', 'POST', { user: 'hamid' });
    if (login.status !== 200) throw new Error(`login failed: ${login.status}`);
    const mark = Date.now();
    const company = `同名复现公司${mark}`;
    const unique = `唯一对照公司${mark}`;
    const shell = await json(page, '/api/customers', 'POST', { company, name: '空壳联系人', country: 'China' });
    if (shell.status !== 201) throw new Error(`shell create failed: ${shell.status} ${JSON.stringify(shell.data)}`);
    const talking = await json(page, '/api/customers', 'POST', { company, name: '有沟通联系人', country: 'China' });
    if (talking.status !== 201) throw new Error(`twin create failed: ${talking.status} ${JSON.stringify(talking.data)}`);
    const reply = await json(page, `/api/customers/${talking.data.id}/follow_history`, 'POST', {
      activity_content: '客户回信：确认样品与报价，下周下单。', activity_type: 'customer_reply', direction: 'inbound',
    });
    if (reply.status >= 300) throw new Error(`reply seed failed: ${reply.status} ${JSON.stringify(reply.data)}`);
    const solo = await json(page, '/api/customers', 'POST', { company: unique, name: '对照联系人', country: 'China' });
    if (solo.status !== 201) throw new Error(`control create failed: ${solo.status} ${JSON.stringify(solo.data)}`);
    report.measurements = { company, unique, shell_id: shell.data.id, talking_id: talking.data.id };

    // 2. Search the duplicated company: one row, and it must stand for both.
    await searchLedger(page, company);
    await shot('01-search-duplicate-1440.png');
    const rows = await ledgerRows(page);
    const row = rows[0] || null;
    check('search-shows-one-row',
      '搜索同名公司只出现一行（同一家公司折叠成一条）',
      rows.length === 1,
      `行数：${rows.length}`);
    check('same-name-disclosed',
      '这一行说明还有 1 条同名记录',
      !!row && /另有\s*1\s*条同名记录/.test(row.dup),
      `提示：${row ? row.dup || '没有' : '没有行'}`);
    check('shown-row-holds-the-communication',
      '显示的是有沟通记录的那条，不是空壳',
      !!row && row.snippet.indexOf('尚无沟通记录') < 0 && /回信/.test(row.snippet),
      `摘要：${row ? row.snippet : '没有行'}`);
    check('row-label-names-the-duplicate',
      '这一行的无障碍名称也说明还有同名记录',
      !!row && /另有\s*1\s*条同名记录/.test(row.label),
      `名称：${row ? row.label : '没有行'}`);

    // 3. A company without a twin must not claim one.
    await searchLedger(page, unique);
    await shot('02-search-unique-1440.png');
    const uniqueRows = await ledgerRows(page);
    check('unique-company-has-no-hint',
      '没有同名的公司不出现同名提示',
      uniqueRows.length === 1 && !uniqueRows[0].dup,
      `行数：${uniqueRows.length}；提示：${uniqueRows[0] ? uniqueRows[0].dup || '没有' : '没有行'}`);
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
