// Regression harness: small-window required-field validation is inline.
//
// U-08 of the 2026-09-30 UI review found that, outside 完成这次跟进, the small
// windows answered a missing required field with a toast and left the field
// unmarked. This harness pins the fix:
//   * each required field owns a `.field-inline-error` sentence next to it;
//   * submitting with the field empty shows that sentence, marks the field
//     aria-invalid (so its underline turns the danger colour), sends no
//     request and raises no toast;
//   * clearing the field's error puts the field back to normal;
//   * the complete dialog marks its textarea, and its next-step line, the same
//     way instead of reusing the gold focus colour.
//
// Run standalone (needs node + jsdom):
//   node tests/support/modal_validation_check.cjs
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
win.matchMedia = win.matchMedia || (() => ({
  matches: false,
  media: '',
  addEventListener() {},
  removeEventListener() {},
  addListener() {},
  removeListener() {},
}));
win.eval(script);
win._motionReduced = true;

const tick = () => new Promise((resolve) => setTimeout(resolve, 0));

let failures = 0;
function check(label, body) {
  return Promise.resolve()
    .then(body)
    .then(() => console.log('  ok   - ' + label))
    .catch((error) => {
      failures += 1;
      console.log('  FAIL - ' + label + '\n         ' + error.message);
    });
}

function resetToasts() {
  doc.getElementById('toastContainer').innerHTML = '';
}
function toastCount() {
  return doc.querySelectorAll('#toastContainer .toast').length;
}
function isInvalid(id) {
  const el = doc.getElementById(id);
  return el.getAttribute('aria-invalid') === 'true' && el.classList.contains('is-invalid');
}

const PAIRS = [
  { modal: 'addNewCustomerModal', field: 'newCustomerName', error: 'newCustomerNameError', submit: () => win.submitNewCustomer(), message: /请填写公司名称/ },
  { modal: 'addCustomerModal', field: 'addExistName', error: 'addExistNameError', submit: () => win.submitExistCustomer(false), message: /请填写公司名称/ },
  { modal: 'batchAddModal', field: 'batchAddText', error: 'batchAddTextError', submit: () => win.submitBatchAdd(), message: /请输入客户数据/ },
];

