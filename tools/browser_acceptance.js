/*
 * Real-browser acceptance program for the Trosa rehearsal service.
 *
 * This program is executed in Node by tools/run_browser_acceptance.cjs inside a
 * freshly launched headless Playwright Chromium -- not by jsdom or a DOM
 * simulator, and not by a shared desktop browser. Keep the flow deliberately
 * user-shaped: login, open Customer, record a communication and dated next
 * step, then verify Today, Inbox and Search through the rendered application.
 *
 * Every run stamps a unique RUN_TAG into the records it creates, so each
 * assertion -- and the Search "exactly one hit" check -- targets this run's own
 * data instead of ambiguous text an earlier run may also have written.
 */

const baseUrl = (typeof process !== 'undefined' && process.env && process.env.TROSA_BROWSER_ACCEPTANCE_URL)
  || 'http://127.0.0.1:18180';

// Unique per run: passed by the launcher, with a defensive fallback.
const runTag = (typeof process !== 'undefined' && process.env && process.env.TROSA_BROWSER_ACCEPTANCE_RUN_TAG)
  || ('BAR' + Date.now().toString(36).toUpperCase());

const customerName = 'Rehearsal Acrylic Co';
const replyText = 'Browser acceptance customer reply — confirm acrylic sheet sample quotation. [' + runTag + ']';
const taskTitle = 'Browser acceptance: send sample quotation [' + runTag + ']';
const completeText = 'Browser acceptance complete follow-up [' + runTag + ']';

function check(condition, message) {
  if (!condition) {
    // Mark it as a product assertion: the driver exits 20 and the release gate
    // never retries it, so a real defect cannot be washed green by a rerun.
    const error = new Error(message);
    error.acceptanceClass = 'assertion';
    throw error;
  }
}

// There is no resident navigation: pages are reached through the summoned
// index layer (brand click), exactly as a user does.
async function gotoPage(name) {
  await page.locator('#roomBrand').click();
  await page.locator('#roomIndex').waitFor({state: 'visible', timeout: 15000});
  await page.locator('#roomIndex [data-page="' + name + '"]').first().click();
}

function localDate() {
  const now = new Date();
  const pad = (value) => String(value).padStart(2, '0');
  return String(now.getFullYear()) + '-' + pad(now.getMonth() + 1) + '-' + pad(now.getDate());
}

const origin = new URL(baseUrl).origin;
const hostname = new URL(baseUrl).hostname;
await context.clearCookies({domain: hostname}).catch(() => {});
// Keep the desktop layout stable regardless of the harness window size: below
// 1025px the room becomes a single column.
await page.setViewportSize({width: 1440, height: 900});
// A fresh Chromium per run starts blank; detach from about:blank before touching
// origin-scoped storage anyway, so a reused context can never leak state.
await page.goto('about:blank', {waitUntil: 'domcontentloaded'}).catch(() => {});
await page.goto(origin + '/?browser_acceptance=1', {waitUntil: 'domcontentloaded'});
// Remove an unrelated app's service worker if this origin was used by another
// local tool before the acceptance run. A service worker can otherwise serve
// stale HTML while the real Trosa server is healthy on the same loopback port.
await page.evaluate(async () => {
  if (navigator.serviceWorker) {
    const registrations = await navigator.serviceWorker.getRegistrations();
    await Promise.all(registrations.map((registration) => registration.unregister()));
  }
  if (typeof caches !== 'undefined') {
    const names = await caches.keys();
    await Promise.all(names.map((name) => caches.delete(name)));
  }
  try { localStorage.clear(); sessionStorage.clear(); } catch (_) {}
});
await page.reload({waitUntil: 'domcontentloaded'});

// A fresh context carries no session; log in only when the overlay asks.
const loginButton = page.locator('#loginUsers [data-user-id="hamid"]');
const dashboard = page.locator('#page-dashboard.active');
// The room shell renders behind the account overlay, so the dashboard being
// visible is not proof of a session: the overlay is.
const needsLogin = await page.locator('#loginOverlay').waitFor({state: 'visible', timeout: 8000}).then(() => true, () => false);
if (needsLogin) {
  await loginButton.waitFor({state: 'visible', timeout: 15000});
  await loginButton.click();
  await page.locator('#loginOverlay').waitFor({state: 'hidden', timeout: 15000});
} else {
  // keep the session
}
await dashboard.waitFor({state: 'visible', timeout: 15000});

