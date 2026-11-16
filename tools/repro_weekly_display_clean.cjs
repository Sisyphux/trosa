'use strict';
// Synthetic API fixtures on an isolated loopback dev server; no business writes.
const fs = require('fs');
const path = require('path');
const { chromium } = require('../browser-extension/node_modules/playwright');
const args = Object.fromEntries(process.argv.slice(2).reduce((a, v, i, all) => i % 2 ? a : [...a, [v.slice(2), all[i + 1]]], []));
if (!/^http:\/\/127\.0\.0\.1:\d+$/.test(args.url) || !args.out) throw Error('loopback --url and --out required');
const raw = '【客户背调 2026-11-12】\n主营亚克力板，报价 105529 元，2026-11-12 交货，3mm 200张。<br><mark class="hl-green">保留高亮</mark>\n【/客户背调】\n客户回复了邮件：[ticket:105529] 请发样品 [订单:105529]';
const next = '2026-11-20 寄出 12 件 [ticket:105530]';
const profile = '【客户背调 2026-11-10】<mark class="hl-green">Acrylic importer</mark><br>Delivery 2026-12-20, 200 sheets [订单:42] [ticket:42]【/客户背调】';
const profileZh = '【客户背调 2026-11-10】亚克力进口商，2026-12-20 交货，200 张 [ticket:42]【/客户背调】';
const record = { customer_profile: profile, customer_profile_zh: profileZh, customer_id: 90001, customer_company: 'Synthetic Acrylic', date: '2026-11-12', actual_work: raw, next_step: next };
const item = { id: 90002, type: 'follow', date: '2026-11-12', content: raw, activity_type: 'email', next_plan: next };
const dossier = { customer: { id: 90001, company: 'Synthetic Acrylic', owner: 'hamid', profile, translation_pending: true }, week_activity: [item], recent_timeline: [{...item, id: 90003, date: '2026-11-01', content: '', result: raw}], open_tasks: [] };
(async () => {
  fs.mkdirSync(args.out, {recursive:true});
  const report = { label: args.label, checks: [], pageErrors: [], syntheticOnly: true, screenshots: [] };
  let browser;
  try {
    browser = await chromium.launch({headless:true});
    report.browser = browser.version();
    const page = await browser.newPage({viewport:{width:1440,height:900}, reducedMotion:'reduce'});
    page.on('pageerror', e => report.pageErrors.push(e.message));
    await page.route('**/api/weekly-summary/**', r => r.fulfill({json:{reported_customers:[record]}}));
    let detailCalls = 0;
    await page.route('**/api/overview/customers/hamid/90001?**', r => r.fulfill({json: ++detailCalls === 1 ? dossier : {...dossier, customer: {...dossier.customer, profile_zh: profileZh, translation_pending: false}}}));
    await page.goto(args.url);
    await page.waitForSelector('.wk-record-name');
    const check = (id, pass) => report.checks.push({id,pass});
    const inspectProfile = async (selector, translated, label) => {
      const el = page.locator(selector).first();
      const text = await el.textContent(), title = await el.getAttribute('title') || '';
      check(label+'-profile-hides-metadata-and-markup', !/客户背调|ticket:|2026-11-10|<mark|<br/.test(text+title));
      check(label+'-profile-keeps-business-content', text.includes(translated ? '亚克力进口商' : 'Acrylic importer') && text.includes('2026-12-20'));
      if (translated) check(label+'-profile-original-title', title.includes('Acrylic importer') && title.includes('[订单:42]'));
    };
    const inspect = async (selector, label) => {
      const text = await page.locator(selector).first().innerText();
      check(label+'-hides-markers', !/【\/?客户背调|\[ticket:\d+\]/.test(text));
      check(label+'-keeps-content-dates-numbers', ['主营亚克力板','105529 元','2026-11-12','3mm 200张','请发样品','[订单:105529]','2026-11-20','12 件'].every(s=>text.includes(s)));
      check(label+'-keeps-highlight', await page.locator(selector+' mark.hl-green').count() > 0);
    };
    for (const width of [1440,1280,960,768,390]) {
      await page.setViewportSize({width,height:900});
      await inspectProfile('.wk-record-profile', true, 'weekly-'+width);
      await inspect('.wk-record', 'weekly-'+width);
      if (width === 1440 || width === 390) {
        const name = 'weekly-'+width+'.png'; await page.screenshot({path:path.join(args.out,name)}); report.screenshots.push(name);
      }
    }
    await page.locator('.wk-record-name').first().click();
    await page.waitForSelector('#ovDetailModal .wk-dossier-item');
    await inspectProfile('[data-dossier-profile]', false, 'detail-original');
    await page.waitForFunction(() => document.querySelector('[data-dossier-profile]')?.textContent.includes('亚克力进口商'));
    await inspectProfile('[data-dossier-profile]', true, 'detail-translated');
    for (const width of [1440,768,390]) {
      await page.setViewportSize({width,height:900});
      await inspect('#ovDetailModal .wk-dossier-item', 'detail-'+width);
      const earlier = await page.locator('.wk-dossier-item').last().innerText();
      check('earlier-'+width+'-hides-markers', !/【\/?客户背调|\[ticket:\d+\]/.test(earlier));
      if (width !== 768) { const name='detail-'+width+'.png'; await page.screenshot({path:path.join(args.out,name)}); report.screenshots.push(name); }
    }
    const preserved = await page.evaluate(({raw,next,profile,profileZh}) => {
      const data = weeklyCurrentData('hamid').reported_customers[0];
      return data.customer_profile === profile && data.customer_profile_zh === profileZh && data.actual_work === raw && data.next_step === next && renderRichText(raw).includes('[ticket:105529]') && renderRichText(raw).includes('【客户背调 2026-11-12】');
    }, {raw,next,profile,profileZh});
    check('raw-cache-and-global-renderer-unchanged', preserved);
    check('no-page-errors', report.pageErrors.length===0);
    fs.writeFileSync(path.join(args.out,'report.json'),JSON.stringify(report,null,2)+'\n');
    const failed=report.checks.filter(c=>!c.pass); console.log(`${report.checks.length-failed.length}/${report.checks.length} passed`); console.log(failed);
    process.exitCode=failed.length?20:0;
  } catch(e) {
    report.infrastructureError = String(e);
    fs.writeFileSync(path.join(args.out,'report.json'),JSON.stringify(report,null,2)+'\n');
    console.error(e); process.exitCode=21;
  }
  finally { if(browser) await browser.close(); }
})();
