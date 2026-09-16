// Regression harness: every modal must keep an exit.
//
// Every `.modal-overlay` covers the whole viewport and shares one z-index, so the
// browser paints and hit-tests open overlays in DOM order: only the last open
// overlay in the body can be clicked. When the unsaved-changes prompt was shown
// with `classList.add('show')` while a composer defined later in `index.html`
// (安排下一步 / 记录沟通 / 批量操作) stayed open, the prompt was painted behind the
// composer: the X, 取消 and backdrop all re-triggered the guard, Escape hit the
// composer again, and the only way out was to actually create the next step.
//
// Run standalone (needs node + jsdom):
//   node tests/support/modal_exit_check.cjs
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

// The last shown overlay in body order is the one painted on top and clicked.
function topmostOpenOverlay() {
  const overlays = Array.from(doc.body.querySelectorAll('.modal-overlay')).filter((el) => el.classList.contains('show'));
  return overlays[overlays.length - 1] || null;
}

function pressEscape() {
  doc.dispatchEvent(new win.KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
}

// Force everything closed so one scenario cannot leak into the next one.
function resetModals() {
  Array.from(doc.querySelectorAll('.modal-overlay.show')).forEach((overlay) => win.closeModal(overlay.id, true));
  Array.from(doc.querySelectorAll('.modal-overlay.is-closing')).forEach((overlay) => overlay.classList.remove('is-closing'));
  win._pendingCustomerModalClose = '';
}

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

console.log('scenario: 安排下一步 composer has unsaved text and the user wants out');
check('close is intercepted by the unsaved-changes guard instead of silently dropping the text', () => {
  resetModals();
  win.openModal('customerEditModal');
  win.openCustomerTaskModal();
  doc.getElementById('customerTaskTitle').value = '确认样品测试结果';
  doc.getElementById('customerTaskDate').value = '2026-10-01';
  assert.equal(win.closeModal('customerTaskModal'), false);
  assert.ok(doc.getElementById('customerTaskModal').classList.contains('show'));
  assert.ok(doc.getElementById('unsavedChangesModal').classList.contains('show'));
});
check('the prompt is the topmost overlay, so 继续编辑 / 放弃修改 are clickable', () => {
  assert.equal(topmostOpenOverlay().id, 'unsavedChangesModal');
});
check('Escape dismisses the prompt instead of re-guarding the composer', () => {
  pressEscape();
  assert.equal(doc.getElementById('unsavedChangesModal').classList.contains('show'), false);
  assert.equal(win._pendingCustomerModalClose, '');
  assert.ok(doc.getElementById('customerTaskModal').classList.contains('show'));
});
check('放弃修改 closes both the prompt and the composer', () => {
  win.closeModal('customerTaskModal');
  assert.equal(topmostOpenOverlay().id, 'unsavedChangesModal');
  win.discardCustomerFormChanges();
  assert.equal(doc.getElementById('unsavedChangesModal').classList.contains('show'), false);
  assert.equal(doc.getElementById('customerTaskModal').classList.contains('show'), false);
  assert.equal(win._pendingCustomerModalClose, '');
});
check('保存并退出 keeps using the composer save handler', () => {
  win.openCustomerTaskModal();
  doc.getElementById('customerTaskTitle').value = '再次测试';
  doc.getElementById('customerTaskDate').value = '2026-10-02';
  win.closeModal('customerTaskModal');
  assert.equal(doc.getElementById('unsavedSaveButton').style.display, '');
  win.savePendingCustomerForm();
  assert.equal(win._pendingCustomerModalClose, '');
  assert.equal(doc.getElementById('unsavedChangesModal').classList.contains('show'), false);
});
check('an untouched composer still closes immediately', () => {
  resetModals();
  win.openCustomerTaskModal();
  assert.equal(win.closeModal('customerTaskModal'), true);
  assert.equal(doc.getElementById('unsavedChangesModal').classList.contains('show'), false);
});

console.log('scenario: the prompt also stays usable over other late-DOM modals');
check('customer workspace modal', () => {
  resetModals();
  win.openModal('customerEditModal');
  doc.getElementById('editName').value = '改名';
  assert.equal(win.closeModal('customerEditModal'), false);
  assert.equal(topmostOpenOverlay().id, 'unsavedChangesModal');
  win.continueEditingCustomerForm();
  assert.equal(doc.getElementById('unsavedChangesModal').classList.contains('show'), false);
});
check('communication composer', () => {
  resetModals();
  win.openModal('inboxReplyModal');
  doc.getElementById('inboxReplyContent').value = '客户回复';
  assert.equal(win.closeModal('inboxReplyModal'), false);
  assert.equal(topmostOpenOverlay().id, 'unsavedChangesModal');
  win.discardCustomerFormChanges();
  assert.equal(doc.getElementById('inboxReplyModal').classList.contains('show'), false);
});
check('repeated exit attempts keep the prompt on top', () => {
  resetModals();
  win.openModal('customerEditModal');
  win.openCustomerTaskModal();
  doc.getElementById('customerTaskTitle').value = '第三次测试';
  assert.equal(win.closeModal('customerTaskModal'), false);
  win.continueEditingCustomerForm();
  doc.getElementById('customerTaskTitle').value = '第三次测试-修改';
  assert.equal(win.closeModal('customerTaskModal'), false);
  assert.equal(topmostOpenOverlay().id, 'unsavedChangesModal');
  win.discardCustomerFormChanges();
  assert.equal(doc.getElementById('customerTaskModal').classList.contains('show'), false);
  assert.ok(doc.getElementById('customerEditModal').classList.contains('show'));
  resetModals();
});

if (failures) {
  console.error('\nmodal exit regression: ' + failures + ' check(s) failed');
  process.exit(1);
}
console.log('\nmodal exit regression: OK');
process.exit(0);
