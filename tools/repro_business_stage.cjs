/*
 * Real-browser verification for the six business stages.
 *
 * It signs in on an isolated local Trosa, opens the real customer editor and
 * batch editor, filters the customer pool by the new stages, and measures the
 * badge colours in the real page.  Run it against the pre-change source and it
 * reports the defect (exit 20); run it against the change and it exits 0.
 *
 * Usage:
 *   node tools/repro_business_stage.cjs --url http://127.0.0.1:18191 --label after --out docs/repro/business-stage/after
 */
'use strict';

const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');
const EXIT_PASS = 0;
const EXIT_DEFECT = 20;
const EXIT_INFRA = 21;
const STAGES = ['', '回复过', '寄过样品', '报过价', '成交', '流失'];
const COLOUR_STAGES = ['回复过', '寄过样品', '报过价', '成交', '流失'];

function parseArgs(argv) {
  const options = { url: process.env.TROSA_REPRO_URL || 'http://127.0.0.1:18191', label: 'run', out: '' };
  for (let i = 0; i < argv.length; i += 1) {
    const key = argv[i];
    const value = argv[i + 1];
    if (key === '--url') { options.url = value; i += 1; }
    else if (key === '--label') { options.label = value; i += 1; }
    else if (key === '--out') { options.out = value; i += 1; }
    else throw new Error(`unknown argument: ${key}`);
  }
  if (!options.out) throw new Error('--out <directory> is required');
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

async function settle(page) {
  await page.waitForTimeout(450);
}

async function runScenario(browser, options, outDir, report) {
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, locale: 'zh-CN' });
  const page = await context.newPage();
  page.on('pageerror', (error) => report.pageErrors.push(String(error.message).slice(0, 200)));
  const shot = async (name) => {
    await settle(page);
    await page.screenshot({ path: path.join(outDir, name) });
    report.screenshots.push(name);
  };
  const check = (id, title, pass, detail) => {
    report.checks.push({ id, title, pass: !!pass, detail });
  };

  try {
    await page.goto(`${options.url}/`, { waitUntil: 'load', timeout: 30000 });
    // The isolated server opens on the weekly board; sign in through the index,
    // exactly as an office-LAN visitor does.
    await page.click('#roomBrand');
    await page.waitForFunction(() => !document.getElementById('roomIndex').hidden, null, { timeout: 8000 });
    await page.click('#accountButton');
    await page.waitForSelector('#loginUsers [data-user-id="hamid"]', { state: 'visible', timeout: 20000 });
    await page.click('#loginUsers [data-user-id="hamid"]');
    await page.waitForFunction(
      () => getComputedStyle(document.getElementById('loginOverlay')).display === 'none',
      null, { timeout: 15000 },
    );
    await shot('01-signed-in.png');

    // 1. Every user-facing dropdown must offer exactly the same six stages.
    const dropdowns = await page.evaluate(() => {
      const read = (id, labelKey) => {
        const el = document.getElementById(id);
        return {
          values: el ? Array.from(el.options).map((o) => o.value) : [],
          labels: el ? Array.from(el.options).map((o) => o.textContent.trim()) : [],
          labelKey,
        };
      };
      return {
        filter: read('newPoolStageFilter', '全部业务阶段'),
        edit: read('editBusinessStage', '未标记'),
        add: read('addExistBusinessStage', '未标记'),
        today: read('todayQuickEditBusinessStage', '不修改'),
      };
    });
    report.measurements.dropdowns = dropdowns;
    for (const [name, expectedFirstLabel] of [
      ['filter', '全部业务阶段'], ['edit', '未标记'], ['add', '未标记'], ['today', '不修改'],
    ]) {
      const d = dropdowns[name];
      const pass = JSON.stringify(d.values) === JSON.stringify(STAGES) && d.labels[0] === expectedFirstLabel;
      check(`dropdown-${name}`,
        `${name} 下拉按同一顺序提供六个业务阶段，首项文案保持不变`,
        pass, `值=${d.values.join('|') || '(空)'}，首项=${d.labels[0] || '(缺失)'}`);
    }

    // 2. Creating a customer must accept all six values and reject a made-up one.
    const prefix = `阶段验证${Date.now()}`;
    const created = await page.evaluate(async ({ prefix, stages }) => {
      const result = {};
      for (const stage of stages) {
        const key = stage || '空';
        try {
          const response = await fetch('/api/customers', {
            method: 'POST', credentials: 'same-origin',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name: `${prefix}-${key}`, company: `${prefix}-${key}`, business_stage: stage }),
          });
          const body = await response.json();
          // The create response carries the id, not the saved row: read the
          // customer back through the real detail API to verify persistence.
          let saved;
          if (body.id) {
            const detailResponse = await fetch(`/api/customers/${body.id}`, { credentials: 'same-origin' });
            if (detailResponse.ok) {
              const detail = await detailResponse.json();
              saved = detail.business_stage;
            }
          }
          result[key] = { status: response.status, id: body.id, saved };
        } catch (error) {
          result[key] = { status: 0, error: String(error) };
        }
      }
      try {
        const response = await fetch('/api/customers', {
          method: 'POST', credentials: 'same-origin',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ name: `${prefix}-乱值`, business_stage: '已报价' }),
        });
        result['乱值'] = { status: response.status };
      } catch (error) {
        result['乱值'] = { status: 0, error: String(error) };
      }
      return result;
    }, { prefix, stages: STAGES });
    report.measurements.created = created;
    check('create-accepts-six-stages',
      '新建客户逐档保存六个业务阶段',
      STAGES.every((stage) => created[stage || '空'] && created[stage || '空'].status === 201 && created[stage || '空'].saved === stage),
      STAGES.map((stage) => `${stage || '空'}:${created[stage || '空'] ? created[stage || '空'].status : '缺失'}`).join(' '));
    check('create-rejects-unknown-stage',
      '新建客户拒绝未定义的业务阶段',
      created['乱值'] && created['乱值'].status === 400,
      `乱值返回 ${created['乱值'] ? created['乱值'].status : '缺失'}`);

    // 3. The real customer editor loads a saved new stage into its dropdown.
    const targetId = created['报过价'] && created['报过价'].id;
    if (targetId) {
      await page.evaluate(async (id) => { await window.openEditModal(id); }, targetId);
      await page.waitForFunction(
        () => document.getElementById('customerEditModal').classList.contains('is-open')
          || getComputedStyle(document.getElementById('customerEditModal')).display !== 'none',
        null, { timeout: 12000 },
      ).catch(() => {});
      await page.waitForTimeout(900);
      const editState = await page.evaluate(() => ({
        value: document.getElementById('editBusinessStage').value,
        options: Array.from(document.getElementById('editBusinessStage').options).map((o) => o.value),
      }));
      report.measurements.editor = editState;
      check('editor-loads-new-stage',
        '真实客户编辑弹窗能读到并选中“报过价”',
        editState.value === '报过价' && JSON.stringify(editState.options) === JSON.stringify(STAGES),
        `选中=${editState.value || '(空)'}，选项=${editState.options.join('|') || '(空)'}`);
      await shot('02-customer-editor.png');
      await page.keyboard.press('Escape');
      await page.waitForTimeout(300);
    } else {
      check('editor-loads-new-stage', '真实客户编辑弹窗能读到并选中“报过价”', false, '没有可用的测试客户');
    }

    // 4. The batch stage picker must offer the same values.
    await page.evaluate(() => window.openBatchSetModal('business-stage', 'new'));
    await page.waitForTimeout(250);
    const batchOptions = await page.evaluate(() => Array.from(
      document.getElementById('batchSetValue').options,
    ).map((o) => o.value));
    report.measurements.batchOptions = batchOptions;
    check('batch-dropdown-six-stages',
      '批量改业务阶段的弹窗提供六个值',
      JSON.stringify(batchOptions) === JSON.stringify(STAGES),
      `选项=${batchOptions.join('|') || '(空)'}`);
    await page.keyboard.press('Escape');

    // 5. The list filter really narrows the pool by a new stage.
    await page.evaluate((value) => {
      document.getElementById('newPoolSearch').value = value;
      const select = document.getElementById('newPoolStageFilter');
      select.value = '报过价';
      select.dispatchEvent(new Event('change', { bubbles: true }));
    }, prefix);
    await page.waitForTimeout(900);
    const filtered = await page.evaluate(() => Array.from(
      document.querySelectorAll('#poolBodyNone .cust-company, #poolBodyToday .cust-company, #poolBodyOverdue .cust-company, #poolBodyTomorrow .cust-company, #poolBodyWeek .cust-company'),
    ).map((el) => el.textContent.trim()));
    report.measurements.filtered = filtered;
    check('list-filter-by-new-stage',
      '列表按“报过价”筛选后只剩这条测试客户',
      filtered.length === 1 && filtered[0] === `${prefix}-报过价`,
      `结果=${JSON.stringify(filtered)}`);

    // 6. Badge colours are visually distinct in a real browser.
    const badges = await page.evaluate((stages) => {
      const host = document.createElement('div');
      host.id = 'stageBadgeEvidence';
      host.style.cssText = 'position:fixed;right:16px;bottom:16px;z-index:99999;background:#fff;padding:12px;display:flex;gap:8px;';
      stages.forEach((stage) => {
        const wrapper = document.createElement('span');
        wrapper.dataset.stage = stage;
        wrapper.innerHTML = window.statusBadge(stage);
        host.appendChild(wrapper);
      });
      // The real page puts badges inside #trosa, where the v3 !important
      // badge rules and our stage-specific overrides actually apply.
      (document.getElementById('trosa') || document.body).appendChild(host);
      return stages.map((stage) => {
        const el = host.querySelector(`[data-stage="${stage}"] .badge`);
        const cs = getComputedStyle(el);
        return {
          stage,
          className: el.className,
          background: cs.backgroundColor,
          color: cs.color,
        };
      });
    }, COLOUR_STAGES);
    report.measurements.badges = badges;
    const signatures = badges.map((b) => `${b.background}|${b.color}`);
    const newSignatures = signatures.slice(0, 3);
    const doneSignature = signatures[3];
    const passed = new Set(newSignatures).size === 3
      && newSignatures.every((s) => s !== doneSignature)
      && signatures[4] === doneSignature;
    check('badge-colours-distinct',
      '三个新阶段颜色两两可分且不同于默认样式，成交和流失保持原样',
      passed, JSON.stringify(badges));
    await shot('03-badge-colours.png');

    check('no-page-errors', '页面运行期间没有 JavaScript 报错', report.pageErrors.length === 0,
      report.pageErrors.join(' / ') || '无');
  } catch (error) {
    const message = String(error && error.message ? error.message : error).split('\n')[0];
    if (/ERR_CONNECTION|ECONNREFUSED|net::ERR_|Timeout.*exceeded/i.test(message)) {
      throw Object.assign(new Error(message), { infra: true });
    }
    report.checks.push({ id: 'scenario-completed', title: '真实浏览器场景能完整走完', pass: false, detail: message.slice(0, 300) });
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
  const report = {
    label: options.label, url: options.url, generatedAt: new Date().toISOString(),
    checks: [], measurements: {}, screenshots: [], pageErrors: [],
  };
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
  for (const c of report.checks) console.log(`${c.pass ? 'PASS' : 'FAIL'} ${c.id}: ${c.detail}`);
  console.log(`${report.label}: ${report.checks.length - failed.length}/${report.checks.length} checks passed`);
  process.exit(failed.length ? EXIT_DEFECT : EXIT_PASS);
}

main().catch((error) => { console.error(error); process.exit(EXIT_INFRA); });
