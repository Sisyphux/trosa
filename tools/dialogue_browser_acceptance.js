// Executed by tools/run_browser_acceptance.cjs inside a locked headless
// Playwright Chromium against the isolated local rehearsal service.  It drives
// the sela<->human dialogue window with the real customer controls (Today entry,
// conversation list, reply box, suggestions, attachment upload, keyboard) and
// writes the S3 evidence screenshots.  The driver replaces the placeholder
// literals below before running the program, so the seeded ids stay out of the
// committed source.
const origin = '__TROSA_DIALOGUE_BROWSER_URL__';
const customerId = '__TROSA_DIALOGUE_BROWSER_CUSTOMER_ID__';
const samples = '__TROSA_DIALOGUE_BROWSER_SAMPLES__';
const shots = '__TROSA_DIALOGUE_BROWSER_SHOTS__';
const threads = JSON.parse('__TROSA_DIALOGUE_BROWSER_THREADS__');

const shot = (name) => page.screenshot({ path: shots + '/' + name });
// A page switch can run inside a View Transition; wait for it to finish so a
// screenshot never captures the outgoing page's frozen snapshot.
const settle = () => page.waitForFunction(() => !document.documentElement.dataset.motionType, null, { timeout: 5000 }).catch(() => {});
const threadUrl = (id) => 'article.dialogue-thread[data-thread-id="' + id + '"]';
const toast = (text) => page.locator('#toastContainer .toast').filter({ hasText: text });

await page.setViewportSize({ width: 1280, height: 900 });
await page.goto('about:blank', { waitUntil: 'domcontentloaded' });
await page.goto(origin, { waitUntil: 'domcontentloaded' });
await page.evaluate(async () => {
  localStorage.clear();
  sessionStorage.clear();
  if (navigator.serviceWorker) await Promise.all((await navigator.serviceWorker.getRegistrations()).map((r) => r.unregister()));
});
await page.reload({ waitUntil: 'domcontentloaded' });
if (await page.locator('#loginOverlay').isVisible()) {
  await page.waitForFunction(() => !document.querySelector('#loginOverlay')
    || getComputedStyle(document.querySelector('#loginOverlay')).display === 'none'
    || !!document.querySelector('#loginUsers [data-user-id="hamid"]'), null, { timeout: 15000 });
  if (await page.locator('#loginUsers [data-user-id="hamid"]').count()) await page.locator('#loginUsers [data-user-id="hamid"]').click();
}
await page.locator('#roomBrand').waitFor({ state: 'visible', timeout: 15000 });

const openDialogue = async () => {
  await page.locator('#roomBrand').click();
  await page.locator('#roomIndex').waitFor({ state: 'visible', timeout: 15000 });
  // 对话是 Inbox 的页签：导航里只有 Inbox，有等你回复的对话时它直接打开「对话」。
  await page.locator('#roomIndex [data-page="inbox"]').first().click();
  await page.locator('#page-dialogue.active').waitFor({ timeout: 15000 });
  await page.locator('#dialogueList .dialogue-row').first().waitFor({ timeout: 15000 });
  await settle();
};
const selectThread = async (id) => {
  await page.locator('.dialogue-row[data-dialogue-id="' + id + '"]').click();
  await page.waitForFunction((tid) => !!document.querySelector('article.dialogue-thread[data-thread-id="' + tid + '"]'), id, { timeout: 15000 });
};
const groupCount = (group) => page.locator('.dialogue-group[data-group="' + group + '"] .dialogue-row').count();
const groupOrder = async () => page.locator('.dialogue-group').evaluateAll((els) => els.map((el) => el.dataset.group));
const selectedId = () => page.evaluate(() => window.dialogueSelectedId);

// ---- 1. Today entry counts only「等你」and the badge matches -----------------
await page.locator('#todayDialogueEntry').waitFor({ state: 'visible', timeout: 15000 });
await page.waitForFunction(() => (document.getElementById('todayDialogueCount') || {}).textContent === '4', null, { timeout: 15000 });
const todayCount = (await page.locator('#todayDialogueCount').innerText()).trim();
const navBadge = (await page.locator('#dialogueNavCount').innerText()).trim();
const todayLabel = (await page.locator('#todayDialogueLabel').innerText()).trim();
if (todayCount !== '4') throw new Error('Today dialogue count is not the「等你」count: ' + todayCount);
if (navBadge !== '4') throw new Error('dialogue nav badge is not the「等你」count: ' + navBadge);
if (!todayLabel.includes('4') && !todayLabel.includes('等你')) throw new Error('Today entry label is wrong: ' + todayLabel);

