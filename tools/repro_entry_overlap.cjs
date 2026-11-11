/*
 * Real-browser reproduction: on the account chooser, the column under the
 * pointer shows its small vertical name on top of the large name
 * (选账号界面竖排小字和大字重叠).
 *
 * The chooser is opened the way a member reaches it (from the navigation
 * index), the pointer rests on one member's column, and the opacity of that
 * column's vertical name and large name are sampled over time.  While the large
 * name is shown, the vertical one must be hidden.
 *
 * Usage (see docs/REAL_BROWSER_REPRO.md):
 *   node tools/repro_entry_overlap.cjs --url http://127.0.0.1:18190 --label after --out docs/repro/entry-overlap/after
 *
 * Exit codes: 0 all checks pass, 20 at least one check fails (the defect is
 * present), 21 infrastructure failure.
 */
'use strict';

const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');

function parseArgs(argv) {
  const options = { url: 'http://127.0.0.1:18190', label: 'run', out: '' };
  for (let i = 0; i < argv.length; i += 1) {
    if (argv[i] === '--url') options.url = argv[++i];
    else if (argv[i] === '--label') options.label = argv[++i];
    else if (argv[i] === '--out') options.out = argv[++i];
    else throw new Error(`unknown argument: ${argv[i]}`);
  }
  if (!options.out) throw new Error('--out <directory> is required');
  return options;
}

function loadPlaywright() {
  try {
    return require(path.join(ROOT, 'browser-extension', 'node_modules', 'playwright'));
  } catch (error) {
    console.error('Playwright 未安装：请在 browser-extension 执行 npm ci');
    process.exit(21);
  }
}

async function main() {
  const options = parseArgs(process.argv.slice(2));
  const outDir = path.resolve(ROOT, options.out);
  fs.mkdirSync(outDir, { recursive: true });
  const { chromium } = loadPlaywright();
  const report = { label: options.label, url: options.url, generatedAt: new Date().toISOString(), checks: [], samples: {}, screenshots: [] };
  let browser;
  try {
    browser = await chromium.launch({ headless: true });
    report.browser = `chromium ${browser.version()}`;
    const page = await browser.newPage({ viewport: { width: 1440, height: 760 }, locale: 'zh-CN' });
    await page.goto(`${options.url}/`, { waitUntil: 'load' });
    await page.waitForSelector('#page-overview.active', { timeout: 20000 });
    await page.addStyleTag({ content: '.entry-lan, [aria-label^="复制其他设备访问地址"] { visibility: hidden !important; }' });
    await page.click('#roomBrand');
    await page.waitForFunction(() => !document.getElementById('roomIndex').hidden, null, { timeout: 8000 });
    await page.waitForTimeout(600);
    await page.click('#roomIndex .room-index-more button:last-child');
    const field = '#loginUsers [data-user-id="hamid"]';
    await page.waitForSelector(field, { state: 'visible', timeout: 8000 });
    const box = await page.locator(field).boundingBox();
    await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
    const sample = () => page.evaluate((sel) => {
      const el = document.querySelector(sel);
      return {
        active: el.classList.contains('is-active'),
        // What the eye sees: opacity times the text colour's own alpha.
        spine: (() => {
          const style = getComputedStyle(el.querySelector('.entry-spine'));
          const m = /rgba?\(([^)]+)\)/.exec(style.color);
          const parts = m ? m[1].split(/[ ,\/]+/).filter(Boolean) : [];
          const alpha = parts.length === 4 ? Number(parts[3]) : 1;
          return Number(style.opacity) * alpha;
        })(),
        body: Number(getComputedStyle(el.querySelector('.entry-body')).opacity),
      };
    }, field);
    const overlaps = [];
    let elapsed = 0;
    for (const wait of [150, 250, 300, 400, 600, 1300]) {
      await page.waitForTimeout(wait);
      elapsed += wait;
      const s = await sample();
      report.samples[`${elapsed}ms`] = s;
      if (s.body > 0.5 && s.spine > 0.15) overlaps.push(`${elapsed}ms 竖排=${s.spine.toFixed(2)} 大字=${s.body.toFixed(2)}`);
      if (elapsed === 700 || elapsed === 3000) {
        const name = `${elapsed}ms-hover-hamid.png`;
        await page.screenshot({ path: path.join(outDir, name) });
        report.screenshots.push(name);
      }
    }
    report.checks.push({
      id: 'no-overlap-while-hovered',
      title: '鼠标停在某人那一栏时，大字出现后竖排小字已隐藏',
      pass: overlaps.length === 0,
      detail: overlaps.length ? `重叠：${overlaps.join('；')}` : '各采样点都没有重叠',
    });
  } catch (error) {
    const message = String(error && error.message ? error.message : error).split('\n')[0];
    console.error(`浏览器不可用或场景异常：${message}`);
    process.exit(21);
  } finally {
    if (browser) await browser.close();
  }
  fs.writeFileSync(path.join(outDir, 'report.json'), `${JSON.stringify(report, null, 2)}\n`, 'utf8');
  const failed = report.checks.filter((c) => !c.pass);
  for (const c of report.checks) console.log(`${c.pass ? 'PASS' : 'FAIL'}  ${c.id}  —  ${c.title}\n        ${c.detail}`);
  console.log(JSON.stringify(report.samples));
  process.exit(failed.length ? 20 : 0);
}

main();
