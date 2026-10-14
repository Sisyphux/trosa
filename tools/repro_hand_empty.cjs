#!/usr/bin/env node
/*
 * 「一笔」二期（空状态插画）真实浏览器验收。
 *
 *   node tools/repro_hand_empty.cjs --url http://127.0.0.1:18190 --label after --out docs/repro/hand-empty-p2/after
 *
 * 服务需用 CRM_SEED_DEMO_DATA=1 启动（一位没有沟通记录的演示客户）。覆盖 8 个高频空状态：
 * Inbox 对话、客户最近沟通、待办、搜索无结果、没有客户（组件用例）、本周没有已确认的沟通、联系人、客户文件，
 * 以及沟通记录页。宽屏与 iPhone 各截图；检查：有插画（带笔画）、有标题、说明不超过两句、
 * 第二次渲染不再描线、减少动态下不描线、无横向滚动、无页面错误。
 * 退出码：0 全部通过；20 有检查失败；21 基础设施故障。
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
const sentences = (t) => (String(t).match(/[。！？!?]/g) || []).length || (String(t).trim() ? 1 : 0);

async function login(p) {
  await p.evaluate(() => { try { showLogin(); } catch (e) { /* 已在登录页 */ } });
  await p.waitForTimeout(400);
  await p.locator('button:visible, [role=button]:visible', { hasText: 'Hamid' }).first().click({ timeout: 5000 });
  await p.waitForTimeout(1500);
}

async function inspect(p, scope, kind, tag, shotName, out) {
  const sel = `${scope} [data-empty-kind="${kind}"]`.trim();
  await p.waitForSelector(sel, { timeout: 6000, state: 'attached' });
  const info = await p.evaluate((s) => {
    const el = document.querySelector(s);
    const art = el.querySelector('svg.hi-illus');
    const hint = el.querySelector('p');
    return {
      hasArt: !!art, strokes: art ? art.querySelectorAll('.s').length : 0,
      title: (el.querySelector('.hand-empty-title') || {}).textContent || '',
      hint: hint ? hint.textContent : '',
      box: (() => { const r = el.getBoundingClientRect(); return [Math.round(r.width), Math.round(r.height)]; })(),
    };
  }, sel);
  const withArt = !/noart/.test(tag);
  if (withArt) check(`${tag}: ${kind} 有插画且带笔画`, info.hasArt && info.strokes >= 2, `${info.strokes} 笔`);
  check(`${tag}: ${kind} 有标题`, info.title.trim().length > 0, info.title);
  check(`${tag}: ${kind} 说明不超过两句`, sentences(info.hint) <= 2, info.hint);
  if (await p.locator(sel).first().isVisible()) {
    await p.locator(sel).first().scrollIntoViewIfNeeded().catch(() => {});
    await p.locator(sel).first().screenshot({ path: path.join(out, shotName + '.png') }).catch(() => {});
  }
  return info;
}

