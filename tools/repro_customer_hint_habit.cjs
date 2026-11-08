/*
 * Real-browser check: the quiet reason line on the customer page also carries
 * "对方现在 HH:MM" and "一般隔 N 天回" when (and only when) they are known.
 * Isolated dev database, synthetic customers only.  Usage as in docs/REAL_BROWSER_REPRO.md:
 *   node tools/repro_customer_hint_habit.cjs --url http://127.0.0.1:18310 --app-root . --db-dir <scratch> --label after --out <dir>
 * Exit codes: 0 pass, 20 defect present, 21 infrastructure failure.
 */
'use strict';
const fs = require('fs');
const path = require('path');
const { spawnSync } = require('child_process');
const ROOT = path.resolve(__dirname, '..');

function args() {
  const o = { url: 'http://127.0.0.1:18310', label: 'run', out: '', appRoot: ROOT, dbDir: '', python: path.join(ROOT, '.venv', 'bin', 'python') };
  const a = process.argv.slice(2);
  for (let i = 0; i < a.length; i += 2) {
    const k = a[i].replace(/^--/, '');
    if (k === 'app-root') o.appRoot = path.resolve(a[i + 1]);
    else if (k === 'db-dir') o.dbDir = path.resolve(a[i + 1]);
    else o[k] = a[i + 1];
  }
  if (!o.out || !o.dbDir) throw new Error('--out and --db-dir are required');
  return o;
}

const PY = `
import json, os, sys
from pathlib import Path
root, db_dir, cid = sys.argv[1], sys.argv[2], int(sys.argv[3])
os.environ.update({'CRM_ENV': 'development', 'TRADE_OS_DEV_SQLITE': '1', 'TRADE_OS_DATA_BACKEND': 'sqlite'})
os.environ.pop('TRADE_OS_DATABASE_URL', None)
sys.path.insert(0, root)
import db
from datetime import date, timedelta
import followup_store as store
from followup_candidates import Candidate
db.DB_DIR = db_dir
db.set_db_user('hamid')
conn = db.get_db()
today = date.today()
rows = json.loads(sys.argv[4])
for ago, direction, text in rows:
    conn.execute("INSERT INTO follow_up_logs (customer_id, content, follow_date, direction, activity_type) VALUES (?,?,?,?,?)",
                 (cid, text, (today - timedelta(days=ago)).isoformat(), direction, 'email' if direction == 'outbound' else 'customer_reply'))
last = conn.execute('SELECT id FROM follow_up_logs WHERE customer_id=? ORDER BY id DESC LIMIT 1', (cid,)).fetchone()[0]
cand = Candidate(account_id=str(cid), rule='R1', certainty='certain', evidence_event_id=str(last), evidence_event_ids=(str(last),),
                 facts={}, reason=sys.argv[5], owner='hamid', priority=1, dedupe_key=f'hamid:{cid}:R1:{last}')
store.upsert_candidates(conn, [cand], today)
conn.commit()
`;

(async () => {
  const o = args();
  fs.mkdirSync(o.out, { recursive: true });
  let pw;
  try { pw = require(path.join(ROOT, 'browser-extension', 'node_modules', 'playwright')); } catch (e) { console.error('Playwright 未安装'); process.exit(21); }
  const browser = await pw.chromium.launch();
  const page = await (await browser.newContext({ viewport: { width: 1440, height: 900 }, locale: 'zh-CN' })).newPage();
  // optional: pin the clock to a weekday so the work-window bar is shown, e.g. --fake-now 2026-11-06T20:10:00+08:00
  if (o['fake-now']) await page.clock.install({ time: new Date(o['fake-now']) });
  const call = (url, method, body) => page.evaluate(async ({ url, method, body }) => {
    const r = await fetch(url, { method, credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: body ? JSON.stringify(body) : undefined });
    let d = null; try { d = await r.json(); } catch (e) {}
    return { status: r.status, data: d };
  }, { url, method, body });
  const report = { label: o.label, checks: [] };
  const check = (id, title, pass, detail) => report.checks.push({ id, title, pass: !!pass, detail });
  try {
    await page.goto(`${o.url}/`, { waitUntil: 'load' });
    await call('/api/auth/login', 'POST', { user: 'hamid' });
    const stamp = Date.now() % 100000;
    const mk = async (company, tz) => {
      const c = await call('/api/customers', 'POST', { company: `${company} ${stamp}`, name: 'Ana', country: 'Spain', timezone: tz });
      if (c.status !== 201) throw new Error('create failed ' + c.status);
      return c.data.id;
    };
    const rich = await mk('Harbor Plastics', 'Europe/Madrid');
    const thin = await mk('Nordlys Trading', '');
    const reason = '客户 10月29日来信，之后还没有回复';
    const seedRows = (id, rows) => {
      const r = spawnSync(o.python, ['-c', PY, o.appRoot, o.dbDir, String(id), JSON.stringify(rows), reason], { encoding: 'utf8' });
      if (r.status !== 0) throw new Error('seed failed: ' + (r.stderr || r.stdout).slice(0, 300));
    };
    seedRows(rich, [[60, 'outbound', '已发资料'], [58, 'inbound', '客户收到，问了价格'], [45, 'outbound', '已发报价 3.2 USD/kg'], [42, 'inbound', '客户觉得偏高，想再谈'],
      [30, 'outbound', '已回复，给了折扣'], [28, 'inbound', '客户确认可以'], [12, 'outbound', '已寄样'], [9, 'inbound', '客户问了3mm透明板最新价格和交期，说下周要定。']]);
    seedRows(thin, [[10, 'inbound', '客户问了价格']]);
    const open = async (id) => {
      await page.goto(`${o.url}/`, { waitUntil: 'load' });
      await page.waitForFunction(() => typeof openEditModal === 'function', null, { timeout: 20000 });
      await page.evaluate((cid) => openEditModal(cid), id);
      await page.waitForSelector('#customerNextTask h2', { timeout: 10000 });
      await page.waitForTimeout(1200);
      return page.evaluate(() => { const el = document.querySelector('#customerFollowHint'); return el ? el.innerText.trim() : ''; });
    };
    const richText = await open(rich);
    await page.screenshot({ path: path.join(o.out, '01-customer-rich-1440.png') });
    const clock = await page.evaluate(() => { const el = document.querySelector('#customerClock'); return el ? el.innerText.replace(/\s+/g, ' ').trim() : ''; });
    check('rich-has-clock', '有时区的客户：下一步下面有「对方当地时间」和「我们这边」两个时钟', /对方当地时间 \d\d:\d\d .*我们这边 \d\d:\d\d/.test(clock), clock);
    check('reason-unchanged', '理由那一行保持原样，不夹带时间', richText === reason, richText);
    check('rich-one-line-group', '时钟区没有按钮', await page.evaluate(() => document.querySelectorAll('#customerClock button, #customerClock a, #customerClock input').length === 0), '');
    const thinText = await open(thin);
    await page.screenshot({ path: path.join(o.out, '02-customer-thin-1440.png') });
    check('thin-reason-unchanged', '另一位客户：理由原样', thinText === reason, thinText);
  } catch (e) {
    console.error(String(e.message || e));
    await browser.close();
    process.exit(21);
  }
  await browser.close();
  fs.writeFileSync(path.join(o.out, 'report.json'), JSON.stringify(report, null, 2));
  report.checks.forEach((c) => console.log(`${c.pass ? '通过' : '失败'}  ${c.title}  ${c.detail}`));
  process.exit(report.checks.every((c) => c.pass) ? 0 : 20);
})();
