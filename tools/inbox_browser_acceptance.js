// Executed only by Tabbit against the local rehearsal service.  Uploads go
// through the visible input and ordinary HTTP endpoints, never an analyzer.
// The launcher replaces these literals before handing this program to the
// browser-owned runtime (which intentionally does not inherit shell env).
const origin = '__TROSA_INBOX_BROWSER_URL__';
const samples = '__TROSA_INBOX_BROWSER_SAMPLES__';
await page.setViewportSize({width: 1440, height: 900});
await page.goto(origin, {waitUntil: 'domcontentloaded'});
if (await page.locator('#loginOverlay').isVisible()) await page.locator('#loginUsers [data-user-id="hamid"]').click();
await page.locator('[data-page="inbox"]').first().click();
await page.locator('#page-inbox.active').waitFor();
const list = page.locator('#inboxList');
await page.waitForFunction(() => !document.querySelector('#inboxList')?.textContent?.includes('正在整理 Inbox'), null, {timeout: 15000});
const cardCount = await list.locator('.inbox-question-open').count();
if (cardCount < 8) throw new Error('fixture card count mismatch: ' + cardCount + ' / ' + (await list.innerText()));
// Draft survives an actual render caused by closing and reopening its card.
await list.locator('.inbox-question-open').first().click();
const field = page.locator('[data-inbox-field="answer"]').first(); await field.fill('draft survives rerender');
await page.getByText('返回队列', {exact:true}).click(); await list.locator('.inbox-question-open').first().click();
if (await field.inputValue() !== 'draft survives rerender') throw new Error('draft lost');
// Drive each upload through the rendered input; analysis remains visible even
// when supported/not_supported closes its investigation question.
const uploads = [['supported.csv','supported'], ['not_supported.xlsx','not_supported'], ['insufficient.pdf','insufficient'], ['image_without_ocr.png','insufficient'], ['broken.pdf','analysis_failed']];
const observed = [];
for (const [name, expected] of uploads) {
  await page.getByText('返回队列', {exact:true}).click().catch(()=>{});
  const card = list.locator('.inbox-question-open').filter({hasText: name.includes('csv') ? 'CSV' : name.includes('xlsx') ? 'XLSX' : name.includes('image') ? '图片' : name.includes('broken') ? '损坏' : 'PDF'}).first();
  await card.click(); const input = page.locator('#inboxList input[type=file]'); await input.setInputFiles(samples + '/' + name);
  await page.locator('.inbox-upload-status').getByText(/证据已分析|失败/).waitFor({timeout:30000});
  observed.push({name, expected, text: await page.locator('#page-inbox').innerText()});
}
// Responsive and reduced-motion inspection use the same rendered fixture.
const layouts=[]; for (const width of [1024, 390, 720]) { await page.setViewportSize({width,height:900}); layouts.push({width, visible: await page.locator('#page-inbox.active').isVisible()}); }
await page.emulateMedia({reducedMotion:'reduce'});
return {cards: await list.locator('.inbox-question-open').count(), draft:true, uploads:observed.map(x=>({name:x.name, expected:x.expected, seen:x.text.includes(x.expected)})), layouts};
