// Regression harness: every dialog is a real dialog.
//
// U-06 of the 2026-09-30 UI review found that the modals only focused their
// first control: there was no aria-modal, no role="dialog" on the card, the page
// behind stayed clickable/focusable, and Tab walked out of the dialog into the
// header. This harness pins the fix:
//   * every `.modal` is role="dialog" + aria-modal with a labelledby that
//     resolves to real, non-empty text;
//   * opening a dialog makes every other body child inert, including a lower
//     dialog in a nested stack, and closing restores the stack;
//   * Tab / Shift+Tab cycle inside the topmost dialog instead of escaping;
//   * ephemeral modals (showCustomModal) get the same contract.
//
// Run standalone (needs node + jsdom):
//   node tests/support/modal_a11y_check.cjs
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const root = path.resolve(__dirname, '..', '..');
const staticDir = path.join(root, 'app', 'static');

function loadJsdom() {
  const candidates = [path.join(root, 'browser-extension', 'node_modules', 'jsdom'), 'jsdom'];
  for (const candidate of candidates) {
    try {
      return require(candidate);
    } catch (error) {
      if (error.code !== 'MODULE_NOT_FOUND') throw error;
    }
  }
  console.error('jsdom 未安装：请在 browser-extension 目录执行 npm install');
  process.exit(2);
}

const { JSDOM } = loadJsdom();
const html = fs.readFileSync(path.join(staticDir, 'index.html'), 'utf8').replace(/\{\{VERSION\}\}/g, 'test');
const script = fs.readFileSync(path.join(staticDir, 'app.js'), 'utf8');

const dom = new JSDOM(html, { url: 'http://localhost/', runScripts: 'outside-only', pretendToBeVisual: true });
const win = dom.window;
const doc = win.document;
win.fetch = async () => ({ ok: true, status: 200, json: async () => ({}) });
win.eval(script);
win._motionReduced = true; // make close() synchronous for assertions

let failures = 0;
function check(label, body) {
  try {
    body();
    console.log('  ok   - ' + label);
  } catch (error) {
    failures += 1;
    console.log('  FAIL - ' + label + '\n         ' + error.message);
  }
}

function resetModals() {
  Array.from(doc.querySelectorAll('.modal-overlay')).forEach((overlay) => win.closeModal(overlay.id, true));
  win._pendingCustomerModalClose = '';
}

function pressTab(shiftKey) {
  doc.dispatchEvent(new win.KeyboardEvent('keydown', { key: 'Tab', shiftKey: !!shiftKey, bubbles: true, cancelable: true }));
}

const labelText = (card) => {
  const ids = (card.getAttribute('aria-labelledby') || '').split(/\s+/).filter(Boolean);
  return ids.map((id) => {
    const el = doc.getElementById(id);
    return el ? (el.textContent || '').trim() : '';
  }).join(' ').trim();
};

console.log('scenario: every dialog card declares itself a dialog');
Array.from(doc.querySelectorAll('.modal-overlay > .modal')).forEach((card) => {
  const overlay = card.parentElement;
  check(overlay.id + ' has role=dialog / aria-modal / resolvable label', () => {
    assert.equal(card.getAttribute('role'), 'dialog');
    assert.equal(card.getAttribute('aria-modal'), 'true');
    const labeledby = card.getAttribute('aria-labelledby');
    assert.ok(labeledby, 'aria-labelledby missing');
    labeledby.split(/\s+/).filter(Boolean).forEach((id) => {
      assert.ok(doc.getElementById(id), 'aria-labelledby points at missing #' + id);
    });
    assert.ok(labelText(card).length > 0, 'accessible name is empty');
  });
});
check('the app-dialog overlay does not nest a second dialog role', () => {
  assert.equal(doc.getElementById('appDialogModal').getAttribute('role'), null);
  const card = doc.querySelector('#appDialogModal > .modal');
  assert.equal(card.getAttribute('role'), 'dialog');
});

