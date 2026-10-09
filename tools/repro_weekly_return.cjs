/*
 * Real-browser reproduction: "after opening the weekly board, the normal workbench
 * cannot be reached again" (周报页回不去操作台).
 *
 * This runs in a fresh headless Chromium (the Playwright build pinned in
 * browser-extension/package-lock.json), not in jsdom, so CSS cascade and layout
 * are real.  It walks the path a LAN internal viewer takes: the app boots into the
 * weekly board, the navigation index is opened, and the way back to the workbench
 * is tried.  Every check states the expected behaviour; a check that fails is the
 * defect reproducing.
 *
 * Usage (see docs/REAL_BROWSER_REPRO.md):
 *   node tools/repro_weekly_return.cjs --url http://127.0.0.1:18190 --label after --out docs/repro/weekly-return/after
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

// Sizes: 945 is the width of the reported screenshot (1890 px at 2x).
const WIDTHS = [945, 1440, 375];
const PERSONAL_LABELS = ['今天', 'Inbox', '客户'];

// Measures every row of the navigation index as a user sees it: whether the row
// is laid out at all, and whether its name is laid out (not just present in the DOM).
function measureIndexRowsInPage() {
  const shown = (el) => !!el && el.getClientRects().length > 0 && el.getBoundingClientRect().height > 0;
  return Array.from(document.querySelectorAll('#roomIndex .room-index-nav .nav-item')).map((row) => {
    const name = row.querySelector('.nav-name');
    return {
      label: row.getAttribute('aria-label'),
      rowShown: shown(row),
      nameShown: shown(name),
    };
  });
}

function summarise(rows) {
  return rows.map((row) => `${row.label}:${row.rowShown ? (row.nameShown ? 'shown' : 'row-only') : 'hidden'}`).join(', ');
}

// The app animates page and index changes with View Transitions.  A frame taken
// while one is running still shows the previous screen, so wait for it to finish.
async function settle(page) {
  await page.waitForFunction(
    () => !document.documentElement.dataset.motionType && !document.activeViewTransition,
    null,
    { timeout: 5000 },
  ).catch(() => {});
  await page.waitForTimeout(500);
}

async function openIndex(page) {
  const isOpen = await page.evaluate(() => !document.getElementById('roomIndex').hidden);
  if (isOpen) return;
  await page.click('#roomBrand');
  await page.waitForFunction(() => !document.getElementById('roomIndex').hidden, null, { timeout: 8000 });
  await settle(page);
}

async function closeIndex(page) {
  const isOpen = await page.evaluate(() => !document.getElementById('roomIndex').hidden);
  if (!isOpen) return;
  await page.keyboard.press('Escape');
  await page.waitForFunction(() => document.getElementById('roomIndex').hidden, null, { timeout: 8000 });
  await page.waitForTimeout(300);
}

async function runScenario(browser, options, outDir, report) {
  const context = await browser.newContext({ viewport: { width: 945, height: 700 }, locale: 'zh-CN' });
  const page = await context.newPage();
  const unauthorized = [];
  page.on('response', (response) => {
    if (response.status() === 401) unauthorized.push(new URL(response.url()).pathname);
  });
  page.on('pageerror', (error) => report.pageErrors.push(String(error.message).slice(0, 200)));
  const shot = async (name) => {
    const file = path.join(outDir, name);
    await settle(page);
    await page.screenshot({ path: file });
    report.screenshots.push(name);
  };
  const check = (id, title, pass, detail) => {
    report.checks.push({ id, title, pass: !!pass, detail });
  };

  try {
    // 1. Boot: an internal viewer lands on the weekly board, as in the reported case.
    await page.goto(`${options.url}/`, { waitUntil: 'load' });
    await page.waitForSelector('#page-overview.active', { timeout: 20000 });
    // The sign-in screen lists this machine's office LAN addresses as copyable chips.
    // They are not part of the defect and must not be saved into evidence (the repo
    // is pushed to GitHub), so hide the chips in screenshots.  Layout is unchanged.
    await page.addStyleTag({
      content: '.entry-lan, [aria-label^="复制其他设备访问地址"] { visibility: hidden !important; }',
    });
    await shot('01-weekly-board-945.png');

    // 2. Open the navigation index from the weekly board.
    await openIndex(page);
    await shot('02-index-weekly-945.png');
    const weeklyRows = await page.evaluate(measureIndexRowsInPage);
    report.measurements.weeklyOnly945 = weeklyRows;
    const personalShownInWeekly = weeklyRows.filter((r) => PERSONAL_LABELS.includes(r.label) && r.rowShown);
    check('weekly-only-hides-personal-entries',
      '只看周报时，不显示需要登录的个人入口（今天 / Inbox / 客户）',
      personalShownInWeekly.length === 0,
      personalShownInWeekly.length ? `仍显示：${personalShownInWeekly.map((r) => r.label).join('、')}` : '未显示');
    const unlabelledVisible = weeklyRows.filter((r) => r.rowShown && !r.nameShown);
    check('weekly-only-visible-rows-labelled',
      '只看周报时，导航里可见的每一行都有名字',
      unlabelledVisible.length === 0,
      unlabelledVisible.length ? `无名字的行：${unlabelledVisible.map((r) => r.label || '(无名)').join('、')}` : summarise(weeklyRows));

    // 3. Try a personal entry from the weekly board, if the index offers one.
    const todayRow = page.locator('#roomIndex [data-nav-page="dashboard"]');
    const todayRowShown = await todayRow.evaluate((el) => el.getClientRects().length > 0 && el.getBoundingClientRect().height > 0);
    const before401 = unauthorized.length;
    if (todayRowShown) {
      await todayRow.click();
      await page.waitForTimeout(1500);
      const attemptPaths = unauthorized.slice(before401);
      const loginShown = await page.evaluate(() => getComputedStyle(document.getElementById('loginOverlay')).display !== 'none');
      check('weekly-only-personal-entry-no-login-bounce',
        '只看周报时点个人入口，不会弹出“登录已过期”并跳回登录页',
        attemptPaths.length === 0 && !loginShown,
        attemptPaths.length ? `点击“今天”后返回 401：${attemptPaths.join('、')}` : (loginShown ? '点击后跳回了账号选择' : '未发生 401'));
    } else {
      check('weekly-only-personal-entry-no-login-bounce',
        '只看周报时点个人入口，不会弹出“登录已过期”并跳回登录页',
        true,
        '周报模式不提供个人入口，无法误点');
      await page.click('#roomIndex .room-index-more button[aria-label="切换账号"]');
    }

    // 4. Get back to the workbench through the account chooser.  The chooser is
    // re-rendered when it is opened (and again after a 401 bounce), so wait for it
    // to settle and retry the selection a few times.  Success means the sign-in
    // overlay is hidden AND the workbench is the active page: the dashboard
    // section can already be marked active behind the overlay after a bounce.
    await page.waitForFunction(() => getComputedStyle(document.getElementById('loginOverlay')).display !== 'none',
      null, { timeout: 8000 }).catch(() => null);
    await page.waitForSelector('#loginUsers [data-user-id="hamid"]', { state: 'visible', timeout: 8000 }).catch(() => null);
    await page.waitForTimeout(800);
    await shot('03-account-chooser-945.png');
    const returnStart = unauthorized.length;
    let attempts = 0;
    let backOnToday = false;
    while (attempts < 3 && !backOnToday) {
      attempts += 1;
      await page.click('#loginUsers [data-user-id="hamid"]', { timeout: 5000 }).catch(() => {});
      backOnToday = await page.waitForFunction(
        () => getComputedStyle(document.getElementById('loginOverlay')).display === 'none'
          && !!document.querySelector('#page-dashboard.active'),
        null, { timeout: 6000 },
      ).then(() => true, () => false);
    }
    await shot('04-workbench-after-return-945.png');
    const returnPaths = unauthorized.slice(returnStart);
    check('way-back-lands-on-today',
      '从周报经“切换账号”选择成员后，回到个人操作台（今天）',
      backOnToday,
      backOnToday ? `今天页已显示（选择成员尝试 ${attempts} 次）` : `选择成员 ${attempts} 次后仍没有回到今天页`);
    check('way-back-no-401',
      '返回操作台的过程中没有 401',
      returnPaths.length === 0,
      returnPaths.length ? returnPaths.join('、') : '无 401');

    // 5. Signed in: the personal rows must carry their names at every width.
    for (const width of WIDTHS) {
      await page.setViewportSize({ width, height: 700 });
      await page.waitForTimeout(400);
      await closeIndex(page);
      await openIndex(page);
      const rows = await page.evaluate(measureIndexRowsInPage);
      report.measurements[`signedIn${width}`] = rows;
      if (width === 945 || width === 375) await shot(`05-index-signed-in-${width}.png`);
      const unlabelled = PERSONAL_LABELS.map((label) => rows.find((r) => r.label === label))
        .filter((r) => !r || !r.rowShown || !r.nameShown);
      check(`signed-in-labels-at-${width}`,
        `登录后，导航里“今天 / Inbox / 客户”在 ${width}px 宽时都显示名字`,
        unlabelled.length === 0,
        unlabelled.length ? `无名字：${unlabelled.map((r) => (r ? r.label : '(缺失)')).join('、')}` : summarise(rows));
    }
  } catch (error) {
    const message = String(error && error.message ? error.message : error).split('\n')[0];
    if (/ERR_CONNECTION|ECONNREFUSED|net::ERR_/.test(message)) throw Object.assign(new Error(message), { infra: true });
    report.checks.push({ id: 'scenario-completed', title: '场景能完整走完', pass: false, detail: message.slice(0, 300) });
    await shot('99-failure.png').catch(() => {});
  } finally {
    report.unauthorizedPaths = unauthorized;
    await context.close();
  }
}

async function main() {
  const options = parseArgs(process.argv.slice(2));
  const outDir = path.resolve(ROOT, options.out);
  fs.mkdirSync(outDir, { recursive: true });
  const { chromium } = loadPlaywright();
  const report = {
    label: options.label,
    url: options.url,
    generatedAt: new Date().toISOString(),
    checks: [],
    measurements: {},
    screenshots: [],
    pageErrors: [],
    unauthorizedPaths: [],
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