// ---- 2. Today -> first 等你 thread in ONE click ------------------------------
await page.locator('#todayDialogueEntry').click();
await page.locator('#page-dialogue.active').waitFor({ timeout: 15000 });
await page.locator('#dialogueList .dialogue-row').first().waitFor({ timeout: 15000 });
await page.waitForFunction(() => !!window.dialogueSelectedId && !!document.querySelector('.dialogue-row.is-selected'), null, { timeout: 15000 });
await settle();
const clicksToFirst = 1;
const selectedGroup = await page.evaluate(() => {
  const row = document.querySelector('.dialogue-row.is-selected');
  const group = row ? row.closest('.dialogue-group') : null;
  return group ? group.dataset.group : null;
});
if (selectedGroup !== 'awaiting') throw new Error('one click did not land on a 等你 thread: ' + selectedGroup);
const order = await groupOrder();
if (order.join(',') !== 'awaiting,sela,done') throw new Error('list group order is wrong: ' + order.join(','));
if (await groupCount('awaiting') !== 4) throw new Error('等你 group count is wrong: ' + await groupCount('awaiting'));
if (await groupCount('sela') !== 2) throw new Error('sela group count is wrong: ' + await groupCount('sela'));
if (await groupCount('done') !== 1) throw new Error('done group count is wrong: ' + await groupCount('done'));
const overview = (await page.locator('#dialogueOverview').innerText()).trim();
if (!overview.includes('4') || !overview.includes('2')) throw new Error('dialogue overview counts wrong: ' + overview);
// First sela message states what to decide, then evidence refs; suggestions are buttons.
const firstAsk = (await page.locator('.dialogue-msg--sela .dialogue-msg-text').first().innerText()).trim();
if (!firstAsk) throw new Error('opening sela message is empty');
const refChips = await page.locator('.dialogue-msg--sela .dialogue-msg-refs .dialogue-ref').count();
await shot('01-wide-list-thread.png');

// ---- 3. Suggestion is fill-only, then process ONE thread in <=3 operations --
await page.evaluate(() => {
  window.__dialogueReplyPosts = 0;
  const original = window.fetch;
  window.fetch = function (input, init) {
    const url = typeof input === 'string' ? input : (input && input.url) || '';
    if (/\/api\/inbox\/threads\/[^/]+\/reply$/.test(url)) window.__dialogueReplyPosts += 1;
    return original.apply(this, arguments);
  };
});
await selectThread(threads['dialogue-01']);
const suggestionText = (await page.locator('.dialogue-suggestion').first().innerText()).trim();
await page.locator('.dialogue-suggestion').first().click();               // operation 1
const filled = await page.locator('#dialogueReplyInput').inputValue();
if (filled !== suggestionText) throw new Error('suggestion did not fill the reply box verbatim: ' + JSON.stringify(filled));
if (await page.evaluate(() => window.__dialogueReplyPosts) !== 0) throw new Error('clicking a suggestion sent a reply');
await page.locator('#dialogueReplyInput').press('Enter');                 // operation 2
await toast('sela 已收到').first().waitFor({ timeout: 15000 });
await page.waitForFunction((id) => window.dialogueSelectedId && window.dialogueSelectedId !== id, threads['dialogue-01'], { timeout: 15000 });
const opsToOneThread = 2;
if (await groupCount('awaiting') !== 3) throw new Error('reply did not move the thread out of 等你: ' + await groupCount('awaiting'));
await page.waitForFunction(() => (document.getElementById('dialogueNavCount') || {}).textContent === '3', null, { timeout: 15000 });
await shot('02-wide-after-reply.png');

// ---- 4. System message thread (dialogue-04) ---------------------------------
await selectThread(threads['dialogue-04']);
await page.locator('.dialogue-msg--system').first().waitFor({ timeout: 15000 });
const systemText = (await page.locator('.dialogue-msg--system').first().innerText()).trim();
if (!systemText) throw new Error('system message did not render');

// ---- 5. Attachment upload on dialogue-02 (has a customer ref) ---------------
await selectThread(threads['dialogue-02']);
if (await page.locator('#dialogueAttachInput').isDisabled()) throw new Error('attachment input is disabled on a thread with a customer ref');
await page.locator('#dialogueAttachInput').setInputFiles(samples + '/supported.csv');
await page.locator('.dialogue-composer-attachments .dialogue-attachment').first().waitFor({ timeout: 15000 });
const chipText = (await page.locator('.dialogue-composer-attachments .dialogue-attachment').first().innerText()).trim();
if (!chipText.includes('supported.csv')) throw new Error('uploaded attachment chip is wrong: ' + chipText);
await page.locator('#dialogueReplyInput').fill('附件在这里，请查收。');
await page.locator('.dialogue-send').click();
await toast('sela 已收到').first().waitFor({ timeout: 15000 });
await selectThread(threads['dialogue-02']);
await page.locator('.dialogue-msg--human .dialogue-msg-attachments .dialogue-attachment').first().waitFor({ timeout: 15000 });
const sentAttachment = (await page.locator('.dialogue-msg--human .dialogue-msg-attachments .dialogue-attachment').first().innerText()).trim();
if (!sentAttachment.includes('supported.csv')) throw new Error('sent attachment did not persist: ' + sentAttachment);