console.log('scenario: opening a dialog makes the rest of the page inert');
check('body children except the top overlay become inert', () => {
  resetModals();
  win.openModal('completeModal');
  const top = doc.getElementById('completeModal');
  assert.equal(top.hasAttribute('inert'), false, 'top overlay must stay interactive');
  assert.equal(doc.getElementById('appLayout').hasAttribute('inert'), true);
  assert.equal(doc.getElementById('toastContainer').hasAttribute('inert'), true);
  assert.equal(doc.getElementById('customerEditModal').hasAttribute('inert'), true, 'a closed overlay is inert too');
});
check('a lower dialog in a nested stack is inert, the top one is not', () => {
  resetModals();
  win.openModal('customerEditModal');
  win.openModal('customerTaskModal');
  assert.equal(doc.getElementById('customerEditModal').hasAttribute('inert'), true);
  assert.equal(doc.getElementById('customerTaskModal').hasAttribute('inert'), false);
  assert.equal(doc.getElementById('appLayout').hasAttribute('inert'), true);
});
check('closing restores the stack (no leaked inert)', () => {
  resetModals();
  Array.from(doc.body.children).forEach((child) => {
    assert.equal(child.hasAttribute('inert'), false, child.id || child.className || child.tagName);
  });
});
check('closing the top dialog re-enables the one it covered', () => {
  resetModals();
  win.openModal('customerEditModal');
  win.openModal('customerTaskModal');
  win.closeModal('customerTaskModal', true);
  assert.equal(doc.getElementById('customerEditModal').hasAttribute('inert'), false);
  assert.equal(doc.getElementById('appLayout').hasAttribute('inert'), true);
  resetModals();
});

console.log('scenario: Tab cycles inside the topmost dialog');
check('Tab from the last control wraps to the first', () => {
  resetModals();
  win.openModal('completeModal');
  const overlay = doc.getElementById('completeModal');
  const focusables = win.modalFocusableElements(overlay);
  assert.ok(focusables.length > 1, 'expected several controls');
  const first = focusables[0];
  const last = focusables[focusables.length - 1];
  last.focus();
  assert.equal(doc.activeElement, last);
  pressTab(false);
  assert.equal(doc.activeElement, first);
});
check('Shift+Tab from the first control wraps to the last', () => {
  const overlay = doc.getElementById('completeModal');
  const focusables = win.modalFocusableElements(overlay);
  const first = focusables[0];
  const last = focusables[focusables.length - 1];
  first.focus();
  pressTab(true);
  assert.equal(doc.activeElement, last);
});
check('Tab from outside the dialog pulls focus back in', () => {
  const overlay = doc.getElementById('completeModal');
  const first = win.modalFocusableElements(overlay)[0];
  doc.body.focus();
  pressTab(false);
  assert.equal(doc.activeElement, first);
});
check('Tab is left alone when no dialog is open', () => {
  resetModals();
  assert.equal(win.modalTopOverlay(), null);
  const before = doc.activeElement;
  pressTab(false);
  assert.equal(doc.activeElement, before);
});

console.log('scenario: ephemeral modals share the contract');
check('showCustomModal renders a labelled dialog and inerts the page', () => {
  resetModals();
  win.showCustomModal('日历订阅说明', '<p>说明</p>');
  const overlay = doc.querySelector('.modal-overlay.ephemeral-modal');
  assert.ok(overlay, 'ephemeral overlay missing');
  const card = overlay.querySelector('.modal');
  assert.equal(card.getAttribute('role'), 'dialog');
  assert.equal(card.getAttribute('aria-modal'), 'true');
  assert.equal(labelText(card), '日历订阅说明');
  assert.equal(doc.getElementById('appLayout').hasAttribute('inert'), true);
});
check('closing an ephemeral modal removes it and restores the page', () => {
  const overlay = doc.querySelector('.modal-overlay.ephemeral-modal');
  const returnTarget = doc.getElementById('roomBrand');
  returnTarget.focus();
  overlay._returnFocus = returnTarget;
  win.closeEphemeralModal(overlay);
  assert.equal(doc.querySelector('.modal-overlay.ephemeral-modal'), null);
  assert.equal(doc.getElementById('appLayout').hasAttribute('inert'), false);
  assert.equal(doc.activeElement, returnTarget);
});

if (failures) {
  console.error('\nmodal a11y regression: ' + failures + ' check(s) failed');
  process.exit(1);
}
console.log('\nmodal a11y regression: OK');
process.exit(0);