// Customer → real customer workspace.
await gotoPage('customers');
await page.locator('#page-customers.active').waitFor({state: 'visible', timeout: 15000});
// The customer list is the ledger (design/rooms/customers.md): select the row,
// then open it with the keyboard path (Enter) the room documents.
const ledgerRow = () => page.locator('#ledgerScroll .ld-ent').filter({hasText: customerName}).first();
const openLedgerRow = async () => {
  await ledgerRow().waitFor({state: 'visible', timeout: 15000});
  await ledgerRow().click();
  await page.keyboard.press('Enter');
};
await openLedgerRow();
let customerModal = page.locator('#customerEditModal.show:visible');
await customerModal.waitFor({state: 'visible', timeout: 15000});
await customerModal.getByRole('button', {name: '记录沟通', exact: true}).click();

// Communication capture → explicit action and date. The body carries this run's
// unique marker so the later Search assertion can require exactly one hit.
const communicationModal = page.locator('.modal-overlay.show:visible').last();
await communicationModal.locator('#inboxReplyContent').fill(replyText);
const today = localDate();
await communicationModal.locator('#inboxReplyDate').fill(today);
await communicationModal.locator('#saveInboxReplyButton').click();
await page.locator('#inboxReplyModal.show:visible').waitFor({state: 'hidden', timeout: 30000});

// The communication dialog is a separate overlay and the SPA caches the initial
// workspace read. Refresh the real page after saving so this assertion proves
// persistence through a new browser document, not a stale in-memory response.
await customerModal.getByRole('button', {name: '关闭'}).first().click();
await customerModal.waitFor({state: 'hidden', timeout: 15000});
await page.reload({waitUntil: 'domcontentloaded'});
await page.locator('#loginOverlay').waitFor({state: 'hidden', timeout: 15000});
await page.locator('#page-dashboard.active').waitFor({state: 'visible', timeout: 15000});
await gotoPage('customers');
await page.locator('#page-customers.active').waitFor({state: 'visible', timeout: 15000});
await openLedgerRow();
customerModal = page.locator('#customerEditModal.show:visible');
await customerModal.waitFor({state: 'visible', timeout: 15000});
await customerModal.getByText('Browser acceptance customer reply', {exact: false}).first()
  .waitFor({state: 'visible', timeout: 30000});

const customerText = await customerModal.innerText();
check(customerText.includes(runTag),
  'saved communication is not visible in the Customer timeline');

// Next step is now scheduled from the customer workspace, not from the
// communication dialog. Create a dated task so Today coverage stays real.
// The action button may read 安排下一步 (no open task) or 调整下一步 (existing
// task from an earlier run); both open the same task modal.
const taskButton = customerModal.locator('#customerTaskActionButton');
await taskButton.waitFor({state: 'visible', timeout: 15000});
await taskButton.click();
const taskModal = page.locator('#customerTaskModal.show:visible');
await taskModal.waitFor({state: 'visible', timeout: 15000});
await taskModal.locator('#customerTaskTitle').fill(taskTitle);
await taskModal.locator('#customerTaskDate').fill(today);
await taskModal.locator('#customerTaskSubmit').click();
await taskModal.waitFor({state: 'hidden', timeout: 15000});

const customerTaskText = await customerModal.innerText();
check(customerTaskText.includes(taskTitle),
  'saved next step is not visible in the Customer workspace');

await customerModal.getByRole('button', {name: '关闭'}).first().click();
await customerModal.waitFor({state: 'hidden', timeout: 15000});

// Today must show the dated action, not just the communication fact. Scope the
// lookup to the Today container: an unscoped getByText can match the same text
// in the customer workspace that is still in the DOM while its modal closes,
// which made this assertion pass (or fail) for the wrong reason.
// The tide renders every due row directly in the table; the old queue drawer
// (#todayQueueToggle / #todayQueue) no longer exists, so the assertions below
// read the row where it is actually drawn. The assertion itself is unchanged.
await gotoPage('dashboard');
await page.locator('#page-dashboard.active').waitFor({state: 'visible', timeout: 15000});
const todayTask = page.locator('#page-dashboard')
  .getByText(taskTitle, {exact: true}).first();
await todayTask.waitFor({state: 'visible', timeout: 15000});
const todayText = await page.locator('#page-dashboard').innerText();
check(todayText.includes(taskTitle),
  'dated next step is missing from Today');

// The 完成这次跟进 modal (#completeModal) must open ready to type from a real
// entry and record the follow-up through its keyboard path. Today's dated action
// is a real reminder, so it is also reachable from the full calendar — the other
// caller of the same dialog. The old dialog called loadDashboard()/loadCalendar()
// and repainted the whole page; this path proves the inline save instead.
await page.locator('#page-dashboard [data-module="calendar_sync"]').first().click();
await page.locator('#page-calendar.active').waitFor({state: 'visible', timeout: 15000});
const calendarToday = page.locator('#calendarGrid .calendar-day.today').first();
await calendarToday.waitFor({state: 'visible', timeout: 15000});
await calendarToday.click();
const calendarRow = page.locator('#calendarDetail .reminder-item')
  .filter({hasText: taskTitle}).first();
