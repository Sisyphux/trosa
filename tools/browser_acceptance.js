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

function localDate() {
  const now = new Date();
  const pad = (value) => String(value).padStart(2, '0');
  return String(now.getFullYear()) + '-' + pad(now.getMonth() + 1) + '-' + pad(now.getDate());
}

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

const origin = new URL(baseUrl).origin;
const hostname = new URL(baseUrl).hostname;
await context.clearCookies({domain: hostname}).catch(() => {});
await page.goto(origin + '/?browser_acceptance=1', {waitUntil: 'domcontentloaded'});
await page.locator('#loginUsers [data-user-id="hamid"]').waitFor({state: 'visible', timeout: 15000});

await page.locator('#loginUsers [data-user-id="hamid"]').click();
await page.locator('#loginOverlay').waitFor({state: 'hidden', timeout: 15000});
await page.locator('#page-dashboard.active').waitFor({state: 'visible', timeout: 15000});

// Customer → real customer workspace.
await page.locator('[data-page="customers"]').first().click();
await page.locator('#page-customers.active').waitFor({state: 'visible', timeout: 15000});
await page.getByRole('button', {name: 'Rehearsal Acrylic Co', exact: true}).click();
let customerModal = page.locator('#customerEditModal.show:visible');
await customerModal.waitFor({state: 'visible', timeout: 15000});
await customerModal.getByRole('button', {name: '记录沟通', exact: true}).click();

// Communication capture → explicit action and date.
const communicationModal = page.locator('.modal-overlay.show:visible').last();
await communicationModal.locator('#inboxReplyContent').fill(
  'Browser acceptance customer reply — confirm acrylic sheet sample quotation.'
);
const today = localDate();
const nextToggle = communicationModal.locator('#inboxReplyHasNext');
if (!(await nextToggle.isChecked())) await nextToggle.check();
await communicationModal.locator('#inboxReplyNextTask').fill('Browser acceptance: send sample quotation');
await communicationModal.locator('#inboxReplyDate').fill(today);
await communicationModal.locator('#inboxReplyNextDate').fill(today);
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
await page.locator('[data-page="customers"]').first().click();
await page.locator('#page-customers.active').waitFor({state: 'visible', timeout: 15000});
await page.getByRole('button', {name: 'Rehearsal Acrylic Co', exact: true})
  .waitFor({state: 'visible', timeout: 15000});
await page.getByRole('button', {name: 'Rehearsal Acrylic Co', exact: true}).click();
customerModal = page.locator('#customerEditModal.show:visible');
await customerModal.waitFor({state: 'visible', timeout: 15000});
await customerModal.getByText('Browser acceptance customer reply', {exact: false}).first()
  .waitFor({state: 'visible', timeout: 30000});

const customerText = await customerModal.innerText();
check(customerText.includes('Browser acceptance customer reply'),
  'saved communication is not visible in the Customer timeline');
check(customerText.includes('Browser acceptance: send sample quotation'),
  'saved next step is not visible in the Customer workspace');

await customerModal.getByRole('button', {name: '关闭'}).first().click();

// Today must show the dated action, not just the communication fact.
await page.locator('[data-page="dashboard"]').first().click();
await page.locator('#page-dashboard.active').waitFor({state: 'visible', timeout: 15000});
await page.getByText('Browser acceptance: send sample quotation', {exact: true}).first()
  .waitFor({state: 'visible', timeout: 15000});
const todayText = await page.locator('#page-dashboard').innerText();
check(todayText.includes('Browser acceptance: send sample quotation'),
  'dated next step is missing from Today');

// Inbox must remain a real rendered workspace with pending judgement items.
await page.locator('[data-page="inbox"]').first().click();
await page.locator('#page-inbox.active').waitFor({state: 'visible', timeout: 15000});
const inboxText = await page.locator('#page-inbox').innerText();
check(inboxText.includes('Inbox'), 'Inbox page did not render');
check(await page.locator('#inboxList > *').count() > 0, 'Inbox rendered no items');

// Search input → live preview → Enter → matching Customer result.
const search = page.locator('#globalPageSearch');
await search.fill('Browser acceptance');
await page.locator('#globalSearchPreview.show:visible').waitFor({state: 'visible', timeout: 15000});
const previewText = await page.locator('#globalSearchPreview').innerText();
check(previewText.includes('Browser acceptance'), 'global Search preview has no matching fact');
await search.press('Enter');
await page.locator('#page-customers.active').waitFor({state: 'visible', timeout: 15000});
const searchMatch = page.locator('#customerTableBody .customer-match-context')
  .filter({hasText: 'Browser acceptance customer reply'}).first();
await searchMatch.waitFor({state: 'visible', timeout: 30000});
const searchResultText = await page.locator('#customerTableBody').innerText();
check(searchResultText.includes('Browser acceptance customer reply'),
  'Search Enter did not show the matching communication');

return {
  browser: await page.evaluate(() => navigator.userAgent),
  baseUrl: origin,
  activePage: await page.locator('.page-section.active').getAttribute('id'),
  customer: 'Rehearsal Acrylic Co',
  communicationVisible: customerText.includes('Browser acceptance customer reply'),
  nextStepVisibleInCustomer: customerText.includes('Browser acceptance: send sample quotation'),
  nextStepVisibleInToday: todayText.includes('Browser acceptance: send sample quotation'),
  inboxItems: await page.locator('#inboxList > *').count(),
  searchMatched: searchResultText.includes('Browser acceptance customer reply'),
};
