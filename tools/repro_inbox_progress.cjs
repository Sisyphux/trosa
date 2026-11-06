#!/usr/bin/env node
/*
 * Inbox 对话里“助手正在做什么”真实浏览器验收。
 *
 *   node tools/repro_inbox_progress.cjs --url http://127.0.0.1:18191 --out docs/repro/inbox-progress/after
 *
 * 服务需是隔离的本地开发服务，并在其系统库里放了 sela 测试令牌 trosa_sela_dialogue_test_token
 * （见 tests/test_inbox_progress.py 的写法）。脚本播种一条等助手回话的对话，
 * 依次推送几句进度，检查：状态行、被用到的字面内容画一笔、换句时旧标记擦掉、原话里找不到就不画、
 * 助手回话后整体消失、没有进度时界面不多出任何东西；宽屏、200% 缩放（窄视口）、减少动态各一遍。
 * 退出码：0 全部通过；20 有检查失败；21 基础设施故障。
 */
const path = require('path');
const fs = require('fs');
const { chromium } = require(path.join(__dirname, '..', 'browser-extension', 'node_modules', 'playwright'));
const args = process.argv.slice(2);
const opt = (n, d) => { const i = args.indexOf('--' + n); return i >= 0 ? args[i + 1] : d; };
const base = opt('url'); const out = path.resolve(opt('out', 'repro-out'));
if (!base) { console.error('缺少 --url'); process.exit(21); }
fs.mkdirSync(out, { recursive: true });
const TOKEN = 'trosa_sela_dialogue_test_token';
const checks = []; const errors = [];
const check = (name, ok, detail) => checks.push({ name, ok: !!ok, detail: detail === undefined ? '' : String(detail) });
const sela = async (p, body, method = 'POST') => (await fetch(base + p, { method, headers: { Authorization: 'Bearer ' + TOKEN, 'Content-Type': 'application/json' }, body: body ? JSON.stringify(body) : undefined })).json();