(async () => {
  let browser;
  try { browser = await chromium.launch(); } catch (e) { console.error('浏览器不可用：' + e.message); process.exit(21); }
  try {
    for (const sc of [
      { key: 'wide', viewport: { width: 1440, height: 900 } },
      { key: 'iphone', viewport: { width: 390, height: 844 }, extra: { hasTouch: true, isMobile: true, deviceScaleFactor: 2 } },
      { key: 'reduced', viewport: { width: 1440, height: 900 }, extra: { reducedMotion: 'reduce' } },
    ]) {
      const ctx = await browser.newContext({ viewport: sc.viewport, ...(sc.extra || {}) });
      const p = await ctx.newPage();
      p.on('pageerror', (e) => errors.push(`[${sc.key}] ${e.message}`));
      await p.goto(url); await p.waitForTimeout(800);

      // 周报（登录前内部入口）
      const hasWeek = await p.locator('.wk-empty').count();
      if (hasWeek) await inspect(p, '.wk-empty', 'week', sc.key, `${sc.key}-week`, out).catch((e) => check(`${sc.key}: 周报空状态`, false, e.message));

      await login(p);
      // Today 右栏：最近沟通
      await inspect(p, '#todayFacts', 'timeline', sc.key, `${sc.key}-today-facts`, out).catch((e) => check(`${sc.key}: Today 最近沟通空状态`, false, e.message));

      // Inbox
      await p.evaluate(() => openInboxRoom());
      await p.waitForTimeout(1500);
      await inspect(p, '#dialogueThreadPane', 'inbox', sc.key, `${sc.key}-inbox`, out).catch((e) => check(`${sc.key}: Inbox 空状态`, false, e.message));
      const listText = await p.evaluate(() => (document.getElementById('dialogueList') || {}).textContent || '');
      check(`${sc.key}: Inbox 列表仍写“现在没有对话”`, listText.includes('现在没有对话'), listText.slice(0, 40));

      // 客户台账：没有匹配
      await p.evaluate(() => switchPage('customers'));
      await p.waitForTimeout(1200);
      const input = p.locator('#ledgerSearch');
      if (await input.count()) {
        await input.fill('zzzz-no-such-customer'); await p.waitForTimeout(1500);
        const art = await p.locator('#page-customers .ld-st svg.hi-illus').count();
        check(`${sc.key}: 台账“没有匹配”有插画`, art === 1, art);
        await p.locator('#page-customers .ld-state').first().screenshot({ path: path.join(out, `${sc.key}-ledger-search.png`) }).catch(() => {});
        await input.fill(''); await p.waitForTimeout(600);
      }

      // 客户工作区：时间线 / 联系人 / 文件 / 待办
      const id = await p.evaluate(async () => { const r = await fetch('/api/customers?limit=1'); const j = await r.json(); const a = j.customers || j.items || j; return a[0] && a[0].id; });
      await p.evaluate((i) => openEditModal(i), id);
      await p.waitForTimeout(1800);
      await inspect(p, '#customerEditModal', 'timeline', sc.key, `${sc.key}-cw-timeline`, out).catch((e) => check(`${sc.key}: 客户最近沟通空状态`, false, e.message));
      for (const [tab, kind, name] of [['editTabContacts', 'contacts', 'contacts'], ['editTabFiles', 'files', 'files'], ['editTabTasks', 'todo', 'todo']]) {
        await p.evaluate((t) => switchCustomerSection(t), tab);
        await p.waitForTimeout(900);
        // 演示客户带一条待办：用组件本身渲染空状态，检查文案与插画。
        if (kind === 'todo') await p.evaluate(() => renderCustomerTaskEmpty(document.getElementById('customerTasksList')));
        if (kind === 'todo') await p.waitForTimeout(700);
        await inspect(p, '#customerEditModal', kind, sc.key, `${sc.key}-cw-${name}`, out).catch((e) => check(`${sc.key}: ${kind} 空状态`, false, e.message));
      }
      // 插画只描一次：同类型第二次渲染不带 hi-draw
      const second = await p.evaluate(() => { const a = emptyStateHtml('files', 't', 'h'); return a.includes('hi-draw'); });
      check(`${sc.key}: 同一插画第二次出现不再描线`, second === false);
      if (sc.key === 'reduced') {
        const running = await p.evaluate(() => document.getAnimations().filter((a) => a.animationName === 'hiDraw' && a.playState === 'running').length);
        check('reduced: 插画不描线', running === 0, running);
      }
      await p.evaluate(() => closeModal && closeModal('customerEditModal')).catch(() => {});
      const overflow = await p.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
      check(`${sc.key}: 无横向滚动`, overflow <= 1, overflow);

      // 沟通记录页
      await p.evaluate(() => switchPage('history'));
      await p.waitForTimeout(1500);
      await inspect(p, '#historyTimeline', 'log', sc.key, `${sc.key}-history`, out).catch((e) => check(`${sc.key}: 沟通记录页空状态`, false, e.message));
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
  for (const c of checks) console.log(`${c.ok ? 'PASS' : 'FAIL'}  ${c.name}${c.detail ? '  [' + c.detail.slice(0, 60) + ']' : ''}`);
  console.log(failed.length ? `\n${failed.length} 项失败` : '\n全部通过');
  process.exit(failed.length ? 20 : 0);
})();
