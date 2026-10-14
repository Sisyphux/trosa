#!/usr/bin/env node
/*
 * 「一笔」手绘图标与等待组件的真实浏览器验收。
 *
 *   node tools/repro_hand_icons.cjs --url http://127.0.0.1:18190 --label after --out docs/repro/hand-icons-p1/after
 *
 * 在 browser-extension 锁定的 Chromium 里，按六种情形（宽屏、iPad、iPhone、键盘、
 * 200% 缩放、减少动态）截图并做检查：等待组件 300ms 内不出现、之后出现；减少动态下
 * 没有循环动画；遮罩图标都能解析；无横向滚动、无页面错误；悬停会描线；提示位移 ≤ 8px。
 * 退出码：0 全部通过；20 有检查失败；21 基础设施故障。
 */
const path = require('path');
const fs = require('fs');
const { chromium } = require(path.join(__dirname, '..', 'browser-extension', 'node_modules', 'playwright'));

const args = process.argv.slice(2);
const opt = (name, fallback) => { const i = args.indexOf('--' + name); return i >= 0 ? args[i + 1] : fallback; };
const url = opt('url'); const label = opt('label', 'run'); const out = path.resolve(opt('out', 'repro-out'));
if (!url) { console.error('缺少 --url'); process.exit(21); }
fs.mkdirSync(out, { recursive: true });

const checks = [];
const errors = [];
const check = (name, ok, detail) => { checks.push({ name, ok: !!ok, detail: detail === undefined ? '' : String(detail) }); };

const SCENARIOS = [
  { key: 'wide', label: '宽屏 1440', viewport: { width: 1440, height: 900 } },
  { key: 'ipad', label: 'iPad 820', viewport: { width: 820, height: 1180 }, extra: { hasTouch: true } },
  { key: 'iphone', label: 'iPhone 390', viewport: { width: 390, height: 844 }, extra: { hasTouch: true, isMobile: true, deviceScaleFactor: 2 } },
  { key: 'zoom200', label: '200% 缩放（720 逻辑宽）', viewport: { width: 720, height: 450 }, extra: { deviceScaleFactor: 2 } },
  { key: 'reduced', label: '减少动态', viewport: { width: 1440, height: 900 }, extra: { reducedMotion: 'reduce' } },
  { key: 'keyboard', label: '键盘', viewport: { width: 1440, height: 900 } },
];

async function login(p) {
  await p.goto(url); await p.waitForTimeout(600);
  await p.evaluate(() => { try { showLogin(); } catch (e) { /* 已在登录页 */ } });
  await p.waitForTimeout(400);
  await p.locator('button:visible, [role=button]:visible', { hasText: 'Hamid' }).first().click({ timeout: 5000 });
  await p.waitForTimeout(1500);
}