// ---- 6. thread_changed conflict on the next reply ---------------------------
await selectThread(threads['dialogue-03']);
const staleRevision = await page.evaluate(() => window.dialogueThread.revision);
// A second writer (another tab) replies with the same revision: the server
// advances the thread, so the UI's in-flight reply must be rejected as stale.
const raced = await page.evaluate(async (payload) => {
  const response = await fetch('/api/inbox/threads/' + payload.id + '/reply', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ text: '（另一处）先回了这条。', seen_revision: payload.revision }),
  });
  return { status: response.status, body: await response.json().catch(() => ({})) };
}, { id: threads['dialogue-03'], revision: staleRevision });
if (raced.status !== 200) throw new Error('second writer could not race the thread: ' + JSON.stringify(raced));
await page.locator('#dialogueReplyInput').fill('我的回复');
await page.locator('.dialogue-send').click();
await page.locator('.dialogue-conflict').waitFor({ timeout: 15000 });
const conflictText = (await page.locator('.dialogue-conflict').innerText()).trim();
if (!conflictText.includes('有新消息，请先看')) throw new Error('conflict prompt text is wrong: ' + conflictText);
await shot('03-conflict.png');
await page.locator('.dialogue-conflict button').click();
await page.waitForFunction(() => !document.querySelector('.dialogue-conflict'), null, { timeout: 15000 });

// ---- 7. Load-failure reason (never「没有对话」) -------------------------------
await page.route('**/api/inbox/threads**', (route) => route.fulfill({
  status: 500, contentType: 'application/json',
  body: JSON.stringify({ error: { code: 'internal_error', message: '对话服务暂时不可用' } }),
}));
await page.evaluate(() => loadDialogue());
const errorState = page.locator('#dialogueList [data-state="error"]');
await errorState.waitFor({ timeout: 15000 });
const errorText = (await errorState.innerText()).trim();
const retryControl = errorState.locator('button').first();
if (!await errorState.locator('button').count()) throw new Error('load failure has no retry control');
const retryName = ((await retryControl.innerText()) || '').trim()
  || (await retryControl.getAttribute('aria-label'))
  || (await retryControl.getAttribute('title'))
  || '';
if (!retryName) throw new Error('load failure retry control has no accessible label');
if (errorText.includes('现在没有对话')) throw new Error('load failure was rendered as an empty state: ' + errorText);
await page.waitForTimeout(250);
await shot('04-error.png');
await page.unroute('**/api/inbox/threads**');

// ---- 8. Clean empty state ---------------------------------------------------
await page.route('**/api/inbox/threads**', (route) => route.fulfill({
  status: 200, contentType: 'application/json',
  body: JSON.stringify({ success: true, threads: [], next_cursor: null, counts: { awaiting_human: 0, awaiting_sela: 0, open: 0 } }),
}));
await page.evaluate(() => loadDialogue());
const emptyState = page.locator('#dialogueList [data-state="empty"]');
await emptyState.waitFor({ timeout: 15000 });
if (!(await emptyState.innerText()).includes('现在没有对话')) throw new Error('empty state text is wrong: ' + await emptyState.innerText());
await shot('05-empty.png');
await page.unroute('**/api/inbox/threads**');

// ---- 9. Responsive widths + mobile split ------------------------------------
await page.reload({ waitUntil: 'domcontentloaded' });
await page.locator('#roomBrand').waitFor({ state: 'visible', timeout: 15000 });
await openDialogue();
const layouts = [];
for (const width of [1280, 768, 390]) {
  await page.setViewportSize({ width, height: 900 });
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  const visible = await page.locator('#page-dialogue.active').isVisible();
  layouts.push({ width, visible, overflow });
  if (!visible) throw new Error('dialogue page not visible at width ' + width);
  if (overflow > 1) throw new Error('dialogue page overflows horizontally at width ' + width + ': ' + overflow);
  if (width === 1280) await shot('06-width-1280.png');
  if (width === 768) await shot('07-width-768.png');
  if (width === 390) {
    await page.evaluate(() => showDialogueList());
    await page.waitForFunction(() => !document.getElementById('page-dialogue').classList.contains('dialogue-thread-open'));
    await shot('08-width-390-list.png');
    await page.locator('.dialogue-row.is-selected').click();
    await page.waitForFunction(() => document.getElementById('page-dialogue').classList.contains('dialogue-thread-open'));
    const composerVisible = await page.locator('#dialogueComposer').isVisible() && await page.locator('#dialogueMessages').isVisible();
    if (!composerVisible) throw new Error('mobile reply box or messages are not visible in the thread view');
    await shot('09-width-390-thread.png');
  }
}
await page.setViewportSize({ width: 1280, height: 900 });

