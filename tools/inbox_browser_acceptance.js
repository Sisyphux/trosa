// Executed only by Tabbit against the local rehearsal service.  Uploads go
// through the visible input and ordinary HTTP endpoints, never an analyzer.
// The launcher replaces these literals before handing this program to the
// browser-owned runtime (which intentionally does not inherit shell env).
const origin = '__TROSA_INBOX_BROWSER_URL__';
const samples = '__TROSA_INBOX_BROWSER_SAMPLES__';
const fixtureContactId = '__TROSA_INBOX_BROWSER_CONTACT_ID__';
await page.setViewportSize({width: 1440, height: 900});
await page.goto('about:blank', {waitUntil: 'domcontentloaded'});
await page.goto(origin, {waitUntil: 'domcontentloaded'});
await page.evaluate(async () => { localStorage.clear(); sessionStorage.clear(); if (navigator.serviceWorker) await Promise.all((await navigator.serviceWorker.getRegistrations()).map(r => r.unregister())); });
await page.reload({waitUntil: 'domcontentloaded'});
if (await page.locator('#loginOverlay').isVisible()) {
  await page.waitForFunction(() => !document.querySelector('#loginOverlay') || getComputedStyle(document.querySelector('#loginOverlay')).display === 'none' || !!document.querySelector('#loginUsers [data-user-id="hamid"]'), null, {timeout:15000});
  if (await page.locator('#loginUsers [data-user-id="hamid"]').count()) await page.locator('#loginUsers [data-user-id="hamid"]').click();
}
await page.locator('[data-page="inbox"]').first().click();
await page.locator('#page-inbox.active').waitFor();
const apiKinds = await page.evaluate(() => fetch('/api/inbox').then(r => r.json()).then(x => x.questions.filter(q => q.headline === '调查证据 CSV').map(q => [q.kind, q.response_schema.attachments.allowed])));
if (apiKinds.length !== 1 || apiKinds[0][0] !== 'investigation_request' || !apiKinds[0][1]) throw new Error('fixture API contract missing: ' + JSON.stringify(apiKinds));
const list = page.locator('#inboxList');
await page.waitForFunction(() => !document.querySelector('#inboxList')?.textContent?.includes('正在整理 Inbox'), null, {timeout: 15000});
// A successful submit (and Undo) re-renders the workspace and closes the open
// card, so 返回队列 may legitimately already be gone. Never wait 30s for it.
const backToQueue = async () => {
  const back = page.getByText('返回队列', {exact: true});
  if (await back.count()) await back.first().click({timeout: 5000}).catch(() => {});
};
const inboxCount = async () => Number((await page.locator('#inboxOverview strong').innerText()).trim());
const cardCount = await list.locator('.inbox-question-open').count();
if (cardCount < 8) throw new Error('fixture card count mismatch: ' + cardCount + ' / ' + (await list.innerText()));
// Draft survives an actual render caused by closing and reopening its card.
await list.locator('.inbox-question-open').first().click();
const field = page.locator('[data-inbox-field="answer"]').first(); await field.fill('draft survives rerender');
await backToQueue(); await list.locator('.inbox-question-open').first().click();
if (await field.inputValue() !== 'draft survives rerender') throw new Error('draft lost');
// An invalid optional email is a controlled server-side rejection.  Its text
// answer must remain editable, and the normal retry must then resolve.
await page.locator('[data-inbox-field="contact_id"]').fill(fixtureContactId);
await page.locator('[data-inbox-field="confirmed_email"]').fill('not-an-email');
await page.getByRole('button', {name:'提交回答', exact:true}).click();
await page.locator('.inbox-inline-error').getByText('请输入有效邮箱').waitFor({timeout:15000});
if (await field.inputValue() !== 'draft survives rerender') throw new Error('draft lost after rejected request');
await page.locator('[data-inbox-field="confirmed_email"]').fill('');
const firstCardId = await page.locator('.inbox-question-active').getAttribute('id');
await page.getByRole('button', {name:'提交回答', exact:true}).click();
await page.locator('#' + firstCardId).waitFor({state:'detached', timeout:15000});
if (await inboxCount() !== 7) throw new Error('Inbox count did not update immediately after a response: ' + await inboxCount());
// Correct the deliberately stale rehearsal email through the normal answer
// form, then use the visible Undo action returned by the same response.
await backToQueue();
await list.locator('.inbox-question-open').filter({hasText:'更正失效邮箱'}).click();
await page.locator('[data-inbox-field="contact_id"]').fill(fixtureContactId);
await page.locator('[data-inbox-field="confirmed_email"]').fill('buyer-fixed@rehearsal.example');
await page.getByRole('button', {name:'提交回答', exact:true}).click();
await page.waitForFunction(() => !document.querySelector('#inboxList')?.textContent?.includes('更正失效邮箱') || document.querySelector('.inbox-inline-error')?.textContent, null, {timeout:15000});
const undo = page.getByRole('button', {name:'撤销', exact:true}).last(); if (!await undo.count()) throw new Error('email correction failed: ' + await page.locator('#inboxList').innerText()); await undo.click();
await page.waitForFunction(() => document.querySelector('#inboxList')?.textContent?.includes('更正失效邮箱'), null, {timeout:15000});
if (await inboxCount() !== 7) throw new Error('Inbox count did not restore after Undo: ' + await inboxCount());
// Drive each upload through the rendered input.  A supported/not_supported
// conclusion resolves its investigation question server-side, but the analysis
// stays visible in the still-open rendered card until the next queue reload.
const uploads = [
  {file:'supported.csv', filter:'CSV', status:'supported', citation:'CSV', ref:'2'},
  {file:'not_supported.xlsx', filter:'XLSX', status:'not_supported', citation:'Trade rows', ref:'2'},
  {file:'insufficient.pdf', filter:'PDF', status:'not_supported', citation:'PDF', ref:'1'},
  {file:'image_without_ocr.png', filter:'图片', status:'insufficient'},
  {file:'broken.pdf', filter:'损坏', status:'analysis_failed'},
];
const observed = [];
for (const spec of uploads) {
  await backToQueue();
  const card = list.locator('.inbox-question-open').filter({hasText: spec.filter}).first();
  await card.click();
  const input = page.locator('#inboxList input[type=file]');
  if (!await input.count()) throw new Error('Inbox upload input missing for ' + spec.file + ': ' + (await list.innerText()));
  await input.setInputFiles(samples + '/' + spec.file);
  await page.locator('.inbox-upload-status').getByText(/证据已分析|失败/).waitFor({timeout:30000});
  const result = page.locator('.inbox-analysis-result').first();
  await result.waitFor({timeout:15000});
  const status = await result.getAttribute('data-analysis-status');
  const resultText = await result.innerText();
  if (status !== spec.status) throw new Error(spec.file + ' expected status ' + spec.status + ' but rendered ' + status + ': ' + resultText);
  if (spec.citation && !resultText.includes(spec.citation)) throw new Error(spec.file + ' citation source missing: ' + resultText);
  if (spec.ref && !new RegExp('(^|[^0-9])' + spec.ref + '([^0-9]|$)').test(resultText)) throw new Error(spec.file + ' citation location missing: ' + resultText);
  observed.push({file: spec.file, expected: spec.status, status, resultText});
}
// Reload so the durable queue (investigation questions resolved by supported /
// not_supported evidence) matches the rendered workspace before the remaining
// keyboard and empty-state checks run against fresh server state.
await page.reload({waitUntil:'domcontentloaded'});
await page.locator('#loginOverlay').waitFor({state:'hidden', timeout:15000}).catch(() => {});
await page.locator('[data-page="inbox"]').first().click();
await page.locator('#page-inbox.active').waitFor({timeout:15000});
await page.waitForFunction(() => !document.querySelector('#inboxList')?.textContent?.includes('正在整理 Inbox'), null, {timeout:15000});
// Responsive and reduced-motion inspection use the same rendered fixture.
// Responsive inspection: wide desktop, iPad (portrait/landscape) and iPhone.
const layouts=[]; for (const width of [1024, 390, 720]) { await page.setViewportSize({width,height:900}); const entry = {width, visible: await page.locator('#page-inbox.active').isVisible(), cards: await list.locator('.inbox-question-open').count()}; layouts.push(entry); if (!entry.visible) throw new Error('Inbox not visible at width ' + width); }
await page.setViewportSize({width:1440,height:900});
// 200% zoom must keep the primary Inbox surface usable.
await page.evaluate(() => { document.body.style.zoom = '2'; });
const zoomVisible = await page.locator('#page-inbox.active').isVisible();
const zoomCards = await list.locator('.inbox-question-open').count();
layouts.push({width:'200%', visible: zoomVisible, cards: zoomCards});
if (!zoomVisible || zoomCards < 1) throw new Error('Inbox unusable at 200% zoom');
await page.evaluate(() => { document.body.style.zoom = ''; });
// Keyboard can activate an Inbox card, focus stays inside the opened card, and
// every status surface is a polite live region.
await backToQueue();
const keyboardCard = list.locator('.inbox-question-open').first(); await keyboardCard.focus(); await keyboardCard.press('Enter');
if (!await page.locator('.inbox-question-active').count()) throw new Error('keyboard did not open Inbox card');
if (await page.locator('.inbox-inline-error').getAttribute('aria-live') !== 'polite') throw new Error('Inbox error live region missing');
await page.locator('.inbox-question-active [data-inbox-field]').first().focus();
await page.keyboard.press('Tab');
const focusInside = await page.evaluate(() => !!document.activeElement.closest('.inbox-question-active'));
if (!focusInside) throw new Error('keyboard focus did not stay inside the opened Inbox card');
await backToQueue();
await list.locator('.inbox-question-open').filter({hasText:'图片'}).click();
if (await page.locator('.inbox-upload-status').getAttribute('aria-live') !== 'polite') throw new Error('Inbox upload live region missing');
await backToQueue();
// Reduced motion must not break the workspace.
await page.emulateMedia({reducedMotion:'reduce'});
const reducedMotionApplied = await page.evaluate(() => window.matchMedia('(prefers-reduced-motion: reduce)').matches);
if (!reducedMotionApplied || !await page.locator('#page-inbox.active').isVisible()) throw new Error('reduced-motion rendering broke Inbox');
// Finish every remaining fixture card through its rendered controls. The email
// card was restored by Undo, so this second correction deliberately completes it.
let finished = 0;
while (await list.locator('.inbox-question-open').count()) {
  if (++finished > 12) throw new Error('Inbox queue did not drain: ' + (await list.innerText()));
  await list.locator('.inbox-question-open').first().click();
  const card = page.locator('.inbox-question-active');
  const headline = (await card.locator('h3').innerText()).trim();
  const email = card.locator('[data-inbox-field="confirmed_email"]');
  // Fact questions expose optional correction fields.  Only the explicit
  // email-correction card should exercise that write path; ordinary text
  // answers must stay ordinary text answers.
  if (headline === '更正失效邮箱' && await email.count()) { await card.locator('[data-inbox-field="contact_id"]').fill(fixtureContactId); await email.fill('buyer-final@rehearsal.example'); }
  const decision = card.locator('[data-inbox-field="decision"]'); if (await decision.count()) await decision.fill('skip');
  const answer = card.locator('[data-inbox-field="answer"]'); if (await answer.count()) await answer.fill('fixture complete');
  const conclusion = card.locator('[data-inbox-field="conclusion"]'); if (await conclusion.count()) await conclusion.fill('insufficient');
  const activeId = await card.getAttribute('id');
  await card.getByRole('button', {name:'提交回答', exact:true}).click();
  await page.locator('#' + activeId).waitFor({state:'detached', timeout:15000}).catch(async () => {
    const error = await page.locator('.inbox-inline-error').first().innerText().catch(() => '');
    throw new Error('Inbox card ' + headline + ' was not completed: ' + error);
  });
  await page.waitForTimeout(300);
}
await page.getByText('当前没有需要你判断的问题', {exact:true}).waitFor({timeout:15000});
return {cards: 8, draft:true, retry:true, emailCorrection:true, undo:true, keyboard:true, focusManaged:true, ariaLive:true, reducedMotion:true, empty:true, uploads:observed, layouts};