await calendarRow.waitFor({state: 'visible', timeout: 15000});
await calendarRow.getByRole('button', {name: '记录沟通'}).click();
const completeModal = page.locator('#completeModal.show:visible');
await completeModal.waitFor({state: 'visible', timeout: 15000});
// The modal schedules its focus one animation frame after it is shown
// (openModal → requestAnimationFrame → focusFirstModalControl) and the modal
// itself opens after an async fetch, so a single instantaneous read races that
// frame. Poll for the caret instead of sampling once.
let completeFocusReady = true;
try {
  await page.waitForFunction(
    () => document.activeElement && document.activeElement.id === 'completeResult',
    null,
    {timeout: 15000}
  );
} catch (focusError) {
  completeFocusReady = false;
}
check(completeFocusReady,
  'the complete modal did not put the caret in the capture textarea');

await completeModal.locator('#completeResult').fill(completeText);
// 沟通方式 now lives below the textarea as a compact custom menu; ArrowDown opens
// it and Enter selects the next channel (WhatsApp → email).
await completeModal.locator('#completeActivityTrigger').click();
await page.keyboard.press('ArrowDown');
await page.keyboard.press('Enter');
const completeChannel = await page.evaluate(
  () => document.getElementById('completeActivityType').value
);
check(completeChannel === 'email',
  'the complete modal channel menu did not switch with the keyboard');

await completeModal.locator('#completeResult').click();
await page.keyboard.press('Control+Enter');
await completeModal.waitFor({state: 'hidden', timeout: 30000});
const completeToast = page.locator('#toastContainer .toast').first();
await completeToast.waitFor({state: 'visible', timeout: 15000});
check((await completeToast.innerText()).includes('已记录'),
  'completing the follow-up did not confirm inline with 已记录');
const calendarAfter = await page.locator('#calendarDetail').innerText();
check(!calendarAfter.includes(taskTitle),
  'the completed follow-up stayed in the calendar after an inline save');

// Inbox must remain a real rendered workspace with pending judgement items.
await gotoPage('inbox');
await page.locator('#page-inbox.active').waitFor({state: 'visible', timeout: 15000});
const inboxText = await page.locator('#page-inbox').innerText();
check(inboxText.includes('Inbox'), 'Inbox page did not render');
check(await page.locator('#inboxList > *').count() > 0, 'Inbox rendered no items');

// Search input (in the summoned index layer) → live preview → Enter → exactly one
// matching Customer result. Ctrl/Cmd-K is the real user path for reaching it. The
// query is this run's unique RUN_TAG, so the result must be exactly the record
// this run just wrote -- not a stale fact from an earlier run.
await page.keyboard.press('Control+k');
await page.locator('#roomIndex').waitFor({state: 'visible', timeout: 15000});
const search = page.locator('#globalPageSearch');
await search.fill(runTag);
await page.locator('#globalSearchPreview.show:visible').waitFor({state: 'visible', timeout: 15000});
const previewText = await page.locator('#globalSearchPreview').innerText();
check(previewText.includes(runTag), 'global Search preview has no fact for this run marker');
await search.press('Enter');
await page.locator('#page-customers.active').waitFor({state: 'visible', timeout: 15000});
// Wait for the fresh, post-refresh snapshot: the row must carry the 命中
// highlight and this run's marker. Exactly one row may match.
const searchHits = page.locator('#ledgerScroll .ld-ent')
  .filter({hasText: '命中'})
  .filter({hasText: runTag});
await searchHits.first().waitFor({state: 'visible', timeout: 30000});
const searchHitCount = await searchHits.count();
check(searchHitCount === 1,
  'Search for this run marker matched ' + searchHitCount + ' highlighted rows; expected exactly one');
const searchHit = searchHits.first();
check((await searchHit.innerText()).includes(customerName),
  'the single Search hit is not the customer under test');
const searchHitText = await searchHit.locator('.ld-sn').innerText();
check(searchHitText.includes(runTag),
  'Search Enter did not surface this run marker in the matched snippet (snippet='
    + JSON.stringify(searchHitText) + ')');

return {
  browser: await page.evaluate(() => navigator.userAgent),
  baseUrl: origin,
  activePage: await page.locator('.page-section.active').getAttribute('id'),
  runTag: runTag,
  customer: customerName,
  communicationVisible: customerText.includes(runTag),
  nextStepVisibleInCustomer: customerTaskText.includes(taskTitle),
  nextStepVisibleInToday: todayText.includes(taskTitle),
  inboxItems: await page.locator('#inboxList > *').count(),
  searchMatched: searchHitText.includes(runTag),
};