// ---- 10. 200% zoom ----------------------------------------------------------
await page.evaluate(() => { document.body.style.zoom = '2'; });
const zoomVisible = await page.locator('#page-dialogue.active').isVisible() && await page.locator('#dialogueComposer').isVisible();
await page.evaluate(() => { document.body.style.zoom = ''; });
if (!zoomVisible) throw new Error('dialogue unusable at 200% zoom');
await page.evaluate(() => { document.body.style.zoom = '2'; });
await shot('10-zoom-200.png');
await page.evaluate(() => { document.body.style.zoom = ''; });

// ---- 11. Fully keyboard-driven flow (J/K, Enter send, Esc back) -------------
await page.evaluate(() => { document.querySelector('.dialogue-row.is-selected').focus(); });
const beforeJ = await selectedId();
await page.keyboard.press('j');
await page.waitForFunction((id) => window.dialogueSelectedId !== id, beforeJ, { timeout: 15000 });
const afterJ = await selectedId();
await page.waitForFunction((id) => {
  const row = document.querySelector('.dialogue-row[data-dialogue-id="' + id + '"]');
  return !!row && document.activeElement === row;
}, afterJ, { timeout: 15000 });
await page.keyboard.press('k');
await page.waitForFunction((id) => window.dialogueSelectedId === id, beforeJ, { timeout: 15000 });
await page.waitForFunction((id) => {
  const row = document.querySelector('.dialogue-row[data-dialogue-id="' + id + '"]');
  return !!row && document.activeElement === row;
}, beforeJ, { timeout: 15000 });
if (afterJ === beforeJ) throw new Error('J did not move the dialogue selection');
await page.keyboard.press('Enter');
await page.waitForFunction(() => document.activeElement && document.activeElement.id === 'dialogueReplyInput', null, { timeout: 15000 });
const focusedReply = await page.evaluate(() => document.activeElement && document.activeElement.id);
if (focusedReply !== 'dialogueReplyInput') throw new Error('Enter did not focus the reply box: ' + focusedReply);
const keyboardThread = await selectedId();
await page.keyboard.type('键盘回复：收到。');
await page.keyboard.press('Enter');
await toast('sela 已收到').first().waitFor({ timeout: 15000 });
await page.waitForFunction((id) => window.dialogueSelectedId !== id, keyboardThread, { timeout: 15000 });
await page.evaluate(() => { const row = document.querySelector('.dialogue-row.is-selected') || document.querySelector('.dialogue-row'); if (row) row.focus(); });
await page.keyboard.press('Escape');
await page.waitForFunction(() => !document.getElementById('page-dialogue').classList.contains('dialogue-thread-open'), null, { timeout: 15000 });
const listFocused = await page.evaluate(() => !!(document.activeElement && document.activeElement.classList && document.activeElement.classList.contains('dialogue-row')));
if (!listFocused) throw new Error('Esc did not return focus to the conversation list');

// ---- 12. Accessibility basics ----------------------------------------------
await page.locator('.dialogue-row').first().click();
await page.locator('.dialogue-messages').waitFor({ timeout: 15000 });
const a11y = await page.evaluate(() => {
  const messages = document.querySelector('.dialogue-messages');
  const listPane = document.querySelector('.dialogue-list-pane');
  const back = document.querySelector('.dialogue-back');
  const input = document.getElementById('dialogueReplyInput');
  const label = document.querySelector('label[for="dialogueReplyInput"]');
  return {
    log: !!messages && messages.getAttribute('role') === 'log' && messages.getAttribute('aria-live') === 'polite',
    listLabel: !!listPane && !!listPane.getAttribute('aria-label'),
    backLabel: !!back && !!back.getAttribute('aria-label'),
    replyLabelled: !!input && !!label,
    rowRole: !!document.querySelector('.dialogue-row[role="listitem"]'),
  };
});
if (!a11y.log || !a11y.listLabel || !a11y.backLabel || !a11y.replyLabelled || !a11y.rowRole) {
  throw new Error('dialogue accessibility basics missing: ' + JSON.stringify(a11y));
}

const metrics = {
  todayClicksToOneThread: clicksToFirst,
  operationsToProcessOneThread: opsToOneThread,
  awaitingBefore: 4,
  awaitingAfterOneReply: 3,
  autoAdvanced: true,
  suggestionFillsOnly: true,
  keyboardJkEnterEsc: true,
  conflictPrompt: true,
  systemMessage: true,
  attachmentUpload: true,
  errorShowsReason: true,
  emptyState: true,
  layouts,
  a11y,
  openingAskLength: firstAsk.length,
  refChipsOnOpening: refChips,
};
console.log('DIALOGUE METRICS ' + JSON.stringify(metrics));
return metrics;
