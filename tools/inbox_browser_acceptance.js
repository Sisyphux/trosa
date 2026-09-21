// Executed only by Tabbit against the local rehearsal service.  Uploads go
// through the visible input and ordinary HTTP endpoints, never an analyzer.
// The launcher replaces these literals before handing this program to the
// browser-owned runtime (which intentionally does not inherit shell env).
const origin = '__TROSA_INBOX_BROWSER_URL__';
const samples = '__TROSA_INBOX_BROWSER_SAMPLES__';
await page.setViewportSize({width: 1440, height: 900});
await page.goto('about:blank', {waitUntil: 'domcontentloaded'});
await page.goto(origin, {waitUntil: 'domcontentloaded'});
await page.evaluate(async () => { localStorage.clear(); sessionStorage.clear(); if (navigator.serviceWorker) await Promise.all((await navigator.serviceWorker.getRegistrations()).map(r => r.unregister())); });
await page.reload({waitUntil: 'domcontentloaded'});
if (await page.locator('#loginOverlay').isVisible()) await page.locator('#loginUsers [data-user-id="hamid"]').click();
await page.locator('[data-page="inbox"]').first().click();
await page.locator('#page-inbox.active').waitFor();
const apiKinds = await page.evaluate(() => fetch('/api/inbox').then(r => r.json()).then(x => x.questions.filter(q => q.headline === '调查证据 CSV').map(q => [q.kind, q.response_schema.attachments.allowed])));
if (apiKinds.length !== 1 || apiKinds[0][0] !== 'investigation_request' || !apiKinds[0][1]) throw new Error('fixture API contract missing: ' + JSON.stringify(apiKinds));
const list = page.locator('#inboxList');
await page.waitForFunction(() => !document.querySelector('#inboxList')?.textContent?.includes('正在整理 Inbox'), null, {timeout: 15000});
const cardCount = await list.locator('.inbox-question-open').count();
if (cardCount < 8) throw new Error('fixture card count mismatch: ' + cardCount + ' / ' + (await list.innerText()));
// Draft survives an actual render caused by closing and reopening its card.
await list.locator('.inbox-question-open').first().click();
const field = page.locator('[data-inbox-field="answer"]').first(); await field.fill('draft survives rerender');
await page.getByText('返回队列', {exact:true}).click(); await list.locator('.inbox-question-open').first().click();
if (await field.inputValue() !== 'draft survives rerender') throw new Error('draft lost');
// Correct the deliberately stale rehearsal email through the normal answer
// form, then use the visible Undo action returned by the same response.
await page.getByText('返回队列', {exact:true}).click();
await list.locator('.inbox-question-open').filter({hasText:'更正失效邮箱'}).click();
await page.locator('[data-inbox-field="contact_id"]').fill('1');
await page.locator('[data-inbox-field="confirmed_email"]').fill('buyer-fixed@rehearsal.example');
await page.getByRole('button', {name:'提交回答', exact:true}).click();
await page.waitForFunction(() => !document.querySelector('#inboxList')?.textContent?.includes('更正失效邮箱') || document.querySelector('.inbox-inline-error')?.textContent, null, {timeout:15000});
const undo = page.getByRole('button', {name:'撤销', exact:true}).last(); if (!await undo.count()) throw new Error('email correction failed: ' + await page.locator('#inboxList').innerText()); await undo.click();
await page.waitForFunction(() => document.querySelector('#inboxList')?.textContent?.includes('更正失效邮箱'), null, {timeout:15000});
// Drive each upload through the rendered input; analysis remains visible even
// when supported/not_supported closes its investigation question.
const uploads = [['supported.csv','supported'], ['not_supported.xlsx','not_supported'], ['insufficient.pdf','insufficient'], ['image_without_ocr.png','insufficient'], ['broken.pdf','analysis_failed']];
const observed = [];
for (const [name, expected] of uploads) {
  await page.getByText('返回队列', {exact:true}).click().catch(()=>{});
  const card = list.locator('.inbox-question-open').filter({hasText: name.includes('csv') ? 'CSV' : name.includes('xlsx') ? 'XLSX' : name.includes('image') ? '图片' : name.includes('broken') ? '损坏' : 'PDF'}).first();
  await card.click(); const input = page.locator('#inboxList input[type=file]');
  if (!await input.count()) throw new Error('Inbox upload input missing for ' + name + ': ' + (await list.innerText()));
  await input.setInputFiles(samples + '/' + name);
  await page.locator('.inbox-upload-status').getByText(/证据已分析|失败/).waitFor({timeout:30000});
  observed.push({name, expected, text: await page.locator('#page-inbox').innerText()});
}
// Responsive and reduced-motion inspection use the same rendered fixture.
const layouts=[]; for (const width of [1024, 390, 720]) { await page.setViewportSize({width,height:900}); layouts.push({width, visible: await page.locator('#page-inbox.active').isVisible()}); }
await page.emulateMedia({reducedMotion:'reduce'});
// Finish every remaining fixture card through its rendered controls. The email
// card was restored by Undo, so this second correction deliberately completes it.
while (await list.locator('.inbox-question-open').count()) {
  await list.locator('.inbox-question-open').first().click();
  const card = page.locator('.inbox-question-active');
  const email = card.locator('[data-inbox-field="confirmed_email"]');
  if (await email.count()) { await card.locator('[data-inbox-field="contact_id"]').fill('1'); await email.fill('buyer-final@rehearsal.example'); }
  const decision = card.locator('[data-inbox-field="decision"]'); if (await decision.count()) await decision.fill('skip');
  const answer = card.locator('[data-inbox-field="answer"]'); if (await answer.count()) await answer.fill('fixture complete');
  const conclusion = card.locator('[data-inbox-field="conclusion"]'); if (await conclusion.count()) await conclusion.fill('insufficient');
  const activeId = await card.getAttribute('id');
  await card.getByRole('button', {name:'提交回答', exact:true}).click();
  await page.locator('#' + activeId).waitFor({state:'detached', timeout:15000});
}
await page.getByText('当前没有需要你判断的问题', {exact:true}).waitFor({timeout:15000});
return {cards: 8, draft:true, empty:true, uploads:observed.map(x=>({name:x.name, expected:x.expected, seen:x.text.includes(x.expected)})), layouts};