(async () => {
  let browser;
  try { browser = await chromium.launch(); } catch (e) { console.error('浏览器不可用：' + e.message); process.exit(21); }
  try {
    for (const sc of SCENARIOS) {
      const ctx = await browser.newContext({ viewport: sc.viewport, ...(sc.extra || {}) });
      const p = await ctx.newPage();
      p.on('pageerror', (e) => errors.push(`[${sc.key}] ${e.message}`));
      await login(p);
      const shot = (n) => p.screenshot({ path: path.join(out, `${sc.key}-${n}.png`) });
      await shot('today');

      // 导航面板
      await p.evaluate(() => { try { openRoomIndex(); } catch (e) { /* 以按钮为准 */ } });
      if (!(await p.locator('.room-index-nav').first().isVisible())) {
        await p.getByRole('button', { name: /搜索与导航/ }).first().click().catch(() => {});
      }
      await p.waitForTimeout(700);
      await shot('nav');
      const navIcons = await p.evaluate(() => [...document.querySelectorAll('.nav-hand')].map((e) => { const r = e.getBoundingClientRect(); return [r.width, r.height, e.querySelectorAll('.s').length]; }));
      check(`${sc.key}: 导航里有 3 个带笔画的手绘图标`, navIcons.length === 3 && navIcons.every((n) => n[0] >= 20 && n[2] > 0), JSON.stringify(navIcons));
      await p.keyboard.press('Escape'); await p.waitForTimeout(400);

      // 页面宽度与遮罩图标
      const overflow = await p.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
      check(`${sc.key}: 今天页无横向滚动`, overflow <= 1, overflow);
      const masks = await p.evaluate(() => [...document.querySelectorAll('.ui-icon')].map((e) => { const s = getComputedStyle(e); return (s.maskImage || s.webkitMaskImage || 'none'); }));
      check(`${sc.key}: 页面上的遮罩图标都有图`, masks.every((m) => m !== 'none' && m.includes('icons/hand/')), `${masks.length} 个`);

      if (sc.key === 'keyboard') {
        await p.keyboard.press('Tab'); await p.keyboard.press('Tab'); await p.keyboard.press('Tab');
        await shot('focus');
        const focused = await p.evaluate(() => { const a = document.activeElement; return a ? (a.getAttribute('aria-label') || a.tagName) : ''; });
        check('keyboard: Tab 能到达可聚焦控件（焦点环由既有规则负责）', !!focused, focused);
      }

      // 悬停描线（减少动态下不应有动画）
      const icon = p.locator('.command-icon svg.hi').first();
      if (await icon.count()) {
        await icon.hover().catch(() => {});
        await p.waitForTimeout(120);
        const drawing = await p.evaluate(() => document.getAnimations().filter((a) => a.animationName === 'hiDraw').length);
        if (sc.key === 'reduced') check('reduced: 悬停不再描线', drawing === 0, drawing);
        else if (sc.key !== 'iphone' && sc.key !== 'ipad') check(`${sc.key}: 悬停图标会描线一次`, drawing > 0, drawing);
      }

      // 提示位移与状态条
      await p.evaluate(() => showToast('已加入本周工作', 'success'));
      const tx = await p.evaluate(() => { const t = document.querySelector('.toast'); const m = new DOMMatrix(getComputedStyle(t).transform); return Math.abs(m.m41); });
      check(`${sc.key}: 提示起始位移 ≤ 8px`, tx <= 8.01, tx);
      await p.evaluate(() => { window.__a = beginActionStatus(null, '正在保存…'); });
      await p.waitForTimeout(sc.key === 'reduced' ? 100 : 600);
      await shot('saving');
      const penVisible = await p.evaluate(() => { const e = document.querySelector('#actionStatus .wait-pen'); return !!e && getComputedStyle(e).display !== 'none'; });
      check(`${sc.key}: 保存中显示笔`, penVisible);
      await p.evaluate(() => finishActionStatus(window.__a, 'success', '已保存'));
      await p.waitForTimeout(sc.key === 'reduced' ? 100 : 700);
      await shot('saved');
      const okVisible = await p.evaluate(() => { const e = document.querySelector('#actionStatus .wait-ok'); return !!e && getComputedStyle(e).display !== 'none'; });
      check(`${sc.key}: 已保存显示画出的勾`, okVisible);
      await p.evaluate(() => resetActionStatus());

      // 等待组件：超过 300ms 才出现；减少动态下没有循环动画
      await p.route('**/api/**', async (route) => { await new Promise((r) => setTimeout(r, 2500)); route.continue(); });
      await p.evaluate(() => switchPage('logs'));
      await p.waitForTimeout(120);
      const early = await p.evaluate(() => { const e = document.querySelector('.wait'); return e ? getComputedStyle(e).opacity : 'none'; });
      // 减少动态时 html.motion-reduced 关掉全部动画，等待组件直接以终态出现——这是预期。
      if (sc.key !== 'reduced') check(`${sc.key}: 120ms 内等待组件还没出现`, early === '0' || early === 'none', early);
      await p.waitForTimeout(700);
      const late = await p.evaluate(() => { const e = document.querySelector('.wait'); return e ? getComputedStyle(e).opacity : 'none'; });
      check(`${sc.key}: 800ms 后等待组件已出现`, late === '1', late);
      await shot('waiting');
      if (sc.key === 'reduced') {
        const loops = await p.evaluate(() => document.getAnimations().filter((a) => a.effect && a.effect.getComputedTiming().iterations === Infinity && a.playState === 'running').length);
        check('reduced: 没有循环动画在跑', loops === 0, loops);
      }
      await p.unroute('**/api/**');

      // 客户页
      await p.evaluate(() => switchPage('customers'));
      await p.waitForTimeout(1200);
      await shot('customers');
      const ov2 = await p.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
      check(`${sc.key}: 客户页无横向滚动`, ov2 <= 1, ov2);
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
  for (const c of checks) console.log(`${c.ok ? 'PASS' : 'FAIL'}  ${c.name}${c.detail ? '  [' + c.detail + ']' : ''}`);
  console.log(failed.length ? `\n${failed.length} 项失败` : '\n全部通过');
  process.exit(failed.length ? 20 : 0);
})();
