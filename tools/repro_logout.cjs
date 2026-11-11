/*
 * Real-browser reproduction: "there is no way to sign out" (页面没有退出登录).
 *
 * The navigation index only offered 切换账号, which showed the account chooser
 * but left the server session alive: reloading the page went straight back into
 * the same account.  This walks the path a member takes on a shared computer:
 * sign in, sign out from the navigation index, reload, and checks that the
 * session really ended.  It also checks the not-signed-in weekly viewer still
 * has a way to reach the account chooser.
 *
 * Usage (see docs/REAL_BROWSER_REPRO.md):
 *   node tools/repro_logout.cjs --url http://127.0.0.1:18190 --label after --out docs/repro/logout/after
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

// The account button is the last tool in the navigation index's "more" menu.
const ACCOUNT_BUTTON = '#roomIndex .room-index-more button:last-child';
const LOGIN_SHOWN = () => getComputedStyle(document.getElementById('loginOverlay')).display !== 'none';

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

async function accountButtonLabel(page) {
  return page.locator(ACCOUNT_BUTTON).evaluate((el) => ({
    text: (el.textContent || '').trim(),
    aria: el.getAttribute('aria-label') || '',
    shown: el.getClientRects().length > 0 && el.getBoundingClientRect().height > 0,
  }));
}

async function signedIn(page) {
  return page.evaluate(async () => {
    const resp = await fetch('/api/auth/me', { credentials: 'include' });
    const data = await resp.json().catch(() => ({}));
    return !!data.logged_in;
  });
}

async function runScenario(browser, options, outDir, report, width) {
  const context = await browser.newContext({ viewport: { width, height: 760 }, locale: 'zh-CN' });
  const page = await context.newPage();
  page.on('pageerror', (error) => report.pageErrors.push(String(error.message).slice(0, 200)));
  const shot = async (name) => {
    await settle(page);
    await page.screenshot({ path: path.join(outDir, name) });
    report.screenshots.push(name);
  };
  const check = (id, title, pass, detail) => report.checks.push({ id: `${id}-${width}`, title: `${title}（${width}px）`, pass: !!pass, detail });
  const hideLan = () => page.addStyleTag({
    content: '.entry-lan, [aria-label^="复制其他设备访问地址"] { visibility: hidden !important; }',
  });

  try {
    // 1. Not signed in: the internal viewer lands on the weekly board.
    await page.goto(`${options.url}/`, { waitUntil: 'load' });
    await page.waitForSelector('#page-overview.active', { timeout: 20000 });
    await hideLan();
    await openIndex(page);
    const weeklyLabel = await accountButtonLabel(page);
    await shot(`01-weekly-index-${width}.png`);
    check('weekly-viewer-can-sign-in', '只看周报、未登录时，导航里有进入账号选择的按钮，且不叫“退出登录”',
      weeklyLabel.shown && weeklyLabel.text && weeklyLabel.text !== '退出登录', JSON.stringify(weeklyLabel));

    // 2. Sign in as hamid through that button.
    await page.click(ACCOUNT_BUTTON);
    await page.waitForSelector('#loginUsers [data-user-id="hamid"]', { state: 'visible', timeout: 8000 });
    await page.waitForTimeout(600);
    let inApp = false;
    for (let attempt = 0; attempt < 3 && !inApp; attempt += 1) {
      await page.click('#loginUsers [data-user-id="hamid"]', { timeout: 5000 }).catch(() => {});
      inApp = await page.waitForFunction(
        () => getComputedStyle(document.getElementById('loginOverlay')).display === 'none'
          && !!document.querySelector('#page-dashboard.active'),
        null, { timeout: 6000 },
      ).then(() => true, () => false);
    }
    check('signed-in', '选择成员后进入今天页', inApp, inApp ? '已进入' : '未能进入今天页');
    if (!inApp) return;

    // 3. Signed in: the index offers a clearly named sign-out.
    await openIndex(page);
    const signedInLabel = await accountButtonLabel(page);
    await shot(`02-signed-in-index-${width}.png`);
    check('sign-out-button-named', '登录后，导航里有“退出登录”按钮', signedInLabel.shown && signedInLabel.text === '退出登录',
      JSON.stringify(signedInLabel));

    // 4. Use it: the account chooser appears and the server session ends.
    await page.click(ACCOUNT_BUTTON);
    const chooserShown = await page.waitForFunction(LOGIN_SHOWN, null, { timeout: 8000 }).then(() => true, () => false);
    await hideLan();
    await shot(`03-after-sign-out-${width}.png`);
    check('chooser-after-sign-out', '点击后回到账号选择', chooserShown, chooserShown ? '已显示' : '未显示账号选择');
    const sessionAfterClick = await signedIn(page);
    check('session-ended', '点击后服务器上的登录已结束', !sessionAfterClick, sessionAfterClick ? '服务器仍认为已登录' : '已结束');

    // 5. Reload: nobody is dropped back into the previous account.  On the office
    // LAN a reload always opens the weekly board first, so the page alone cannot
    // prove it; the session check is what decides (outside the LAN a live session
    // reopens the account directly).
    await page.reload({ waitUntil: 'load' });
    await page.waitForTimeout(2500);
    await hideLan();
    const backInApp = await page.evaluate(() => getComputedStyle(document.getElementById('loginOverlay')).display === 'none'
      && !!document.querySelector('#page-dashboard.active'));
    const sessionAfterReload = await signedIn(page);
    await shot(`04-after-reload-${width}.png`);
    check('reload-stays-signed-out', '刷新页面后不会回到刚才的账号', !backInApp && !sessionAfterReload,
      backInApp ? '刷新后直接回到了 hamid 的今天页' : (sessionAfterReload ? '刷新后服务器仍是 hamid 登录（办公网外会直接进入账号）' : '保持未登录'));

    // 6. Office LAN: sign in, reload (the app opens the weekly board even with a
    // live session), then use the button there.  The leftover session must end.
    if (await page.evaluate(() => getComputedStyle(document.getElementById('loginOverlay')).display === 'none')) {
      await openIndex(page);
      await page.click(ACCOUNT_BUTTON);
    }
    await page.waitForSelector('#loginUsers [data-user-id="hamid"]', { state: 'visible', timeout: 8000 });
    await page.waitForTimeout(600);
    await page.click('#loginUsers [data-user-id="hamid"]', { timeout: 5000 }).catch(() => {});
    await page.waitForSelector('#page-dashboard.active', { timeout: 8000 }).catch(() => {});
    await page.reload({ waitUntil: 'load' });
    await page.waitForSelector('#page-overview.active', { timeout: 20000 });
    await hideLan();
    const leftover = await signedIn(page);
    await openIndex(page);
    await page.click(ACCOUNT_BUTTON);
    await page.waitForFunction(LOGIN_SHOWN, null, { timeout: 8000 }).catch(() => {});
    const leftoverAfter = await signedIn(page);
    check('lan-leftover-session-ended', '办公网里登录后刷新、再点导航按钮，残留的登录会被结束',
      leftover && !leftoverAfter, `刷新后仍登录=${leftover}，点击后仍登录=${leftoverAfter}`);
  } catch (error) {
    const message = String(error && error.message ? error.message : error).split('\n')[0];
    if (/ERR_CONNECTION|ECONNREFUSED|net::ERR_/.test(message)) throw Object.assign(new Error(message), { infra: true });
    report.checks.push({ id: `scenario-completed-${width}`, title: `场景能完整走完（${width}px）`, pass: false, detail: message.slice(0, 300) });
    await shot(`99-failure-${width}.png`).catch(() => {});
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
    for (const width of [1440, 375]) await runScenario(browser, options, outDir, report, width);
  } catch (error) {
    console.error(error.infra ? `基础设施失败：${error.message}（请确认服务在 ${options.url} 运行）` : `浏览器不可用或场景异常：${error.message}`);
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
