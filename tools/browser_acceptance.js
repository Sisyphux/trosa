/*
 * Real-browser acceptance program for the Trosa rehearsal service.
 *
 * This file is executed by the Tabbit Browser CLI, not by jsdom or a DOM
 * simulator. Keep the flow deliberately user-shaped: login, open Customer,
 * record a communication and dated next step, then verify Today, Inbox and
 * Search through the rendered application.
 */

const baseUrl = (typeof process !== 'undefined' && process.env && process.env.TROSA_BROWSER_ACCEPTANCE_URL)
  || 'http://127.0.0.1:18180';

function check(condition, message) {
  if (!condition) throw new Error(message);
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
// Keep the desktop layout stable regardless of the Tabbit window size: below
// 1025px the room becomes a single column.
await page.setViewportSize({width: 1440, height: 900});
// A reused Tabbit task can still sit on a dead rehearsal port from an earlier
// run. Detach from it before touching origin-scoped storage.
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

// A reused Tabbit task may already carry a session; only log in when asked.
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
const ledgerRow = () => page.locator('#ledgerScroll .ld-ent').filter({hasText: 'Rehearsal Acrylic Co'}).first();
const openLedgerRow = async () => {
  await ledgerRow().waitFor({state: 'visible', timeout: 15000});
  await ledgerRow().click();
  await page.keyboard.press('Enter');
};
await openLedgerRow();
let customerModal = page.locator('#customerEditModal.show:visible');
await customerModal.waitFor({state: 'visible', timeout: 15000});
await customerModal.getByRole('button', {name: '记录沟通', exact: true}).click();

// Communication capture → explicit action and date.
const communicationModal = page.locator('.modal-overlay.show:visible').last();
await communicationModal.locator('#inboxReplyContent').fill(
  'Browser acceptance customer reply — confirm acrylic sheet sample quotation.'
);
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
check(customerText.includes('Browser acceptance customer reply'),
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
await taskModal.locator('#customerTaskTitle').fill('Browser acceptance: send sample quotation');
await taskModal.locator('#customerTaskDate').fill(today);
await taskModal.locator('#customerTaskSubmit').click();
await taskModal.waitFor({state: 'hidden', timeout: 15000});

const customerTaskText = await customerModal.innerText();
check(customerTaskText.includes('Browser acceptance: send sample quotation'),
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
  .getByText('Browser acceptance: send sample quotation', {exact: true}).first();
await todayTask.waitFor({state: 'visible', timeout: 15000});
const todayText = await page.locator('#page-dashboard').innerText();
check(todayText.includes('Browser acceptance: send sample quotation'),
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
  .filter({hasText: 'Browser acceptance: send sample quotation'}).first();
await calendarRow.waitFor({state: 'visible', timeout: 15000});
await calendarRow.getByRole('button', {name: '记录跟进'}).click();
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

await completeModal.locator('#completeResult').fill('Browser acceptance complete follow-up');
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
check(!calendarAfter.includes('Browser acceptance: send sample quotation'),
  'the completed follow-up stayed in the calendar after an inline save');

// Inbox must remain a real rendered workspace with pending judgement items.
await gotoPage('inbox');
await page.locator('#page-inbox.active').waitFor({state: 'visible', timeout: 15000});
const inboxText = await page.locator('#page-inbox').innerText();
check(inboxText.includes('Inbox'), 'Inbox page did not render');
check(await page.locator('#inboxList > *').count() > 0, 'Inbox rendered no items');

// Search input (in the summoned index layer) → live preview → Enter → matching
// Customer result. Ctrl/Cmd-K is the real user path for reaching it.
await page.keyboard.press('Control+k');
await page.locator('#roomIndex').waitFor({state: 'visible', timeout: 15000});
const search = page.locator('#globalPageSearch');
await search.fill('Browser acceptance');
await page.locator('#globalSearchPreview.show:visible').waitFor({state: 'visible', timeout: 15000});
const previewText = await page.locator('#globalSearchPreview').innerText();
check(previewText.includes('Browser acceptance'), 'global Search preview has no matching fact');
await search.press('Enter');
await page.locator('#page-customers.active').waitFor({state: 'visible', timeout: 15000});
// The product intentionally surfaces exactly one best search match per customer
// (the newest interaction whose content matches the query), and the ledger keeps
// the previous snapshot on screen while it refetches. After the complete-modal
// step above, the newest matching fact for the customer under test is
// "Browser acceptance complete follow-up", so asserting on the earlier reply text
// is ambiguous. Assert the search outcome instead: wait for the customer's row to
// render with a 命中 highlight (the fresh, post-refresh snapshot) and check that
// the highlight references the query.
const searchHit = page.locator('#ledgerScroll .ld-ent')
  .filter({hasText: 'Rehearsal Acrylic Co'})
  .filter({hasText: '命中'})
  .first();
await searchHit.waitFor({state: 'visible', timeout: 30000});
const searchHitText = await searchHit.locator('.ld-sn').innerText();
check(searchHitText.includes('Browser acceptance'),
  'Search Enter did not highlight a matching fact for the query (snippet='
    + JSON.stringify(searchHitText) + ')');

return {
  browser: await page.evaluate(() => navigator.userAgent),
  baseUrl: origin,
  activePage: await page.locator('.page-section.active').getAttribute('id'),
  customer: 'Rehearsal Acrylic Co',
  communicationVisible: customerText.includes('Browser acceptance customer reply'),
  nextStepVisibleInCustomer: customerTaskText.includes('Browser acceptance: send sample quotation'),
  nextStepVisibleInToday: todayText.includes('Browser acceptance: send sample quotation'),
  inboxItems: await page.locator('#inboxList > *').count(),
  searchMatched: searchHitText.includes('Browser acceptance'),
};