(async function main() {
  console.log('scenario: 每个必填字段都带内联错误');
  for (const pair of PAIRS) {
    await check(pair.modal + ' 的空提交显示内联错误、标记字段、不发请求、不 toast', async () => {
      resetToasts();
      const field = doc.getElementById(pair.field);
      field.value = '';
      await pair.submit();
      await tick();
      const error = doc.getElementById(pair.error);
      assert.equal(error.hidden, false, 'inline error should be visible');
      assert.match(error.textContent, pair.message);
      assert.ok(isInvalid(pair.field), 'field should be aria-invalid');
      assert.equal(toastCount(), 0, 'must not fall back to a toast');
    });

    await check(pair.modal + ' 清空错误后字段恢复正常', () => {
      win.setFieldInlineError(pair.field, pair.error, '');
      assert.equal(doc.getElementById(pair.error).hidden, true);
      assert.equal(isInvalid(pair.field), false, 'field should not stay invalid');
    });
  }

  console.log('scenario: 下一步动作与日期的内联错误');
  await check('缺少动作时标题字段标红', async () => {
    resetToasts();
    doc.getElementById('customerTaskTitle').value = '';
    doc.getElementById('customerTaskDate').value = '';
    await win.createCustomerTask(null);
    await tick();
    assert.equal(doc.getElementById('customerTaskTitleError').hidden, false);
    assert.match(doc.getElementById('customerTaskTitleError').textContent, /请填写具体动作/);
    assert.ok(isInvalid('customerTaskTitle'));
    assert.equal(toastCount(), 0);
  });
  await check('只缺日期时日期字段标红', async () => {
    resetToasts();
    doc.getElementById('customerTaskTitle').value = '确认样品测试结果';
    doc.getElementById('customerTaskDate').value = '';
    await win.createCustomerTask(null);
    await tick();
    assert.equal(doc.getElementById('customerTaskDateError').hidden, false);
    assert.match(doc.getElementById('customerTaskDateError').textContent, /请选择日期/);
    assert.ok(isInvalid('customerTaskDate'));
    assert.equal(toastCount(), 0);
  });
  await check('选择日期 chip 会清掉日期错误', () => {
    win.setCustomerTaskDate(7, doc.querySelector('#customerTaskModal .task-date-choices button'));
    assert.equal(doc.getElementById('customerTaskDateError').hidden, true);
    assert.equal(isInvalid('customerTaskDate'), false);
    assert.ok(doc.getElementById('customerTaskDate').value);
  });

  console.log('scenario: 快速编辑 / 收件箱确认的内联错误');
  await check('快速编辑没有可改字段时给出内联说明', async () => {
    resetToasts();
    const todayList = doc.createElement('div');
    todayList.id = 'todayReminders';
    todayList.innerHTML = '<div class="today-task-row"><input type="checkbox" class="today-task-checkbox" data-id="1" checked></div>';
    doc.body.appendChild(todayList);
    win.updateTodaySelection();
    doc.getElementById('todayQuickEditLevel').value = '';
    doc.getElementById('todayQuickEditBusinessStage').value = '';
    doc.getElementById('todayQuickEditNextFollowUp').value = '';
    doc.getElementById('todayQuickEditContent').value = '';
    await win.submitTodayQuickEdit();
    await tick();
    const error = doc.getElementById('todayQuickEditError');
    assert.equal(error.hidden, false);
    assert.match(error.textContent, /请至少选择一项/);
    assert.equal(toastCount(), 0);
  });
  await check('收件箱确认缺内容 / 缺客户分别内联提示', async () => {
    resetToasts();
    win._communicationConfirmContext = { source: 'manual', direction: 'inbound', activityType: 'customer_reply' };
    doc.getElementById('inboxReplyCustomer').value = '';
    doc.getElementById('inboxReplyContent').value = '';
    await win.saveInboxReply();
    await tick();
    assert.equal(doc.getElementById('inboxReplyContentError').hidden, false);
    assert.ok(isInvalid('inboxReplyContent'));
    doc.getElementById('inboxReplyContent').value = '客户回复了报价';
    await win.saveInboxReply();
    await tick();
    assert.equal(doc.getElementById('inboxReplyContentError').hidden, true, 'content error should clear');
    assert.equal(doc.getElementById('inboxReplyCustomerError').hidden, false);
    assert.ok(isInvalid('inboxReplyCustomerSearch'));
    assert.equal(toastCount(), 0);
  });

  console.log('scenario: 完成这次跟进的字段线用危险色而不是金色');
  await check('空内容把文本框标为 aria-invalid', () => {
    doc.getElementById('completeResult').value = '';
    win.submitComplete();
    assert.equal(doc.getElementById('completeResultError').hidden, false);
    assert.ok(isInvalid('completeResult'));
  });
  await check('缺少下一步动作时输入框标红', () => {
    doc.getElementById('completeResult').value = '客户回复';
    doc.getElementById('completeHasNext').checked = true;
    win.toggleCompleteNext();
    doc.getElementById('completeNextTask').value = '';
    win.submitComplete();
    assert.ok(isInvalid('completeNextTask'));
    assert.equal(doc.getElementById('completeNextError').hidden, false);
  });
  await check('只缺日期时日期 chip 下划线转为危险色', () => {
    doc.getElementById('completeResult').value = '客户回复';
    doc.getElementById('completeHasNext').checked = true;
    win.toggleCompleteNext();
    doc.getElementById('completeNextTask').value = '发报价';
    doc.getElementById('completeNextFollow').value = '';
    doc.getElementById('completeDateChoices').classList.remove('is-invalid');
    win.submitComplete();
    assert.ok(doc.getElementById('completeDateChoices').classList.contains('is-invalid'));
  });
  await check('重开弹窗清掉整组错误', () => {
    win.clearCompleteErrors();
    assert.equal(isInvalid('completeResult'), false);
    assert.equal(isInvalid('completeNextTask'), false);
    assert.equal(doc.getElementById('completeDateChoices').classList.contains('is-invalid'), false);
  });

  if (failures) {
    console.log('\nmodal validation regression: ' + failures + ' FAILED');
    process.exit(1);
  }
  console.log('\nmodal validation regression: OK');
})();
