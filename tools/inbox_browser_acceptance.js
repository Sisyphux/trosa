// Executed by tools/run_browser_acceptance.cjs inside a locked headless
// Playwright Chromium against the local rehearsal service.  Uploads go through
// the visible input and ordinary HTTP endpoints, never an analyzer.  The driver
// replaces these literals before running the program, so the fixture values stay
// out of the committed source.
const origin = '__TROSA_INBOX_BROWSER_URL__';
const samples = '__TROSA_INBOX_BROWSER_SAMPLES__';
const fixtureContactId = '__TROSA_INBOX_BROWSER_CONTACT_ID__';
await page.setViewportSize({width: 1440, height: 900});
await page.goto('about:blank', {waitUntil: 'domcontentloaded'});
await page.goto(origin, {waitUntil: 'domcontentloaded'});
await page.evaluate(async () => { localStorage.clear(); sessionStorage.clear(); if (navigator.serviceWorker) await Promise.all((await navigator.serviceWorker.getRegistrations()).map(r => r.unregister())); });
// This run exercises the classic look's performance tiers; swiss (default) has none.
await page.evaluate(() => localStorage.setItem('trosa.type', 'classic'));
await page.reload({waitUntil: 'domcontentloaded'});
if (await page.locator('#loginOverlay').isVisible()) {
  await page.waitForFunction(() => !document.querySelector('#loginOverlay') || getComputedStyle(document.querySelector('#loginOverlay')).display === 'none' || !!document.querySelector('#loginUsers [data-user-id="hamid"]'), null, {timeout:15000});
if (await page.locator('#loginUsers [data-user-id="hamid"]').count()) await page.locator('#loginUsers [data-user-id="hamid"]').click();
}
// Use the real Settings control to enable performance-priority mode in the
// isolated rehearsal account, then exercise Inbox under that mode.
// There is no resident navigation: summon the index layer, exactly as a user does.
const openIndex = async () => {
  await page.locator('#roomBrand').click();
  await page.locator('#roomIndex').waitFor({state: 'visible', timeout: 15000});
};
await openIndex();
await page.locator('#roomIndex').getByRole('button', {name:'设置', exact:true}).click();
await page.locator('#page-settings.active').waitFor();
await page.locator('#preferenceInterfacePerformance').selectOption('performance');
await page.getByRole('button', {name:'保存设置', exact:true}).click();
await page.waitForFunction(() => document.documentElement.dataset.interfacePerformance === 'performance' && document.documentElement.classList.contains('performance-priority'), null, {timeout:15000});
await openIndex();
await page.locator('#roomIndex [data-page="inbox"]').first().click();
await page.locator('#inboxTabOther').click();
await page.locator('#page-inbox.active').waitFor();
const apiKinds = await page.evaluate(() => fetch('/api/inbox').then(r => r.json()).then(x => x.questions.filter(q => q.headline === '调查证据 CSV').map(q => [q.kind, q.response_schema.attachments.allowed])));
if (apiKinds.length !== 1 || apiKinds[0][0] !== 'investigation_request' || !apiKinds[0][1]) throw new Error('fixture API contract missing: ' + JSON.stringify(apiKinds));
// Every choice field must carry human labels so the UI never asks the operator
// to type a raw enum value such as "approve" or "insufficient".
const choiceContracts = await page.evaluate(() => fetch('/api/inbox').then(r => r.json()).then(x => x.questions.map(q => {
  const field = ((q.response_schema || {}).fields || []).find(f => f.input_type === 'choice' || f.input_type === 'investigation_conclusion');
  return field ? [q.kind, (field.choices || []).map(c => c.value)] : null;
}).filter(Boolean)));
if (!choiceContracts.length || choiceContracts.some(c => c[1].length < 2)) throw new Error('choice label contract missing: ' + JSON.stringify(choiceContracts));
const list = page.locator('#inboxList');
await page.waitForFunction(() => !document.querySelector('#inboxList')?.textContent?.includes('正在整理 Inbox'), null, {timeout: 15000});
const initialRunStatuses = await page.locator('#inboxSelaRuns .inbox-sela-run').evaluateAll((rows) => rows.map(row => row.dataset.status));
if (initialRunStatuses.some(status => status === 'running' || status === 'completed')) {
  throw new Error('normal Sela background state is occupying the Inbox: ' + initialRunStatuses.join(','));
}
// A successful submit (and Undo) re-renders the workspace and closes the open
// card, so 返回队列 may legitimately already be gone. Never wait 30s for it.
const backToQueue = async () => {
  const back = page.getByText('返回队列', {exact: true});
  if (await back.count()) await back.first().click({timeout: 5000}).catch(() => {});
};
// Choice fields are now real buttons backed by a hidden value input; the
// contact picker is a hydrated <select>.  Drive both through their visible
// controls instead of the pre-fix "type a magic value" text inputs.
const selectInboxContact = async (contactId) => {
  const select = page.locator('.inbox-question-active [data-inbox-field="contact_id"]');
  await select.waitFor({state:'visible', timeout:15000});
  await page.waitForFunction((id) => {
    const el = document.querySelector('.inbox-question-active [data-inbox-field="contact_id"]');
    return !!(el && el.tagName === 'SELECT' && Array.from(el.options).some(o => o.value === String(id)));
  }, String(contactId), {timeout:15000});
  await select.selectOption(String(contactId));
};
const inboxCount = async () => Number((await page.locator('#inboxOverview strong').innerText()).trim());
const performanceModeApplied = await page.evaluate(() => document.documentElement.dataset.interfacePerformance === 'performance' && document.documentElement.classList.contains('performance-priority'));
if (!performanceModeApplied) throw new Error('Inbox is not running in performance-priority mode');
const cardCount = await list.locator('.inbox-question-open').count();
if (cardCount < 10) throw new Error('fixture card count mismatch: ' + cardCount + ' / ' + (await list.innerText()));
// On a phone-sized viewport the primary action belongs in the Inbox header;
// it must not float over a question or force horizontal scrolling.
await page.setViewportSize({width:390,height:844});
const mobileActionLayout = await page.evaluate(() => {
  const action = document.querySelector('#page-inbox .inbox-command-shelf');
  const actionRect = action?.getBoundingClientRect();
  const searchRect = document.querySelector('#globalPageSearch')?.getBoundingClientRect();
  const overlapsSearch = !!actionRect && !!searchRect && actionRect.left < searchRect.right
    && actionRect.right > searchRect.left && actionRect.top < searchRect.bottom
    && actionRect.bottom > searchRect.top;
  const rows = Array.from(document.querySelectorAll('#inboxList .inbox-question-row'));
  const overlapsRow = !!actionRect && rows.some((row) => {
    const rect = row.getBoundingClientRect();
    return actionRect.left < rect.right && actionRect.right > rect.left
      && actionRect.top < rect.bottom && actionRect.bottom > rect.top;
  });
  return {
    position: action ? getComputedStyle(action).position : '',
    overlapsRow,
    overlapsSearch,
    scrollWidth: document.documentElement.scrollWidth,
    viewportWidth: window.innerWidth,
  };
});
if (mobileActionLayout.position === 'fixed' || mobileActionLayout.overlapsRow || mobileActionLayout.overlapsSearch
    || mobileActionLayout.scrollWidth > mobileActionLayout.viewportWidth + 1) {
  throw new Error('mobile Inbox action is obscuring content or overflowing: ' + JSON.stringify(mobileActionLayout));
}
await page.setViewportSize({width:1440,height:900});
// Draft survives an actual render caused by closing and reopening its card.
await list.locator('.inbox-question-open').first().click();
const field = page.locator('[data-inbox-field="answer"]').first(); await field.fill('draft survives rerender');
await backToQueue(); await list.locator('.inbox-question-open').first().click();
if (await field.inputValue() !== 'draft survives rerender') throw new Error('draft lost');
// An invalid optional email is a controlled server-side rejection.  Its text
// answer must remain editable, and the normal retry must then resolve.
await selectInboxContact(fixtureContactId);
await page.locator('[data-inbox-field="confirmed_email"]').fill('not-an-email');
await page.getByRole('button', {name:'保存回答', exact:true}).click();
await page.locator('.inbox-inline-error').getByText('请输入有效邮箱').waitFor({timeout:15000});
if (await field.inputValue() !== 'draft survives rerender') throw new Error('draft lost after rejected request');
await page.locator('[data-inbox-field="confirmed_email"]').fill('');
const firstCardId = await page.locator('.inbox-question-active').getAttribute('id');
await page.getByRole('button', {name:'保存回答', exact:true}).click();
await page.locator('#' + firstCardId).waitFor({state:'detached', timeout:15000});
if (await inboxCount() !== 9) throw new Error('Inbox count did not update immediately after a response: ' + await inboxCount());
// A linked Sela decision must persist a durable automatic-run queue entry.
// The fixture does not run Sela itself, so its state should remain queued without occupying the Inbox.
await backToQueue();
await list.locator('.inbox-question-open').filter({hasText:'Sela 需要确认 prospect 的下一步'}).click();
const selaCard = page.locator('.inbox-question-active');
const selaCardText = await selaCard.innerText();
if (!selaCardText.includes('下一步先做什么？') || !selaCardText.includes('回答后的计划') || !selaCardText.includes('公开来源')) throw new Error('structured Sela context is incomplete: ' + selaCardText);
if (!selaCardText.includes('公开证据候选（未验证）') || !selaCardText.includes('buyer@inbox-browser-prospect.example')) throw new Error('unverified contact candidate is missing: ' + selaCardText);
await selaCard.getByRole('link', {name:'查看证据 1', exact:true}).waitFor({timeout:15000});
const beforeContactSaveCount = await inboxCount();
const contactSaveTarget = await page.evaluate(async () => {
  const payload = await fetch('/api/inbox').then(response => response.json());
  const q = payload.questions.find(item => item.headline === 'Sela 需要确认 prospect 的下一步');
  return q && q.sela_contact_save;
});
if (!contactSaveTarget || !contactSaveTarget.candidate || !contactSaveTarget.customer_id) throw new Error('Sela contact save target missing: ' + JSON.stringify(contactSaveTarget));
await selaCard.getByRole('button', {name:'保存邮箱', exact:true}).click();
await page.getByRole('heading', {name:'确认保存联系人邮箱', exact:true}).waitFor({timeout:10000});
await page.getByRole('button', {name:'保存联系人邮箱', exact:true}).click();
const contactSaveToast = page.locator('#toastContainer .toast.success').filter({hasText:'邮箱尚未验证，Inbox 回答仍未提交。'});
await contactSaveToast.waitFor({timeout:15000});
const contactSaveToastText = await contactSaveToast.innerText();
if (!contactSaveToastText.includes('联系人邮箱已保存到 ' + (contactSaveTarget.company || '对应的 Trosa 客户')) || !contactSaveToastText.includes('撤销')) {
  throw new Error('confirmed contact save success/undo feedback missing: ' + contactSaveToastText);
}
if (await inboxCount() !== beforeContactSaveCount || !await selaCard.isVisible()) throw new Error('saving contact email closed or resolved the Inbox request');
const savedContacts = await page.evaluate(async (customerId) => fetch('/api/customers/' + customerId + '/contacts').then(response => response.json()), contactSaveTarget.customer_id);
if (!savedContacts.some(contact => contact.email === 'buyer@inbox-browser-prospect.example')) throw new Error('confirmed contact email was not stored: ' + JSON.stringify(savedContacts));
const contactSaveUndo = page.locator('#toastContainer .toast').filter({hasText:'联系人邮箱已保存到 Inbox Browser Prospect'}).getByRole('button', {name:'撤销', exact:true});
await contactSaveUndo.click();
await page.getByText('已撤销联系人邮箱保存；Inbox 请求仍保持打开。', {exact:true}).waitFor({timeout:15000});
const contactsAfterUndo = await page.evaluate(async (customerId) => fetch('/api/customers/' + customerId + '/contacts').then(response => response.json()), contactSaveTarget.customer_id);
if (contactsAfterUndo.some(contact => contact.email === 'buyer@inbox-browser-prospect.example')) throw new Error('Undo left the confirmed email in Trosa contacts');
if (await inboxCount() !== beforeContactSaveCount) throw new Error('Undo changed the Inbox question count');
const selaContactEmailFieldKey = await page.evaluate(async () => {
  const payload = await fetch('/api/inbox').then(response => response.json());
  const question = payload.questions.find(item => item.headline === 'Sela 需要确认 prospect 的下一步');
  const field = (((question || {}).response_schema || {}).fields || []).find(item => item.fact_field === 'contact_email');
  return field && field.key;
});
if (!selaContactEmailFieldKey) throw new Error('Sela contact-email answer field missing after contact-save Undo');
await page.locator('.inbox-question-active [data-inbox-field="' + selaContactEmailFieldKey + '"]').fill('');
await page.locator('.inbox-question-active [data-inbox-field="note"]').fill('邮箱尚未确认；先继续整理公开来源。');
await selaCard.getByRole('button', {name:'继续整理公开来源', exact:true}).click();
const selaCardId = await selaCard.getAttribute('id');
await selaCard.getByRole('button', {name:'保存回答', exact:true}).click();
await page.locator('#' + selaCardId).waitFor({state:'detached', timeout:15000});
await page.getByText('回答已保存并排入自动处理。会从原阻塞点继续，不会发送邮件，也不会修改客户、联系人、待办。', {exact:true}).waitFor({timeout:15000});
if (await page.locator('#inboxSelaRuns .inbox-sela-run').filter({hasText:'Inbox Browser Prospect'}).count()) {
  throw new Error('newly queued Sela run is occupying the Inbox');
}
const linkedInboxId = Number(String(selaCardId).replace('inbox-question-', ''));
const linkedReceipt = await page.evaluate(async (id) => fetch('/api/inbox/questions/' + id + '/sela-handoff').then(r => r.json()), linkedInboxId);
if (linkedReceipt.status !== 'queued' || !linkedReceipt.automatic_run) throw new Error('linked Sela answer was not queued: ' + JSON.stringify(linkedReceipt));
if (await inboxCount() !== 8) throw new Error('linked Sela answer did not resolve its Inbox request: ' + await inboxCount());
// An answered request without a unique prospect must be honest about staying
// manual. It gets a needs_review receipt, but never an automatic-run status.
await backToQueue();
await list.locator('.inbox-question-open').filter({hasText:'Sela 请求但未关联 prospect'}).click();
const unlinkedCard = page.locator('.inbox-question-active');
await unlinkedCard.getByRole('button', {name:'继续整理公开来源', exact:true}).click();
const unlinkedCardId = await unlinkedCard.getAttribute('id');
await unlinkedCard.getByRole('button', {name:'保存回答', exact:true}).click();
await page.locator('#' + unlinkedCardId).waitFor({state:'detached', timeout:15000});
// Scope to the Sela receipt panel: the same sentence is also rendered as a
// transient toast in #toastContainer, so an unscoped exact-text wait races
// with the toast when both are mounted at once.
await page.locator('#inboxSelaRuns').getByText('这条请求没有唯一对应的潜在客户。回答已保存，但不会自动继续。', {exact:true}).waitFor({timeout:15000});
const unlinkedResume = page.locator('#inboxSelaRuns .inbox-sela-run').filter({hasText:'Unlinked Inbox Prospect'});
await unlinkedResume.waitFor({timeout:15000});
if (await unlinkedResume.getAttribute('data-status') !== 'needs_review') throw new Error('unlinked Sela answer did not stay in review: ' + await unlinkedResume.innerText());
const unlinkedInboxId = Number(String(unlinkedCardId).replace('inbox-question-', ''));
const unlinkedReceipt = await page.evaluate(async (id) => fetch('/api/inbox/questions/' + id + '/sela-handoff').then(r => r.json()), unlinkedInboxId);
if (unlinkedReceipt.status !== 'needs_review' || unlinkedReceipt.automatic_run) throw new Error('unlinked Sela answer has an incorrect handoff receipt: ' + JSON.stringify(unlinkedReceipt));
if (await inboxCount() !== 7) throw new Error('unlinked Sela answer did not resolve its Inbox request: ' + await inboxCount());
// Correct the deliberately stale rehearsal email through the normal answer
// form, then use the visible Undo action returned by the same response.
await backToQueue();
await list.locator('.inbox-question-open').filter({hasText:'更正失效邮箱'}).click();
await selectInboxContact(fixtureContactId);
await page.locator('[data-inbox-field="confirmed_email"]').fill('buyer-fixed@rehearsal.example');
await page.getByRole('button', {name:'保存回答', exact:true}).click();
await page.waitForFunction(() => !document.querySelector('#inboxList')?.textContent?.includes('更正失效邮箱') || document.querySelector('.inbox-inline-error')?.textContent, null, {timeout:15000});
const undo = page.getByRole('button', {name:'撤销', exact:true}).last(); if (!await undo.count()) throw new Error('email correction failed: ' + await page.locator('#inboxList').innerText()); await undo.click();
await page.waitForFunction(() => document.querySelector('#inboxList')?.textContent?.includes('更正失效邮箱'), null, {timeout:15000});
if (await inboxCount() !== 7) throw new Error('Inbox count did not restore after Undo: ' + await inboxCount());
// Drive each upload through the rendered input.  A supported/not_supported
// conclusion resolves its investigation question server-side, so the open card
// can be replaced by a queue reload while the analysis is still being asserted
// (see the state-based wait below).
const uploads = [
  {file:'supported.csv', filter:'CSV', status:'supported', citation:'CSV', ref:'2'},
  {file:'not_supported.xlsx', filter:'XLSX', status:'not_supported', citation:'Trade rows', ref:'2'},
  {file:'insufficient.pdf', filter:'PDF', status:'not_supported', citation:'PDF', ref:'1'},
  {file:'image_without_ocr.png', filter:'图片', status:'insufficient'},
  {file:'broken.pdf', filter:'损坏', status:'analysis_failed'},
];
const observed = [];
const uploadTimings = [];
// The server-side analysis is deterministic (measured 0.09-0.36s across 200+
// rehearsal uploads), but the app re-renders all of #inboxList whenever it
// reloads its queue: a deferred reload after answering a question, the
// global-sync debounce, or 返回队列. A reload that lands after this upload
// resolved its investigation question legitimately drops the open card, and with
// it the rendered .inbox-upload-status / .inbox-analysis-result. Waiting on those
// nodes with a fixed timeout is therefore racy: wait on the app's own state, the
// durable source the render is derived from, and keep asserting the same analysis
// facts, preferring the rendered node whenever it is still on screen.
const INBOX_ANALYSIS_TIMEOUT_MS = 15000; // ~40x the measured p99 (0.36s), for CI jitter.
const readInboxUpload = (questionId) => page.evaluate((qid) => {
  const state = window.inboxState || {};
  const analyses = Array.isArray((state.analysisStates || {})[qid]) ? state.analysisStates[qid] : null;
  const upload = (state.uploadStates || {})[qid] || null;
  const node = document.querySelector('#inboxList .inbox-analysis-result');
  const statusEl = document.querySelector('.inbox-upload-status');
  return {
    analyses: analyses,
    upload: upload,
    rendered: node ? {status: node.getAttribute('data-analysis-status'), text: node.textContent || ''} : null,
    statusText: statusEl ? (statusEl.textContent || '').trim() : null,
  };
}, questionId);
const citationText = (analyses) => (analyses || []).map((a) => (a.citations || []).map((c) => ((c.source || '') + ' ' + (c.row_or_page == null ? '' : c.row_or_page)).trim()).join(' ')).join(' ');
for (const spec of uploads) {
  await backToQueue();
  const card = list.locator('.inbox-question-open').filter({hasText: spec.filter}).first();
  const questionId = String(await card.getAttribute('data-inbox-id') || '');
  if (!questionId) throw new Error('Inbox upload card has no question id for ' + spec.file + ': ' + (await list.innerText()));
  await card.click();
  const input = page.locator('#inboxList input[type=file]');
  if (!await input.count()) throw new Error('Inbox upload input missing for ' + spec.file + ': ' + (await list.innerText()));
  const uploadStartedAt = Date.now();
  const uploadStatusTextBefore = await page.locator('.inbox-upload-status').first().innerText().catch(() => '<absent>');
  await input.setInputFiles(samples + '/' + spec.file);
  let snapshot = null;
  while (Date.now() - uploadStartedAt < INBOX_ANALYSIS_TIMEOUT_MS) {
    snapshot = await readInboxUpload(questionId);
    if ((snapshot.analyses && snapshot.analyses.length) || (snapshot.upload && snapshot.upload.error)) break;
    await page.waitForTimeout(100);
  }
  const elapsedMs = Date.now() - uploadStartedAt;
  const analyses = snapshot && snapshot.analyses;
  const upload = (snapshot && snapshot.upload) || null;
  const rendered = snapshot && snapshot.rendered;
  const settled = (analyses && analyses.length) || (upload && upload.error);
  uploadTimings.push({file: spec.file, statusBefore: uploadStatusTextBefore, elapsedMs: elapsedMs, rendered: !!rendered});
  if (!settled) {
    const diagnostic = await readInboxUpload(questionId).catch(() => null);
    throw new Error(spec.file + ' evidence analysis did not settle within ' + INBOX_ANALYSIS_TIMEOUT_MS + 'ms (elapsed ' + elapsedMs + 'ms; statusBefore=' + JSON.stringify(uploadStatusTextBefore) + '): ' + JSON.stringify(diagnostic));
  }
  if (upload && upload.error) throw new Error(spec.file + ' evidence upload failed: ' + (upload.message || ''));
  if (rendered && snapshot.statusText && !/证据已分析|失败/.test(snapshot.statusText)) {
    throw new Error(spec.file + ' unexpected upload status text: ' + JSON.stringify(snapshot.statusText));
  }
  const status = (rendered && rendered.status) || (analyses && analyses[0] ? analyses[0].status : null);
  const resultText = rendered ? rendered.text : citationText(analyses);
  if (status !== spec.status) throw new Error(spec.file + ' expected status ' + spec.status + ' but saw ' + status + ': ' + resultText);
  if (spec.citation && !resultText.includes(spec.citation)) throw new Error(spec.file + ' citation source missing: ' + resultText);
  if (spec.ref && !new RegExp('(^|[^0-9])' + spec.ref + '([^0-9]|$)').test(resultText)) throw new Error(spec.file + ' citation location missing: ' + resultText);
  observed.push({file: spec.file, expected: spec.status, status, resultText});
}
// Reload so the durable queue (investigation questions resolved by supported /
// not_supported evidence) matches the rendered workspace before the remaining
// keyboard and empty-state checks run against fresh server state.
await page.reload({waitUntil:'domcontentloaded'});
await page.locator('#loginOverlay').waitFor({state:'hidden', timeout:15000}).catch(() => {});
await page.locator('#roomBrand').click();
await page.locator('#roomIndex [data-page="inbox"]').first().click();
await page.locator('#inboxTabOther').click();
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
  if (headline === '更正失效邮箱' && await email.count()) { await selectInboxContact(fixtureContactId); await email.fill('buyer-final@rehearsal.example'); }
  const skipChoice = card.getByRole('button', {name:'本轮跳过', exact:true}); if (await skipChoice.count()) await skipChoice.click();
  const answer = card.locator('[data-inbox-field="answer"]'); if (await answer.count()) await answer.fill('fixture complete');
  const insufficientChoice = card.getByRole('button', {name:'资料不足', exact:true}); if (await insufficientChoice.count()) await insufficientChoice.click();
  const activeId = await card.getAttribute('id');
  await card.getByRole('button', {name:'保存回答', exact:true}).click();
  await page.locator('#' + activeId).waitFor({state:'detached', timeout:15000}).catch(async () => {
    const error = await page.locator('.inbox-inline-error').first().innerText().catch(() => '');
    throw new Error('Inbox card ' + headline + ' was not completed: ' + error);
  });
  await page.waitForTimeout(300);
}
await page.getByText('当前没有需要你判断的问题', {exact:true}).waitFor({timeout:15000});
return {cards: 10, draft:true, retry:true, emailCorrection:true, undo:true, automaticResumeQueued:true, unlinkedSelaManual:true, mobileActionClear:true, keyboard:true, focusManaged:true, ariaLive:true, reducedMotion:true, performanceMode:performanceModeApplied, empty:true, uploads:observed, uploadTimings, layouts};
