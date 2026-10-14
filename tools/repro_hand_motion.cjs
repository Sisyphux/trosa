#!/usr/bin/env node
/*
 * 「一笔」三期（动作回应与每页点睛）真实浏览器验收。
 *
 *   node tools/repro_hand_motion.cjs --url http://127.0.0.1:18190 --label after --out docs/repro/hand-motion-p3/after
 *
 * 服务需用 CRM_SEED_DEMO_DATA=1 启动。覆盖：今天焦点客户名下划线、客户台账书眉分段名下划线、
 * 客户工作区“一句下一步”下划线、Inbox 里 sela 处理中“笔在写”、标记完成时划掉（保存失败撤回）、
 * 新沟通记录下的墨迹（出现后淡去并移除）、按钮处理中的笔与成功的勾。宽屏、iPhone、减少动态各一遍。
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

async function login(p) {
  await p.evaluate(() => { try { showLogin(); } catch (e) { /* 已在登录页 */ } });
  await p.waitForTimeout(400);
  await p.locator('button:visible, [role=button]:visible', { hasText: 'Hamid' }).first().click({ timeout: 5000 });
  await p.waitForTimeout(1500);
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
      const reduced = sc.key === 'reduced';
      const ctx = await browser.newContext({ viewport: sc.viewport, ...(sc.extra || {}) });
      const p = await ctx.newPage();
      p.on('pageerror', (e) => errors.push(`[${sc.key}] ${e.message}`));
      await p.goto(url); await p.waitForTimeout(800);
      await login(p);
      const shot = (n, sel) => (sel ? p.locator(sel).first().screenshot({ path: path.join(out, `${sc.key}-${n}.png`) }) : p.screenshot({ path: path.join(out, `${sc.key}-${n}.png`) })).catch(() => {});

      // 今天：焦点客户名下一笔
      const todayUl = await p.locator('.room-company .hand-ul path').count();
      check(`${sc.key}: 今天焦点客户名下有一笔`, todayUl === 1, todayUl);
      await p.waitForTimeout(reduced ? 100 : 800);
      await shot('today');
      const todayAnim = await p.evaluate(() => { const e = document.querySelector('.room-company .hand-ul path'); return e ? getComputedStyle(e).animationName : ''; });
      if (reduced) check('reduced: 今天的下划线不动画', todayAnim === 'none', todayAnim);

      // 客户台账：书眉分段名
      await p.evaluate(() => switchPage('customers'));
      await p.waitForTimeout(1500);
      const ledgerUl = await p.locator('#ledgerRunningLabel .hand-ul path').count();
      check(`${sc.key}: 台账书眉分段名下有一笔`, ledgerUl === 1, ledgerUl);
      await shot('ledger');

      // 客户工作区：一句下一步
      const id = await p.evaluate(async () => { const r = await fetch('/api/customers?limit=1'); const j = await r.json(); const a = j.customers || j.items || j; return a[0] && a[0].id; });
      await p.evaluate((i) => openEditModal(i), id);
      await p.waitForTimeout(2000);
      const nextUl = await p.locator('#customerNextTask .cw-h2 .hand-ul path').count();
      check(`${sc.key}: “一句下一步”下有一笔`, nextUl === 1, nextUl);
      await shot('workspace');

      // 新沟通记录：墨迹出现、之后淡去并移除
      await p.evaluate((i) => {
        const activity = { type: 'follow', id: 880001, follow_date: localDateString(), content: '客户确认样品，下周下单', result: '', next_plan: '',
          activity_type: 'email', direction: 'outbound', is_reported: false, source: 'manual' };
        const key = upsertCustomerTimelineEntry(activity);
        syncCustomerWorkspaceAfterCommunication(i, activity, key);
      }, id);
      await p.waitForTimeout(250);
      const inkCount = await p.locator('#outreachList .hand-ink').count();
      if (reduced) check('reduced: 新记录不出现墨迹', inkCount === 0, inkCount);
      else {
        check(`${sc.key}: 新记录下出现墨迹`, inkCount === 1, inkCount);
        await shot('ink', '#outreachList');
        await p.waitForTimeout(2300);
        const inkAfter = await p.locator('#outreachList .hand-ink').count();
        check(`${sc.key}: 墨迹淡去后被移除`, inkAfter === 0, inkAfter);
      }

      // 标记完成：点下去立刻划掉；保存失败则撤回
      await p.route(/\/api\/reminders\/\d+$/, async (route) => {
        if (route.request().method() === 'PUT') { await new Promise((r) => setTimeout(r, 1200)); await route.abort('failed'); }
        else await route.continue();
      });
      const completeBtn = p.locator('#cwFocusActions button', { hasText: '标记完成' }).first();
      if (await completeBtn.count()) {
        await completeBtn.click();
        await p.waitForTimeout(250);
        const struck = await p.locator('#customerNextTask .cw-h2 .hand-under .hand-strike').count();
        check(`${sc.key}: 点“标记完成”立刻划掉`, struck === 1, struck);
        const penInBtn = await p.locator('#cwFocusActions .wait-pen-btn').count();
        check(`${sc.key}: 按钮处理中是笔`, penInBtn >= 1, penInBtn);
        await shot('struck', '#customerNextTask');
        await p.waitForTimeout(2500);
        const afterFail = await p.locator('#customerNextTask .cw-h2 .hand-under .hand-strike').count();
        check(`${sc.key}: 保存失败后撤回划线`, afterFail === 0, afterFail);
      } else check(`${sc.key}: 有“标记完成”按钮`, false);
      await p.unroute(/\/api\/reminders\/\d+$/);

      // 按钮成功的勾
      const tick = await p.evaluate(() => {
        const b = document.createElement('button'); b.textContent = '保存'; document.body.appendChild(b);
        setActionFeedback(b, 'success', '已保存');
        const has = !!b.querySelector('svg.hi-btn .s');
        b.remove();
        return has;
      });
      check(`${sc.key}: 按钮成功时是画出来的勾`, tick);

      await p.evaluate(() => closeModal && closeModal('customerEditModal')).catch(() => {});

      // Inbox：sela 处理中
      await p.evaluate(() => openInboxRoom());
      await p.waitForTimeout(1200);
      const writing = await p.evaluate(() => {
        dialogueThread = { id: 'demo-thread', title: '补充邮箱', status: 'open', awaiting: 'sela', revision: 1, messages: [] };
        const html = renderDialogueThreadHtml();
        const box = document.createElement('div'); box.innerHTML = html;
        const pane = document.getElementById('dialogueThreadPane');
        if (pane) pane.innerHTML = html;
        return !!box.querySelector('.dialogue-msg--writing .wait-pen');
      });
      check(`${sc.key}: sela 处理中显示“笔在写”`, writing);
      await p.waitForTimeout(500);
      await shot('inbox-writing', '#dialogueThreadPane');

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
  for (const c of checks) console.log(`${c.ok ? 'PASS' : 'FAIL'}  ${c.name}${c.detail ? '  [' + c.detail.slice(0, 70) + ']' : ''}`);
  console.log(failed.length ? `\n${failed.length} 项失败` : '\n全部通过');
  process.exit(failed.length ? 20 : 0);
})();
