/*
 * Real-browser check: picking a customer's timezone in 资料.
 * Expects: no long native <select>; a button that says the place in words with
 * the customer's local time; clicking opens a small searchable list; typing
 * "dubai" narrows it to 阿联酋, Enter saves it as manual, and a reload keeps it.
 * Also checks 迪拜 / UTC+4 searches and Esc closes without saving.
 *
 *   node tools/repro_customer_timezone_picker.cjs --url http://127.0.0.1:18312 --label after --out docs/repro/customer-timezone-picker/after
 * Exit: 0 pass, 20 defect, 21 infrastructure failure.
 */
'use strict';
const fs = require('fs');
const path = require('path');
const ROOT = path.resolve(__dirname, '..');
function arg(name, def) { const i = process.argv.indexOf(name); return i > 0 ? process.argv[i + 1] : def; }
const url = arg('--url', 'http://127.0.0.1:18312');
const label = arg('--label', 'run');
const out = arg('--out', '');
const width = Number(arg('--width', '1440'));
if (!out) { console.error('--out required'); process.exit(21); }
let chromium;
try { chromium = require(path.join(ROOT, 'browser-extension', 'node_modules', 'playwright')).chromium; } catch (e) { console.error('playwright missing'); process.exit(21); }

async function json(page, u, method, body) {
  return page.evaluate(async ({ u, method, body }) => {
    const r = await fetch(u, { method, credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: body ? JSON.stringify(body) : undefined });
    let d = null; try { d = await r.json(); } catch (e) {}
    return { status: r.status, data: d };
  }, { u, method, body });
}

(async () => {
  fs.mkdirSync(out, { recursive: true });
  const report = { label, url, width, checks: [], pageErrors: [] };
  const check = (id, expect, pass, detail) => report.checks.push({ id, expect, pass: !!pass, detail: detail || '' });
  let browser;
  try {
    browser = await chromium.launch({ headless: true });
    const ctx = await browser.newContext({ viewport: { width, height: 900 }, locale: 'zh-CN' });
    const page = await ctx.newPage();
    page.on('pageerror', (e) => report.pageErrors.push(String(e.message).slice(0, 200)));
    await page.goto(`${url}/`, { waitUntil: 'load' });
    await page.waitForFunction(() => typeof openEditModal === 'function', null, { timeout: 20000 });
    const login = await json(page, '/api/auth/login', 'POST', { user: 'hamid' });
    if (login.status !== 200) throw new Error('login ' + login.status);
    const made = await json(page, '/api/customers', 'POST', { name: 'Hobarts', company: 'Hobarts' });
    if (made.status !== 201) throw new Error('create ' + made.status);
    const id = made.data.id;
    const open = async () => {
      await page.goto(`${url}/`, { waitUntil: 'load' });
      await page.waitForFunction(() => typeof openEditModal === 'function', null, { timeout: 20000 });
      await page.waitForTimeout(1200);
      await page.evaluate((cid) => openEditModal(cid), id);
      await page.waitForTimeout(800);
      await page.evaluate(() => showCustomerSection('editTabBasic'));
      await page.waitForTimeout(500);
    };
    const shot = (name) => page.screenshot({ path: path.join(out, name) });
    await open();
    await shot('1-profile.png');
    check('no-select', '资料里没有长下拉框', (await page.locator('#customerEditModal select.cw-tz-select').count()) === 0);
    const trigger = page.locator('#customerEditModal .cw-tz-trigger');
    check('trigger', '时区是一个按钮', (await trigger.count()) === 1);
    if (await trigger.count()) {
      await trigger.click();
      await page.waitForTimeout(300);
      const search = page.locator('#customerEditModal .cw-tz-search');
      check('search-focus', '点开后搜索框自动获得焦点', await search.evaluate((el) => el === document.activeElement).catch(() => false));
      await shot('2-open.png');
      const box = await page.locator('#customerEditModal .cw-tz-pop').boundingBox();
      check('in-view', '弹层完整在屏幕内', box && box.x >= 0 && box.x + box.width <= width && box.y + box.height <= 900, JSON.stringify(box));
      const noIana = await page.locator('#customerEditModal .cw-tz-opt').allInnerTexts();
      check('no-iana', '列表里不显示 Asia/xxx 这类代号', noIana.length > 20 && !noIana.some((t) => /[A-Z][a-z]+\/[A-Z]/.test(t)), noIana.slice(0, 3).join(' | '));
      for (const [q, want] of [['迪拜', '阿联酋'], ['UTC+4', '阿联酋'], ['new york', '美国东部']]) {
        await search.fill(q);
        await page.waitForTimeout(150);
        const t = await page.locator('#customerEditModal .cw-tz-opt').allInnerTexts();
        check('search-' + q, `搜「${q}」能找到${want}`, t.some((x) => x.includes(want)) && t.length <= 6, t.join(' | '));
      }
      await search.press('Escape');
      await page.waitForTimeout(200);
      check('esc', 'Esc 关闭且不保存', (await page.locator('#customerEditModal .cw-tz-pop').count()) === 0 && (await page.locator('#customerEditModal').isVisible()));
      await trigger.click();
      await page.waitForTimeout(200);
      await page.locator('#customerEditModal .cw-tz-search').fill('dubai');
      await shot('3-search.png');
      await page.locator('#customerEditModal .cw-tz-search').press('Enter');
      await page.waitForTimeout(1000);
      await shot('4-saved.png');
      const after = await json(page, '/api/customers/' + id, 'GET');
      check('saved', '回车后保存为阿联酋（手动）', after.data && after.data.timezone === 'Asia/Dubai' && after.data.timezone_source === 'manual', JSON.stringify(after.data && [after.data.timezone, after.data.timezone_source]));
      await open();
      const face = await page.locator('#customerEditModal .cw-tz-trigger').innerText().catch(() => '');
      check('reload', '刷新后按钮显示「阿联酋」和对方此刻时间', face.includes('阿联酋') && /\d\d:\d\d/.test(face), face);
      await shot('5-reload.png');
    }
  } catch (e) {
    report.error = String(e && e.stack || e);
    fs.writeFileSync(path.join(out, 'report.json'), JSON.stringify(report, null, 2));
    console.error(report.error);
    if (browser) await browser.close();
    process.exit(21);
  }
  await browser.close();
  check('no-page-errors', '页面没有脚本错误', report.pageErrors.length === 0, report.pageErrors.join(' | '));
  fs.writeFileSync(path.join(out, 'report.json'), JSON.stringify(report, null, 2));
  for (const c of report.checks) console.log((c.pass ? 'PASS ' : 'FAIL ') + c.id + ' — ' + c.expect + (c.pass ? '' : '  [' + c.detail + ']'));
  process.exit(report.checks.every((c) => c.pass) ? 0 : 20);
})();
