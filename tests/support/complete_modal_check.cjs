// Regression harness: 完成这次跟进 (#completeModal) capture key path.
//
// The modal was reworked to open ready to type, to expose the channel as a
// compact custom menu, to validate inline instead of by toast, and to update the
// source list locally instead of reloading the whole page. These checks pin the
// behaviour that is easy to break on the next edit:
//   * focus lands on the capture textarea when the modal opens
//   * the channel menu keeps writing to #completeActivityType (7 unchanged values)
//   * date choices show real dates; the primary label follows the next step
//   * empty submit is an inline error, never a request
//   * a successful submit closes the modal, sends exactly one PUT and toasts
//   * a failed submit keeps the modal open with the typed text preserved
//
// Run standalone (needs node + jsdom):
//   node tests/support/complete_modal_check.cjs
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

let putCalls = [];
let putMode = 'ok';
win.matchMedia = win.matchMedia || function () {
  return { matches: false, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {} };
};
win.fetch = async (url, options) => {
  const method = String((options && options.method) || 'GET').toUpperCase();
  if (method === 'PUT' && String(url).indexOf('/api/reminders/') === 0) {
    putCalls.push({ url: String(url), body: options.body });
    if (putMode === 'fail') {
      const error = new Error('network down');
      error.name = 'TypeError';
      throw error;
    }
    return { ok: true, status: 200, json: async () => ({ task_id: 77, attention: '' }) };
  }
  return { ok: true, status: 200, json: async () => ({}) };
};
win.eval(script);

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

const tick = () => new Promise((resolve) => win.setTimeout(resolve, 0));
const raf = () => new Promise((resolve) => win.requestAnimationFrame(() => resolve()));
function keyEvent(key, extra) {
  return Object.assign({ key: key, preventDefault() {}, stopPropagation() {} }, extra || {});
}

function openFilled(overrides) {
  win.fillCompleteModal(Object.assign({
    id: 5,
    customer_id: 9,
    customer_name: 'Rehearsal Acrylic Co',
    task_title: '寄样跟进'
  }, overrides || {}));
}