(async () => {
  let browser;
  try { browser = await chromium.launch(); } catch (e) { console.error('浏览器不可用：' + e.message); process.exit(21); }
  try {
    for (const sc of [
      { key: 'wide', viewport: { width: 1440, height: 900 } },
      { key: 'zoom200', viewport: { width: 720, height: 450 }, extra: { deviceScaleFactor: 2 } },
      { key: 'reduced', viewport: { width: 1440, height: 900 }, extra: { reducedMotion: 'reduce' } },
    ]) {
      const created = await sela('/api/integrations/sela/threads', { title: 'Northwind Plastics', text: '联系人邮箱我没找到，你有吗？', subject: 'prospect:progress-' + sc.key + Date.now() });
      const tid = created.thread.id;
      const ctx = await browser.newContext({ viewport: sc.viewport, ...(sc.extra || {}) });
      const p = await ctx.newPage();
      p.on('pageerror', (e) => errors.push(`[${sc.key}] ${e.message}`));
      await p.goto(base); await p.waitForTimeout(600);
      await p.evaluate(() => fetch('/api/auth/login', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ user: 'hamid' }) }));
      await p.goto(base); await p.waitForTimeout(800);
      await p.evaluate(() => switchPage('dialogue'));
      await p.waitForTimeout(1200);
      await p.evaluate((id) => fetch('/api/inbox/threads/' + id + '/reply', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ text: 'Northwind 的联系人是 Ana，邮箱 ana@northwind.com，帮我存一下。', seen_revision: 1 }) }), tid);
      await p.evaluate((id) => selectDialogueThread(id), tid);
      await p.waitForTimeout(1200);
      const shot = (name) => p.screenshot({ path: path.join(out, `${sc.key}-${name}.png`) }).catch(() => {});
      const q = (s) => p.evaluate((sel) => { const e = document.querySelector(sel); return e ? e.textContent : null; }, s);
      const count = (s) => p.locator(s).count();

      // 没有进度：和以前一样，没有状态行文字、没有标记
      check(`${sc.key}: 没有进度时不显示状态行`, (await p.evaluate(() => { const r = document.querySelector('.dialogue-msg--writing'); return r && getComputedStyle(r.querySelector('.dialogue-progress')).display; })) === 'none');
      check(`${sc.key}: 没有进度时没有标记`, (await count('.dialogue-mark')) === 0);
      await shot('0-idle');

      await sela(`/api/integrations/sela/threads/${tid}/progress`, { text: '正在查 Northwind 的资料', marks: ['Northwind'], run_id: 'r', seq: 1 });
      await p.waitForTimeout(2600);
      check(`${sc.key}: 显示状态行`, (await q('.dialogue-progress-text')) === '正在查 Northwind 的资料', await q('.dialogue-progress-text'));
      check(`${sc.key}: 原话里公司名被标出`, (await p.evaluate(() => (document.querySelector('.dialogue-mark:not(.is-erasing)') || {}).textContent)) === 'Northwind');
      check(`${sc.key}: 状态行里命中的字更重`, (await q('.dialogue-progress-text b')) === 'Northwind');
      check(`${sc.key}: 状态行是 aria-live=polite`, (await p.getAttribute('.dialogue-progress', 'aria-live')) === 'polite');
      check(`${sc.key}: 标记有下划线形状（不只靠颜色）`, (await p.evaluate(() => { const e = document.querySelector('.dialogue-mark'); const s = getComputedStyle(e); return s.backgroundImage !== 'none' || s.outlineStyle !== 'none'; })));
      await p.waitForTimeout(700);
      await shot('1-company');

      await sela(`/api/integrations/sela/threads/${tid}/progress`, { text: '正在保存邮箱 ana@northwind.com', marks: ['ana@northwind.com'], run_id: 'r', seq: 2 });
      await p.waitForTimeout(2600);
      check(`${sc.key}: 换句后只剩邮箱一处标记`, (await p.evaluate(() => [...document.querySelectorAll('.dialogue-mark:not(.is-erasing)')].map((e) => e.textContent).join('|'))) === 'ana@northwind.com');
      await p.waitForTimeout(500);
      check(`${sc.key}: 旧标记已擦掉`, (await count('.dialogue-mark')) === 1, await count('.dialogue-mark'));
      await shot('2-email');

      await sela(`/api/integrations/sela/threads/${tid}/progress`, { text: '正在核对 Zzz 的网站', marks: ['Zzz'], run_id: 'r', seq: 3 });
      await p.waitForTimeout(3000);
      check(`${sc.key}: 原话里找不到的不画`, (await count('.dialogue-mark:not(.is-erasing)')) === 0);

      await sela(`/api/integrations/sela/threads/${tid}/progress`, { text: '正在保存邮箱 ana@northwind.com', marks: ['ana@northwind.com'], run_id: 'r', seq: 4 });
      await p.waitForTimeout(2200);
      await sela(`/api/integrations/sela/threads/${tid}/messages`, { text: '已存好 Ana 的邮箱。', awaiting: 'human' });
      await p.waitForTimeout(3500);
      check(`${sc.key}: 助手回话后状态行消失`, (await count('.dialogue-progress-text')) === 0);
      check(`${sc.key}: 助手回话后标记消失`, (await count('.dialogue-mark')) === 0);
      check(`${sc.key}: 新回话已显示`, (await p.evaluate(() => document.body.innerText.includes('已存好 Ana 的邮箱'))));
      await shot('3-replied');

      await ctx.close();
    }
  } catch (e) {
    console.error('脚本故障：' + e.message); process.exit(21);
  } finally { await browser.close(); }
  const failed = checks.filter((c) => !c.ok);
  fs.writeFileSync(path.join(out, 'report.json'), JSON.stringify({ checks, errors }, null, 2));
  for (const c of checks) console.log((c.ok ? 'PASS ' : 'FAIL ') + c.name + (c.ok ? '' : '  → ' + c.detail));
  if (errors.length) console.log('页面错误：', errors);
  process.exit(failed.length || errors.length ? 20 : 0);
})();
