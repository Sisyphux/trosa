/*
 * Real-browser reproduction: "I cannot ask the assistant whether a customer is due".
 *
 * Seeds an isolated dev database (synthetic customers only), then in the Inbox
 * conversation asks "<公司>是不是该跟进了？".  Expects a fixed-format answer
 * (结论 + dated basis + what the system cannot see + a customer link), a pick list
 * for an ambiguous name, a plain "not found", and postponing that only writes a
 * dated task after 确认.  Also checks keyboard-only use and that the answer stays
 * inside the screen on iPad, iPhone, an old Win10 laptop and 200% zoom.
 *
 * Usage (see docs/REAL_BROWSER_REPRO.md):
 *   node tools/repro_ask_customer.cjs --url http://127.0.0.1:18303 --app-root . \
 *        --db-dir <scratch db> --label after --out docs/repro/ask-customer/after
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
  const options = { url: process.env.TROSA_REPRO_URL || 'http://127.0.0.1:18302', label: 'run', out: '', appRoot: ROOT, dbDir: '', python: path.join(ROOT, '.venv', 'bin', 'python') };
  for (let i = 0; i < argv.length; i += 1) {
    const key = argv[i];
    const value = argv[i + 1];
    if (key === '--url') { options.url = value; i += 1; }
    else if (key === '--label') { options.label = value; i += 1; }
    else if (key === '--out') { options.out = value; i += 1; }
    else if (key === '--app-root') { options.appRoot = path.resolve(value); i += 1; }
    else if (key === '--db-dir') { options.dbDir = path.resolve(value); i += 1; }
    else if (key === '--python') { options.python = value; i += 1; }
    else throw new Error(`unknown argument: ${key}`);
  }
  if (!options.out) throw new Error('--out <directory> is required (evidence is saved there)');
  if (!options.dbDir) throw new Error('--db-dir <scratch database directory of the running server> is required');
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

function localDate(offsetDays) {
  const d = new Date();
  d.setDate(d.getDate() + offsetDays);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}

async function settle(page) {
  await page.waitForFunction(
    () => !document.documentElement.dataset.motionType && !document.activeViewTransition,
    null, { timeout: 5000 },
  ).catch(() => {});
  await page.waitForTimeout(600);
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


const stamp = Date.now() % 100000;
const NAMES = { waiting: `Harbor Plastics ${stamp}`, twinA: `Bluewave Trading ${stamp}`, twinB: `Bluewave Films ${stamp}`, fresh: `Quiet Acrylic ${stamp}` };

async function openDialogue(page, url) {
  await page.goto(`${url}/`, { waitUntil: 'load' });
  await page.waitForFunction(() => typeof switchPage === 'function', null, { timeout: 20000 });
  for (let attempt = 0; attempt < 4; attempt += 1) {
    await page.evaluate(() => { if (currentPage !== 'dialogue') switchPage('dialogue'); });
    await page.waitForTimeout(1200);
    if (await page.evaluate(() => currentPage === 'dialogue')) break;
  }
  await page.waitForSelector('#page-dialogue.active', { timeout: 10000 });
}

// Opens a fresh "记一笔" conversation and sends `text` with the keyboard only (type, Enter).
async function sendNote(page, text) {
  await page.evaluate(() => startDialogueNote());
  await page.waitForSelector('#dialogueReplyInput', { timeout: 8000 });
  await page.focus('#dialogueReplyInput');
  await page.keyboard.type(text);
  await page.keyboard.press('Enter');
  await page.waitForTimeout(1500);
}

async function reply(page, text) {
  if (!(await page.$('#dialogueReplyInput'))) return false;      // no way to answer: the checks below report it
  await page.focus('#dialogueReplyInput');
  await page.keyboard.type(text);
  await page.keyboard.press('Enter');
  await page.waitForTimeout(1500);
  return true;
}

const answers = (page) => page.evaluate(() => Array.from(document.querySelectorAll('#dialogueMessages .dialogue-msg[data-role="sela"] .dialogue-msg-text')).map((el) => el.innerText.trim()));
const lastAnswer = async (page) => { const list = await answers(page); return list[list.length - 1] || ''; };

async function runScenario(browser, options, outDir, report) {
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, locale: 'zh-CN' });
  const page = await context.newPage();
  page.on('pageerror', (error) => report.pageErrors.push(String(error.message).slice(0, 200)));
  const shot = async (name, target) => {
    await settle(page);
    await (target || page).screenshot({ path: path.join(outDir, name) });
    report.screenshots.push(name);
  };
  const check = (id, title, pass, detail) => report.checks.push({ id, title, pass: !!pass, detail });

  try {
    await page.goto(`${options.url}/`, { waitUntil: 'load' });
    const login = await json(page, '/api/auth/login', 'POST', { user: 'hamid' });
    if (login.status !== 200) throw new Error(`login failed: ${login.status}`);
    const ids = {};
    for (const [key, company, country, said, ago] of [
      ['waiting', NAMES.waiting, 'Spain', '客户问了 3mm 透明板的最新价格和交期，说下周要定。', 10],
      ['twinA', NAMES.twinA, 'Peru', '', 0], ['twinB', NAMES.twinB, 'Chile', '', 0], ['fresh', NAMES.fresh, 'India', '已回复客户报价，等对方确认。', 1],
    ]) {
      const created = await json(page, '/api/customers', 'POST', { company, name: 'Contact', country });
      if (created.status !== 201) throw new Error(`customer create failed: ${created.status} ${JSON.stringify(created.data)}`);
      ids[key] = created.data.id;
      if (said) {
        const rec = await json(page, `/api/customers/${created.data.id}/follow_history`, 'POST', key === 'waiting'
          ? { activity_content: said, activity_type: 'customer_reply', direction: 'inbound', follow_date: localDate(-ago) }
          : { activity_content: said, activity_type: 'email', direction: 'outbound', follow_date: localDate(-ago) });
        if (rec.status >= 300) throw new Error(`record seed failed: ${rec.status}`);
      }
    }

    await openDialogue(page, options.url);

    // 1. Ask about a customer who wrote 10 days ago and got no reply.
    await sendNote(page, `${NAMES.waiting}是不是该跟进了？`);
    const a1 = await lastAnswer(page);
    check('answer-has-verdict', '助手回答以「结论：可以跟」开头', /^结论：可以跟/.test(a1), a1.slice(0, 80) || '助手没有回答');
    check('answer-has-dates', '回答里有带日期的依据', /\d+月\d+日/.test(a1) && /依据/.test(a1), a1.slice(0, 160));
    check('answer-says-what-it-cannot-see', '回答明说「之后的情况我看不到」', /之后的情况我看不到/.test(a1), '');
    const link = await page.$('#dialogueMessages .dialogue-msg[data-role="sela"] .dialogue-ref--link');
    check('answer-links-customer', '回答带「打开客户」，能点开原记录', !!link, '');
    const extra = await page.evaluate(() => document.querySelectorAll('#dialogueMessages .dialogue-msg[data-role="sela"] button:not(.dialogue-ref--link)').length);
    check('no-new-buttons', '回答里没有新增的按钮', extra === 0, `${extra} 个`);
    await shot('01-answer-1440.png');

    // 2. Postpone: nothing is written until the member confirms.
    await reply(page, '推迟到11月5日');
    const a2 = await lastAnswer(page);
    check('postpone-asks-confirm', '说推迟后，助手先问「确认」', /请回复「确认」/.test(a2) && /2\d{3}年11月5日/.test(a2), a2.slice(0, 120));
    const before = await json(page, `/api/agent/customers/${ids.waiting}/workspace`, 'GET');
    check('nothing-written-before-confirm', '确认之前没有新建待办', ((before.data || {}).open_tasks || []).length === 0, '');
    await shot('02-postpone-1440.png');
    await reply(page, '确认');
    const a3 = await lastAnswer(page);
    check('confirm-says-done', '确认后助手说「已记下」', /已记下/.test(a3), a3.slice(0, 80));
    const after = await json(page, `/api/agent/customers/${ids.waiting}/workspace`, 'GET');
    const tasks = ((after.data || {}).open_tasks || []);
    check('dated-task-created', '客户出现一个 11月5日 的待办', tasks.some((t) => String(t.remind_date || '').slice(5, 10) === '11-05'), JSON.stringify(tasks.map((t) => t.remind_date)));
    await shot('03-confirmed-1440.png');

    // 3. A name that fits two customers asks which one; a number picks it.
    await sendNote(page, 'Bluewave 最近怎么样');
    const a4 = await lastAnswer(page);
    check('ambiguous-asks-which', '名字对得上两位时，让人选', /有几位对得上/.test(a4) && /(^|\n)1[.\s]/.test(a4) && /(^|\n)2[.\s]/.test(a4), a4.slice(0, 160));
    await shot('04-pick-1440.png');
    await reply(page, '1');
    const a5 = await lastAnswer(page);
    check('pick-then-answers', '回复序号后给出结论', /^结论：/.test(a5), a5.slice(0, 80));

    // 4. A name the system does not have is said plainly.
    await sendNote(page, '根本不存在的虚构公司是不是该跟进了？');
    const a6 = await lastAnswer(page);
    check('not-found-plain', '找不到时直接说没找到', /没有找到/.test(a6), a6.slice(0, 80));

    // 5. A normal note still goes to the normal assistant (no auto answer).
    await sendNote(page, `今天和 ${NAMES.fresh} 的联系人通了电话，他要 3mm 透明板报价`);
    check('notes-untouched', '普通的记一笔不会被当成提问', (await answers(page)).length === 0, '');

    // 6. Other screens: the answer stays inside the screen.
    for (const [label, vp, scale] of [['ipad-820', { width: 820, height: 1180 }, 1], ['iphone-390', { width: 390, height: 844 }, 1], ['win10-1366', { width: 1366, height: 768 }, 1], ['zoom200-720', { width: 720, height: 450 }, 2]]) {
      const ctx = await browser.newContext({ viewport: vp, deviceScaleFactor: scale, locale: 'zh-CN', hasTouch: label === 'iphone-390' });
      const p2 = await ctx.newPage();
      try {
        await p2.goto(`${options.url}/`, { waitUntil: 'load' });
        await json(p2, '/api/auth/login', 'POST', { user: 'hamid' });
        await openDialogue(p2, options.url);
        await sendNote(p2, `${NAMES.waiting}是不是该跟进了？`);
        const t2 = await lastAnswer(p2);
        check(`answer-visible-${label}`, `${label}：能看到回答`, /^结论：/.test(t2), t2.slice(0, 60) || '没有回答');
        const overflow = await p2.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth + 1);
        const clipped = await p2.evaluate(() => { const el = document.querySelector('#dialogueMessages .dialogue-msg[data-role="sela"] .dialogue-msg-text'); if (!el) return true; const r = el.getBoundingClientRect(); return r.right > window.innerWidth + 1 || r.left < -1; });
        check(`no-overflow-${label}`, `${label}：没有横向滚动，回答不被裁切`, !overflow && !clipped, `${overflow ? '有横向滚动 ' : ''}${clipped ? '被裁切' : ''}`);
        await p2.screenshot({ path: path.join(outDir, `0-${label}.png`) });
        report.screenshots.push(`0-${label}.png`);
      } catch (error) {
        check(`viewport-${label}`, `${label}：能提问并看到回答`, false, String(error.message).split('\n')[0].slice(0, 160));
      }
      await ctx.close();
    }
  } catch (error) {
    const full = String(error && error.message ? error.message : error);
    const message = full.split('\n')[0];
    if (process.env.REPRO_DEBUG) console.error(full);
    if (/ERR_CONNECTION|ECONNREFUSED|net::ERR_/.test(message)) throw Object.assign(new Error(message), { infra: true });
    report.checks.push({ id: 'scenario-completed', title: '场景能完整走完', pass: false, detail: message.slice(0, 300) });
    await shot('99-failure.png').catch(() => {});
  } finally {
    await context.close();
  }
}

async function main() {
  const options = parseArgs(process.argv.slice(2));
  const outDir = path.resolve(ROOT, options.out);
  fs.mkdirSync(outDir, { recursive: true });
  const { chromium } = loadPlaywright();
  const report = { label: options.label, url: options.url, generatedAt: new Date().toISOString(), checks: [], screenshots: [], pageErrors: [] };
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
  for (const c of report.checks) console.log(`${c.pass ? 'PASS' : 'FAIL'}  ${c.id}  —  ${c.title}\n        ${c.detail}`);
  console.log(`\n${report.checks.length - failed.length}/${report.checks.length} 项通过；证据：${path.relative(ROOT, outDir) || outDir}`);
  process.exit(failed.length ? EXIT_DEFECT : EXIT_PASS);
}

main();
