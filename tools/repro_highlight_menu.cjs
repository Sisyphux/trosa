/*
 * Real-browser reproduction: 编辑沟通记录里选中文字后点「标色」颜色，文字没有被标色。
 *
 * Usage (see docs/REAL_BROWSER_REPRO.md):
 *   node tools/repro_highlight_menu.cjs --url http://127.0.0.1:18190 --label before --out docs/repro/highlight-menu/before
 *
 * Exit codes: 0 all checks pass, 20 defect present, 21 infrastructure failure.
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

async function runScenario(engineName, browser, options, outDir, report) {
  const context = await browser.newContext({ viewport: { width: 1100, height: 900 }, locale: 'zh-CN' });
  const page = await context.newPage();
  page.on('pageerror', (error) => report.pageErrors.push(`${engineName}: ${String(error.message).slice(0, 200)}`));
  const shot = async (name) => { await page.screenshot({ path: path.join(outDir, `${engineName}-${name}`) }); report.screenshots.push(`${engineName}-${name}`); };
  const check = (id, title, pass, detail) => report.checks.push({ id: `${engineName}:${id}`, title, pass: !!pass, detail });

  try {
    await page.goto(`${options.url}/`, { waitUntil: 'load' });
    const login = await json(page, '/api/auth/login', 'POST', { user: 'hamid' });
    if (login.status !== 200) throw new Error(`login failed: ${login.status}`);
    const cust = await json(page, '/api/customers', 'POST', { company: `标色复现${Date.now()}`, name: '联系人', country: 'China' });
    if (cust.status !== 201) throw new Error(`customer create failed: ${cust.status}`);
    const log = await json(page, `/api/customers/${cust.data.id}/follow_history`, 'POST', {
      activity_content: '客户询问我方亚克力板材详情，我方发送目录。客户表示已有板材代理，需求为 mix material。',
      activity_type: 'other', direction: 'two_way',
    });
    if (log.status >= 300) throw new Error(`log seed failed: ${log.status}`);
    await page.reload({ waitUntil: 'load' });
    await page.waitForFunction(() => typeof openFollowEditModal === 'function');
    // 缓存需要先有这条记录；直接走真实入口：先加载客户再打开编辑框
    await page.evaluate(async (id) => { await openEditModal(id); }, cust.data.id).catch(() => {});
    await page.waitForTimeout(1500);
    const logId = log.data.id || (log.data.follow_history && log.data.follow_history[0] && log.data.follow_history[0].id);
    await page.evaluate(async (id) => { await openFollowEditModal(id); }, logId);
    await page.waitForSelector('#followEditContent:visible', { timeout: 8000 });
    await page.waitForTimeout(600);

    // 真实鼠标：双击选一个词
    const box = await page.locator('#followEditContent').boundingBox();
    await page.mouse.dblclick(box.x + 60, box.y + 20);
    await page.waitForTimeout(400);
    const menuShown = await page.evaluate(() => !document.getElementById('highlightContextMenu').hidden);
    check('menu-appears', '选中文字后出现标色菜单', menuShown, `菜单可见：${menuShown}`);
    await shot('01-selected.png');

    // 真实鼠标点黄色
    const yb = await page.locator('#highlightContextMenu button[data-color="yellow"]').boundingBox();
    if (yb) await page.mouse.click(yb.x + yb.width / 2, yb.y + yb.height / 2);
    await page.waitForTimeout(400);
    const marks = await page.evaluate(() => document.querySelectorAll('#followEditContent mark.hl-yellow').length);
    check('text-gets-marked', '点黄色后选中文字被标色', marks > 0, `mark.hl-yellow 数量：${marks}`);
    await shot('02-after-click.png');
  } catch (error) {
    const message = String(error && error.message ? error.message : error).split('\n')[0];
    if (/ERR_CONNECTION|ECONNREFUSED|net::ERR_/.test(message)) throw Object.assign(new Error(message), { infra: true });
    report.checks.push({ id: `${engineName}:scenario-completed`, title: '场景能完整走完', pass: false, detail: message.slice(0, 300) });
    await shot('99-failure.png').catch(() => {});
  } finally {
    await context.close();
  }
}

async function main() {
  const options = parseArgs(process.argv.slice(2));
  const outDir = path.resolve(ROOT, options.out);
  fs.mkdirSync(outDir, { recursive: true });
  const pw = loadPlaywright();
  const report = { label: options.label, url: options.url, generatedAt: new Date().toISOString(), checks: [], screenshots: [], pageErrors: [] };
  for (const engineName of ['chromium', 'webkit']) {
    let browser;
    try {
      browser = await pw[engineName].launch({ headless: true });
      await runScenario(engineName, browser, options, outDir, report);
    } catch (error) {
      if (error.infra) { console.error(`基础设施失败：${error.message}`); process.exit(EXIT_INFRA); }
      console.error(`${engineName} 不可用：${error.message}`);
      if (engineName === 'chromium') process.exit(EXIT_INFRA);
    } finally {
      if (browser) await browser.close();
    }
  }
  fs.writeFileSync(path.join(outDir, 'report.json'), `${JSON.stringify(report, null, 2)}\n`, 'utf8');
  const failed = report.checks.filter((c) => !c.pass);
  for (const c of report.checks) console.log(`${c.pass ? 'PASS' : 'FAIL'}  ${c.id}  —  ${c.title}\n        ${c.detail}`);
  console.log(`\n${report.checks.length - failed.length}/${report.checks.length} 项通过`);
  process.exit(failed.length ? EXIT_DEFECT : EXIT_PASS);
}

main();