(async () => {
  console.log('scenario: 打开即输入 / 顶部摘要');
  await check('the modal asks to focus the capture textarea', () => {
    assert.equal(doc.getElementById('completeModal').getAttribute('data-initial-focus'), '#completeResult');
  });
  await check('focus lands on the textarea once the modal settles', async () => {
    openFilled();
    await raf();
    assert.equal(doc.activeElement && doc.activeElement.id, 'completeResult');
    assert.ok(doc.getElementById('completeModal').classList.contains('show'));
  });
  await check('customer and task share one summary line', () => {
    openFilled();
    assert.equal(doc.getElementById('completeCustomerName').textContent, 'Rehearsal Acrylic Co');
    assert.equal(doc.getElementById('completeContent').textContent, '寄样跟进');
  });

  console.log('scenario: 沟通方式菜单');
  await check('the channel menu still writes the 7 legacy values into #completeActivityType', () => {
    openFilled();
    const values = Array.from(doc.querySelectorAll('#completeActivityMenuList .ui-menu-item'))
      .map((item) => item.getAttribute('data-value'));
    assert.deepEqual(values, ['whatsapp', 'email', 'phone', 'meeting', 'quote', 'sample', 'follow_up']);
    assert.equal(doc.getElementById('completeActivityType').value, 'whatsapp');
  });
  await check('keyboard: ArrowDown then Enter picks the next channel and closes the menu', () => {
    openFilled();
    win.uiMenuToggle('completeActivity');
    const list = doc.getElementById('completeActivityMenuList');
    assert.equal(list.hidden, false);
    win.uiMenuListKeydown(keyEvent('ArrowDown'), 'completeActivity');
    const active = list.querySelector('.ui-menu-item.is-active');
    assert.ok(active, 'no highlighted option after ArrowDown');
    win.uiMenuListKeydown(keyEvent('Enter'), 'completeActivity');
    assert.equal(doc.getElementById('completeActivityType').value, 'email');
    assert.equal(list.hidden, true);
  });
  await check('Escape inside the menu closes the menu, not the modal', () => {
    openFilled();
    win.uiMenuToggle('completeActivity');
    win.uiMenuListKeydown(keyEvent('Escape'), 'completeActivity');
    assert.equal(doc.getElementById('completeActivityMenuList').hidden, true);
    assert.ok(doc.getElementById('completeModal').classList.contains('show'));
  });
  await check('changing only the channel does not count as unsaved text', () => {
    openFilled();
    win.uiMenuSetValue('completeActivity', 'phone');
    assert.equal(win.closeModal('completeModal'), true);
  });

  console.log('scenario: AI 按钮与日期');
  await check('the AI button is disabled while the textarea is empty and enables with text', () => {
    openFilled();
    const button = doc.getElementById('completeAiBtn');
    assert.equal(button.disabled, true);
    doc.getElementById('completeResult').value = '客户回复';
    win.onCompleteResultInput();
    assert.equal(button.disabled, false);
  });
  await check('date choices carry real dates and the custom button has a plain label', () => {
    openFilled();
    const seven = doc.querySelector('#completeDateChoices .date-choice[data-days="7"]');
    const fifteen = doc.querySelector('#completeDateChoices .date-choice[data-days="15"]');
    win.setCompleteNextDate(15, fifteen);
    assert.match(fifteen.textContent, /^15 天 · \d+月\d+日$/);
    win.setCompleteNextDate(7, seven);
    assert.match(seven.textContent, /^7 天 · \d+月\d+日$/);
    assert.equal(doc.getElementById('completeCustomDateBtn').textContent, '选择日期');
  });
  await check('checking 安排下一步 focuses the task input and prefills the AI suggestion', () => {
    openFilled();
    win._communicationAnalyses.complete = { suggested_next_action: '发送再生料报价' };
    doc.getElementById('completeHasNext').checked = true;
    win.toggleCompleteNext();
    assert.equal(doc.getElementById('completeNextSection').hidden, false);
    assert.equal(doc.getElementById('completeNextTask').value, '发送再生料报价');
    assert.equal(doc.activeElement && doc.activeElement.id, 'completeNextTask');
    assert.match(doc.getElementById('completeNextFollow').value, /^\d{4}-\d{2}-\d{2}$/);
  });
  await check('the primary label reads 完成跟进 then 完成并安排下一步 · M月D日', () => {
    openFilled();
    assert.equal(doc.getElementById('completeSubmitBtn').textContent, '完成跟进');
    doc.getElementById('completeResult').value = '客户确认';
    win.onCompleteResultInput();
    doc.getElementById('completeHasNext').checked = true;
    win.toggleCompleteNext();
    doc.getElementById('completeNextTask').value = '发送报价';
    win.updateCompleteSaveLabel();
    assert.match(doc.getElementById('completeSubmitBtn').textContent, /^完成并安排下一步 · \d+月\d+日$/);
  });

  console.log('scenario: 提交校验与反馈');
  await check('empty submit shows an inline error, focuses the textarea and sends nothing', () => {
    openFilled();
    putCalls = [];
    doc.getElementById('completeResult').value = '';
    win.submitComplete();
    const error = doc.getElementById('completeResultError');
    assert.equal(error.hidden, false);
    assert.match(error.textContent, /请写下这次发生了什么/);
    assert.equal(putCalls.length, 0);
    assert.equal(doc.activeElement && doc.activeElement.id, 'completeResult');
  });
  await check('missing next step shows the next-step inline error and sends nothing', () => {
    openFilled();
    putCalls = [];
    doc.getElementById('completeResult').value = '客户回复';
    doc.getElementById('completeHasNext').checked = true;
    win.toggleCompleteNext();
    doc.getElementById('completeNextTask').value = '';
    win.submitComplete();
    assert.equal(doc.getElementById('completeNextError').hidden, false);
    assert.equal(putCalls.length, 0);
  });
  await check('Cmd/Ctrl+Enter submits from the textarea', () => {
    assert.ok(/onCompleteResultKeydown/.test(doc.getElementById('completeResult').getAttribute('onkeydown')));
    openFilled();
    putCalls = [];
    putMode = 'ok';
    doc.getElementById('completeResult').value = '快捷键提交';
    win.onCompleteResultKeydown(keyEvent('Enter', { metaKey: true }));
    assert.equal(putCalls.length, 1);
  });
  await check('a successful submit closes the modal, sends one PUT and shows 已记录', async () => {
    openFilled();
    putCalls = [];
    putMode = 'ok';
    doc.getElementById('completeResult').value = '客户确认了样品';
    doc.getElementById('completeHasNext').checked = false;
    win.submitComplete();
    await tick();
    await tick();
    assert.equal(putCalls.length, 1);
    assert.ok(putCalls[0].url.indexOf('/api/reminders/5') >= 0);
    assert.equal(doc.getElementById('completeModal').classList.contains('show'), false);
    const toast = doc.querySelector('.toast');
    assert.ok(toast && /已记录/.test(toast.textContent), 'missing 已记录 toast');
  });
  await check('double submit sends a single write', async () => {
    openFilled();
    putCalls = [];
    putMode = 'ok';
    doc.getElementById('completeResult').value = '并发提交';
    await Promise.all([win.submitComplete(), win.submitComplete()]);
    await tick();
    assert.equal(putCalls.length, 1);
  });
  await check('a failed submit keeps the modal open with the text preserved and an inline error', async () => {
    openFilled();
    putCalls = [];
    putMode = 'fail';
    doc.getElementById('completeResult').value = '这段内容不能丢';
    win.submitComplete();
    await tick();
    await tick();
    assert.equal(doc.getElementById('completeModal').classList.contains('show'), true);
    assert.equal(doc.getElementById('completeResult').value, '这段内容不能丢');
    assert.equal(doc.getElementById('completeSubmitError').hidden, false);
    assert.equal(doc.getElementById('completeSubmitBtn').disabled, false);
    assert.match(doc.getElementById('completeSubmitBtn').textContent, /完成跟进/);
  });

  console.log('scenario: 未保存确认（复用现有机制）');
  await check('closing with typed text opens the shared unsaved-changes prompt', () => {
    openFilled();
    doc.getElementById('completeResult').value = '还没保存的内容';
    assert.equal(win.closeModal('completeModal'), false);
    assert.ok(doc.getElementById('unsavedChangesModal').classList.contains('show'));
    win.discardCustomerFormChanges();
  });
  await check('an untouched modal closes directly', () => {
    openFilled();
    assert.equal(win.closeModal('completeModal'), true);
  });

  if (failures) {
    console.error('\ncomplete modal regression: ' + failures + ' check(s) failed');
    process.exit(1);
  }
  console.log('\ncomplete modal regression: OK');
  process.exit(0);
})();
