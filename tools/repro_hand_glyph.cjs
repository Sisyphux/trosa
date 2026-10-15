#!/usr/bin/env node
/*
 * 「一笔」五期（图标优先第二版）真实浏览器验收。
 *
 *   node tools/repro_hand_glyph.cjs --url http://127.0.0.1:18190 --label after --out docs/repro/hand-glyph-p5/after
 *
 * 重点是用户的硬要求：任何图标 / 符号，鼠标放上去（或键盘聚焦）都要显示功能名称。
 * 检查：提示层出现且文字正确、移开后消失、原生 title 被接管（不出现两层）；工作区分区标签
 * 当前显示名称、其余只留图标（触屏保留名称）；状态符号带名称；数字被“写上去”；推迟时旧日期划掉、
 * 失败撤回；归档时公司名划掉、失败撤回；减少动态下不播动画。
 * 服务需用 CRM_SEED_DEMO_DATA=1 启动。退出码：0 全部通过；20 有失败；21 基础设施故障。
 */
const path = require('path');
const fs = require('fs');
const { chromium } = require(path.join(__dirname, '..', 'browser-extension', 'node_modules', 'playwright'));

const args = process.argv.slice(2);
const opt = (n, d) => { const i = args.indexOf('--' + n); return i >= 0 ? args[i + 1] : d; };
const url = opt('url'); const label = opt('label', 'run'); const out = path.resolve(opt('out', 'repro-out'));
if (!url) { console.error('缺少 --url'); process.exit(21); }
fs.mkdirSync(out, { recursive: true });

const checks = []; const errors = [];
const check = (name, ok, detail) => checks.push({ name, ok: !!ok, detail: detail === undefined ? '' : String(detail) });

async function login(p) {
  await p.evaluate(() => { try { showLogin(); } catch (e) { /* 已在登录页 */ } });
  await p.waitForTimeout(400);
  await p.locator('button:visible, [role=button]:visible', { hasText: 'Hamid' }).first().click({ timeout: 5000 });
  await p.waitForTimeout(1500);
}

// 悬停到元素上，等提示层，返回提示文字（没有则为空串）
async function tipOf(p, locator, wait = 450) {
  await p.mouse.move(2, 2);
  await p.waitForTimeout(80);
  await locator.hover({ timeout: 3000 });
  await p.waitForTimeout(wait);
  return p.evaluate(() => { const t = document.getElementById('handTip'); return t && t.classList.contains('is-on') ? t.textContent : ''; });
}

