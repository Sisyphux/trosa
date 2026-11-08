/*
 * Real-browser check: the customer's hand-written 「一般什么时候回」.
 * Expects: a plain text row in 资料 (no select, no extra button); nothing shown on
 * the first screen while empty; after typing a sentence it shows quietly under
 * the customer-local clock and survives a reload.
 *
 *   node tools/repro_customer_reply_habit.cjs --url http://127.0.0.1:18311 --label after --out docs/repro/customer-reply-habit/after
 * Exit: 0 pass, 20 defect (feature missing), 21 infrastructure failure.
 */
'use strict';
const fs = require('fs');
const path = require('path');
const ROOT = path.resolve(__dirname, '..');
function arg(name, def) { const i = process.argv.indexOf(name); return i > 0 ? process.argv[i + 1] : def; }
const url = arg('--url', 'http://127.0.0.1:18311');
const label = arg('--label', 'run');
const out = arg('--out', '');
if (!out) { console.error('--out required'); process.exit(21); }
let chromium;
try { chromium = require(path.join(ROOT, 'browser-extension', 'node_modules', 'playwright')).chromium; } catch (e) { console.error('playwright missing'); process.exit(21); }
const HABIT = '欧洲时间下午，周一最快';

async function json(page, u, method, body) {
  return page.evaluate(async ({ u, method, body }) => {
    const r = await fetch(u, { method, credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: body ? JSON.stringify(body) : undefined });
    let d = null; try { d = await r.json(); } catch (e) {}
    return { status: r.status, data: d };
  }, { u, method, body });
}

(async () => {
  fs.mkdirSync(out, { recursive: true });
  const report = { label, url, checks: [], pageErrors: [] };
  const check = (id, expect, pass, detail) => report.checks.push({ id, expect, pass: !!pass, detail: detail || '' });
  let browser;
  try {
    browser = await chromium.launch({ headless: true });
    const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, locale: 'zh-CN' });
    const page = await ctx.newPage();
    page.on('pageerror', (e) => report.pageErrors.push(String(e.message).slice(0, 200)));
    await page.goto(`${url}/`, { waitUntil: 'load' });
    await page.waitForFunction(() => typeof openEditModal === 'function', null, { timeout: 20000 });
    const login = await json(page, '/api/auth/login', 'POST', { user: 'hamid' });
    if (login.status !== 200) throw new Error('login ' + login.status);
    const made = await json(page, '/api/customers', 'POST', { name: 'Ana', company: 'Harbor Plastics', country: 'Spain' });
    if (made.status !== 201) throw new Error('create ' + made.status);
    const id = made.data.id;
    await page.goto(`${url}/`, { waitUntil: 'load' });
    await page.waitForFunction(() => typeof openEditModal === 'function', null, { timeout: 20000 });
    await page.waitForTimeout(1500);
    const open = async () => {
      await page.evaluate((cid) => openEditModal(cid), id);
      await page.waitForSelector('#customerClock', { timeout: 10000 });
      await page.waitForTimeout(700);
    };
    await open();
    const shot = async (name) => page.screenshot({ path: path.join(out, name) });
    const section = (tab) => page.evaluate((t) => showCustomerSection(t), tab).then(() => page.waitForTimeout(500));
    const input = page.locator('.cw-habit-input');
    await shot('1-empty.png');
    await section('editTabBasic');
    await shot('1b-profile-empty.png');
    check('row', '资料里有一行手写文字栏，不是下拉', (await input.count()) === 1);
    check('empty-quiet', '没填时首屏什么都不显示', (await page.locator('#customerReplyHabit').count()) === 0);
    if (await input.count()) {
      await input.fill(HABIT);
      await input.press('Tab');
      await page.waitForTimeout(900);
      await shot('2a-profile-filled.png');
      await section('editTabOutreach');
      const t = await page.locator('#customerReplyHabit').first().innerText().catch(() => '');
      check('shown', '填写后在对方时钟旁显示这一句', t.includes(HABIT), t);
      const clockHas = await page.evaluate(() => !!document.querySelector('#customerClock #customerReplyHabit'));
      check('beside-clock', '这句话在时钟块里', clockHas);
      await shot('2-filled.png');
      await page.reload({ waitUntil: 'load' });
      await page.waitForFunction(() => typeof openEditModal === 'function', null, { timeout: 20000 });
      await page.waitForTimeout(1500);
      await open();
      const again = await page.locator('#customerReplyHabit').first().innerText().catch(() => '');
      check('persist', '刷新后仍在', again.includes(HABIT), again);
      await shot('3-after-reload.png');
      await section('editTabBasic');
      await input.fill('');
      await input.press('Tab');
      await section('editTabOutreach');
      await page.waitForTimeout(800);
      check('clear', '清空后又什么都不显示', (await page.locator('#customerReplyHabit').count()) === 0);
    }
    // A weekday, so the working-hours colour shows (the live date may be a weekend).
    const ctx2 = await browser.newContext({ viewport: { width: 1440, height: 900 }, locale: 'zh-CN' });
    const p2 = await ctx2.newPage();
    await p2.clock.install({ time: new Date('2026-11-10T10:00:00Z') });
    await p2.goto(`${url}/`, { waitUntil: 'load' });
    await p2.waitForFunction(() => typeof openEditModal === 'function', null, { timeout: 20000 });
    await json(p2, '/api/auth/login', 'POST', { user: 'hamid' });
    await json(p2, `/api/customers/${id}`, 'PUT', { reply_habit: HABIT });
    await p2.goto(`${url}/`, { waitUntil: 'load' });
    await p2.waitForFunction(() => typeof openEditModal === 'function', null, { timeout: 20000 });
    await p2.waitForTimeout(1500);
    await p2.evaluate((cid) => openEditModal(cid), id);
    await p2.waitForSelector('#customerClock', { timeout: 10000 });
    await p2.waitForTimeout(700);
    await p2.screenshot({ path: path.join(out, '4-weekday.png') });
    check('no-page-errors', '页面没有报错', report.pageErrors.length === 0, report.pageErrors.join('; '));
  } catch (e) {
    console.error('infra:', e.message);
    if (browser) await browser.close();
    process.exit(21);
  }
  await browser.close();
  fs.writeFileSync(path.join(out, 'report.json'), JSON.stringify(report, null, 2));
  const bad = report.checks.filter((c) => !c.pass);
  report.checks.forEach((c) => console.log(`${c.pass ? 'PASS' : 'FAIL'} ${c.id}: ${c.expect} ${c.detail}`));
  process.exit(bad.length ? 20 : 0);
})();