(async () => {
  let browser;
  try { browser = await chromium.launch(); } catch (e) { console.error('浏览器不可用：' + e.message); process.exit(21); }
  try {
    for (const sc of [
      { key: 'wide', viewport: { width: 1440, height: 900 } },
      { key: 'reduced', viewport: { width: 1440, height: 900 }, extra: { reducedMotion: 'reduce' } },
      { key: 'iphone', viewport: { width: 390, height: 844 }, extra: { hasTouch: true, isMobile: true, deviceScaleFactor: 2 } },
    ]) {
      const touch = sc.key === 'iphone';
      const ctx = await browser.newContext({ viewport: sc.viewport, ...(sc.extra || {}) });
      const p = await ctx.newPage();
      p.on('pageerror', (e) => errors.push(`[${sc.key}] ${e.message}`));
      await p.goto(url); await p.waitForTimeout(800);
      await login(p);

      if (!touch) {
        // Today：快捷图标
        const search = p.locator('.command-shelf .command-icon').first();
        const t1 = await tipOf(p, search);
        check(`${sc.key}: 悬停“搜索”图标显示名称`, t1 === '搜索客户', t1);
        const titleLeft = await search.getAttribute('title');
        check(`${sc.key}: 原生 title 已被接管（不出现两层提示）`, !titleLeft, titleLeft);
        await p.mouse.move(700, 700); await p.waitForTimeout(200);
        const gone = await p.evaluate(() => !document.getElementById('handTip').classList.contains('is-on'));
        check(`${sc.key}: 移开后提示消失`, gone);
        await p.screenshot({ path: path.join(out, `${sc.key}-today.png`) });

        // 键盘聚焦也显示
        await p.evaluate(() => document.activeElement && document.activeElement.blur());
        await p.keyboard.press('Tab'); await p.keyboard.press('Tab'); await p.keyboard.press('Tab');
        await p.waitForTimeout(250);
        const focused = await p.evaluate(() => { const a = document.activeElement; const t = document.getElementById('handTip'); return { name: a && (a.getAttribute('aria-label') || a.dataset.tip || ''), tip: t && t.classList.contains('is-on') ? t.textContent : '' }; });
        check(`${sc.key}: 键盘聚焦图标按钮时也显示名称`, !focused.name || focused.tip === focused.name || focused.tip.length > 0, JSON.stringify(focused));

        // 状态符号：Today 行里的状态（时区未知）
        const stOnToday = await p.locator('#todayReminders .tide-c3 .st').count();
        check(`${sc.key}: Today 行状态是符号`, stOnToday >= 1, stOnToday);
        if (stOnToday) {
          const tip = await tipOf(p, p.locator('#todayReminders .tide-c3 .st').first());
          check(`${sc.key}: 悬停状态符号显示名称`, /时区未知|开窗|没有窗口/.test(tip), tip);
        }
      }

      // 客户工作区
      const id = await p.evaluate(async () => { const r = await fetch('/api/customers?limit=1'); const j = await r.json(); const a = j.customers || j.items || j; return a[0] && a[0].id; });
      await p.evaluate((i) => openEditModal(i), id);
      await p.waitForTimeout(2000);
      await p.screenshot({ path: path.join(out, `${sc.key}-workspace.png`) });

      const tabs = await p.evaluate(() => [...document.querySelectorAll('#customerEditModal .cw-tab')].map((b) => {
        const n = b.querySelector('.cw-tab-name'); const r = n.getBoundingClientRect();
        return { name: n.textContent, label: b.getAttribute('aria-label'), selected: b.getAttribute('aria-selected') === 'true', w: Math.round(r.width) };
      }));
      check(`${sc.key}: 五个分区标签都有可访问名称`, tabs.length === 5 && tabs.every((t) => t.label === t.name), JSON.stringify(tabs.map((t) => t.label)));
      const active = tabs.find((t) => t.selected);
      check(`${sc.key}: 当前分区显示名称`, active && active.w > 10, active && active.w);
      const inactive = tabs.filter((t) => !t.selected);
      if (touch) check(`${sc.key}: 触屏保留所有名称`, inactive.every((t) => t.w > 10), JSON.stringify(inactive.map((t) => t.w)));
      else check(`${sc.key}: 其余分区只留图标`, inactive.every((t) => t.w <= 2), JSON.stringify(inactive.map((t) => t.w)));

      if (!touch) {
        const tabTip = await tipOf(p, p.locator('#customerEditModal .cw-tab').nth(2));
        check(`${sc.key}: 悬停分区图标显示名称`, tabTip === '联系人', tabTip);
        const readTip = await tipOf(p, p.locator('#customerEditModal .cw-readouts > div').first());
        check(`${sc.key}: 悬停读数图标显示名称`, readTip === '沟通记录', readTip);
        const exportTip = await tipOf(p, p.locator('#customerEditModal .cw-tools .cw-link-ic').first());
        check(`${sc.key}: 悬停导出图标显示名称`, /^导出邮箱/.test(exportTip), exportTip);
        const kbdTip = await tipOf(p, p.locator('#customerEditModal .cw-close'));
        check(`${sc.key}: 悬停关闭显示名称`, kbdTip.length > 0, kbdTip);
        await p.mouse.move(700, 700);
      }

      // 数字被“写上去”
      const written = await p.evaluate(() => {
        const el = document.createElement('b'); el.textContent = '1'; document.body.appendChild(el);
        setWritten(el, '2');
        const cls = el.classList.contains('hand-write');
        el.remove();
        return cls;
      });
      if (sc.key === 'reduced') check('reduced: 数字变化不播动画', written === false, written);
      else check(`${sc.key}: 数字变化被写上去`, written === true, written);

      // 推迟：旧日期划掉，失败撤回
      await p.route(/\/api\/reminders\/\d+\/reschedule$/, async (route) => { await new Promise((r) => setTimeout(r, 1000)); await route.abort('failed'); });
      const postpone = p.locator('#customerTaskQuickActions button', { hasText: '明天' }).first();
      if (await postpone.count()) {
        await postpone.click();
        await p.waitForTimeout(250);
        const struck = await p.locator('#customerNextTask .cw-when .hand-strike').count();
        check(`${sc.key}: 点“明天”旧日期立刻划掉`, struck === 1, struck);
        await p.waitForTimeout(2200);
        const undone = await p.locator('#customerNextTask .cw-when .hand-strike').count();
        check(`${sc.key}: 推迟失败后撤回划线`, undone === 0, undone);
      } else check(`${sc.key}: 有“明天”顺延入口`, false);
      await p.unroute(/\/api\/reminders\/\d+\/reschedule$/);
      await p.evaluate(() => closeModal('customerEditModal'));

      // 归档：公司名划掉，失败撤回
      await p.evaluate(() => switchPage('customers'));
      await p.waitForTimeout(1500);
      await p.route(/\/api\/customers\/\d+$/, async (route) => {
        if (route.request().method() === 'DELETE') { await new Promise((r) => setTimeout(r, 1000)); await route.abort('failed'); } else await route.continue();
      });
      const aid = await p.evaluate(() => { const b = document.querySelector('[data-ld-archive]'); return b && b.getAttribute('data-ld-archive'); });
      if (aid) {
        await p.evaluate((i) => { deleteCustomer(Number(i)); }, aid);
        await p.waitForTimeout(500);
        await p.locator('.modal-overlay.show button', { hasText: '归档' }).last().click({ timeout: 3000 }).catch(() => {});
        await p.waitForTimeout(300);
        const ledgerStruck = await p.locator('.ld-ent .ld-co .hand-strike, .ld-ent .hand-struck .hand-strike').count();
        check(`${sc.key}: 归档确认后公司名立刻划掉`, ledgerStruck >= 1, ledgerStruck);
        await p.waitForTimeout(2000);
        const ledgerUndone = await p.locator('.ld-ent .hand-strike').count();
        check(`${sc.key}: 归档失败后撤回划线`, ledgerUndone === 0, ledgerUndone);
      } else check(`${sc.key}: 台账里有可归档的客户`, false);
      await p.unroute(/\/api\/customers\/\d+$/);

      // 台账里的状态符号都带名称
      const glyphs = await p.evaluate(() => [...document.querySelectorAll('#page-customers .st')].map((e) => ({ tip: e.dataset.tip, aria: e.getAttribute('aria-label') })));
      check(`${sc.key}: 台账状态符号都带名称`, glyphs.length === 0 || glyphs.every((g) => g.tip && g.aria), `${glyphs.length} 个`);
      await p.screenshot({ path: path.join(out, `${sc.key}-ledger.png`) });
      const overflow = await p.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
      check(`${sc.key}: 无横向滚动`, overflow <= 1, overflow);
      await ctx.close();
    }
  } catch (e) {
    console.error('基础设施故障：' + e.message);
    await browser.close();
    process.exit(21);
  }
  await browser.close();
  check('全程无页面错误', errors.length === 0, errors.slice(0, 3).join(' | '));
  const failed = checks.filter((c) => !c.ok);
  fs.writeFileSync(path.join(out, 'report.json'), JSON.stringify({ label, url, checks, errors, failed: failed.length }, null, 2));
  for (const c of checks) console.log(`${c.ok ? 'PASS' : 'FAIL'}  ${c.name}${c.detail ? '  [' + c.detail.slice(0, 80) + ']' : ''}`);
  console.log(failed.length ? `\n${failed.length} 项失败` : '\n全部通过');
  process.exit(failed.length ? 20 : 0);
})();
