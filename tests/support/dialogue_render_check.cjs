// 对话界面回归检查：通过真实 loadDialogue / selectDialogueThread / submitDialogueReply
// 数据流驱动 renderDialogueList / renderDialogueThreadHtml。
//
//   * 列表按「等你 / sela 处理中 / 已完成」分组，行内显示标题、摘要、轮到谁与多久。
//   * 打开一条对话后显示消息（含 system）与 sela 的建议回复。
//   * 建议回复只填入回复框，不会直接发送。
//   * 发送后提示「sela 已收到」，并自动跳到下一条「等你」。
//   * thread_changed 冲突显示「有新消息，请先看」提示，不静默重试。
//   * 列表为空显示明确空状态；加载失败显示失败原因而不是「没有数据」。
//   * J/K 在列表内切换，界面里没有按问题类型生成的表单分支。
//
// app.js 的顶层 let/var 绑定不在 window 上，因此用 fetch 桩走真实数据路径。
//
// Run standalone (needs node + jsdom):
//   node tests/support/dialogue_render_check.cjs
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
win.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {} });

const A = 'aaaaaaaa-1111-4111-8111-111111111111';
const B = 'bbbbbbbb-2222-4222-8222-222222222222';
const C = 'cccccccc-3333-4333-8333-333333333333';
const D = 'dddddddd-4444-4444-8444-444444444444';

const now = () => new Date().toISOString();

const summaries = [
  { id: A, subject: 'prospect:alpha', title: 'Alpha 竞品排除', status: 'open', awaiting: 'human', revision: 2,
    opened_at: now(), updated_at: now(), closed_at: null, closed_by: null, closed_summary: null,
    last_message_preview: { role: 'sela', text: 'Northwind 是不是已经排除了？', created_at: now() } },
  { id: B, subject: 'prospect:beta', title: 'Beta 联系方式', status: 'open', awaiting: 'human', revision: 3,
    opened_at: now(), updated_at: now(), closed_at: null, closed_by: null, closed_summary: null,
    last_message_preview: { role: 'sela', text: '有可用的联系邮箱吗？', created_at: now() } },
  { id: C, subject: 'prospect:gamma', title: 'Gamma 进度', status: 'open', awaiting: 'sela', revision: 1,
    opened_at: now(), updated_at: now(), closed_at: null, closed_by: null, closed_summary: null,
    last_message_preview: { role: 'human', text: '继续跟进', created_at: now() } },
  { id: D, subject: 'prospect:delta', title: 'Delta 已完成', status: 'closed', awaiting: 'none', revision: 4,
    opened_at: now(), updated_at: now(), closed_at: now(), closed_by: 'human', closed_summary: '用户关闭',
    last_message_preview: { role: 'sela', text: '这件事已完成', created_at: now() } },
];

const threads = {
  [A]: { id: A, subject: 'prospect:alpha', title: 'Alpha 竞品排除', status: 'open', awaiting: 'human', revision: 2,
    opened_at: now(), updated_at: now(), closed_at: null, closed_by: null, closed_summary: null, messages: [
      { id: 'm1', thread_id: A, seq: 1, role: 'sela', text: '要不要排除 Northwind？',
        suggested_replies: ['是，排除这家竞品', '不是，保留它'], refs: [{ type: 'customer', id: '7' }],
        hints: null, attachments: [], created_at: now() },
      { id: 'm2', thread_id: A, seq: 2, role: 'system', text: '该问题已超过等待时限（30 天）。',
        suggested_replies: [], refs: [], hints: null, attachments: [], created_at: now() },
    ] },
  [B]: { id: B, subject: 'prospect:beta', title: 'Beta 联系方式', status: 'open', awaiting: 'human', revision: 3,
    opened_at: now(), updated_at: now(), closed_at: null, closed_by: null, closed_summary: null, messages: [
      { id: 'm3', thread_id: B, seq: 1, role: 'sela', text: '有可用的联系邮箱吗？',
        suggested_replies: ['用官网上的 info@'], refs: [], hints: null, attachments: [], created_at: now() },
      { id: 'm4', thread_id: B, seq: 2, role: 'human', text: '稍后补', suggested_replies: [], refs: [],
        hints: null, attachments: [], created_at: now() },
      { id: 'm5', thread_id: B, seq: 3, role: 'sela', text: '请再确认一下。', suggested_replies: [],
        refs: [], hints: null, attachments: [], created_at: now() },
    ] },
  [C]: { id: C, subject: 'prospect:gamma', title: 'Gamma 进度', status: 'open', awaiting: 'sela', revision: 1,
    opened_at: now(), updated_at: now(), closed_at: null, closed_by: null, closed_summary: null, messages: [
      { id: 'm6', thread_id: C, seq: 1, role: 'human', text: '继续跟进', suggested_replies: ['继续跟进'],
        refs: [], hints: null, attachments: [], created_at: now() },
    ] },
  [D]: { id: D, subject: 'prospect:delta', title: 'Delta 已完成', status: 'closed', awaiting: 'none', revision: 4,
    opened_at: now(), updated_at: now(), closed_at: now(), closed_by: 'human', closed_summary: '用户关闭', messages: [] },
};

const counts = { awaiting_human: 2, awaiting_sela: 1, open: 3 };
let listMode = 'ok';       // 'ok' | 'empty' | 'error'
let replyMode = 'success'; // 'success' | 'conflict'
let replyPosts = 0;
let closePosts = 0;

const jsonResponse = (payload, status) => ({
  ok: (status || 200) < 400, status: status || 200, json: async () => payload,
});
const errorResponse = (payload, status) => ({
  ok: false, status: status, json: async () => payload,
});

win.fetch = async (url, options) => {
  const target = String(url);
  const method = String((options && options.method) || 'GET').toUpperCase();
  // 保持自动会话探测挂起：解析为“无会话”会退回账号界面，丢掉本检查渲染的状态。
  if (target.indexOf('/api/auth/me') === 0) return new Promise(() => {});
  if (target.indexOf('/api/inbox/threads') !== -1) {
    if (method === 'POST' && /\/reply$/.test(target)) {
      replyPosts += 1;
      if (replyMode === 'conflict') {
        return errorResponse({ error: { code: 'thread_changed', message: '对话已被更新',
          details: { current_revision: 3, current_awaiting: 'human' } } }, 409);
      }
      return jsonResponse({ success: true, thread: threads[A], message: {}, undo_token: null });
    }
    if (method === 'POST' && /\/close$/.test(target)) {
      closePosts += 1;
      return jsonResponse({ success: true, thread: threads[A] });
    }
    if (target.indexOf('limit=1') !== -1) {
      return jsonResponse({ success: true, threads: summaries.slice(0, 1), next_cursor: null, counts });
    }
    if (target.indexOf('status=all') !== -1) {
      if (listMode === 'empty') return jsonResponse({ success: true, threads: [], next_cursor: null, counts: { awaiting_human: 0, awaiting_sela: 0, open: 0 } });
      if (listMode === 'error') return errorResponse({ error: { code: 'internal_error', message: '对话列表读取失败' } }, 400);
      return jsonResponse({ success: true, threads: summaries, next_cursor: null, counts });
    }
    if (target.indexOf('/api/inbox/threads/') !== -1) {
      const id = decodeURIComponent(target.split('/api/inbox/threads/')[1].split('?')[0]);
      if (threads[id]) return jsonResponse({ success: true, thread: threads[id] });
      return errorResponse({ error: { code: 'not_found', message: '对话不存在' } }, 404);
    }
  }
  return jsonResponse({});
};
win.eval(script);

const doc = win.document;
const tick = (ms) => new Promise((resolve) => setTimeout(resolve, ms || 0));

(async () => {
  doc.documentElement.className = '';
  const page = doc.getElementById('page-dialogue');
  page.classList.add('active');

  // 列表按状态分组，并把「轮到谁」「多久」呈现出来。
  await win.loadDialogue();
  const list = doc.getElementById('dialogueList');
  const groups = Array.from(list.querySelectorAll('.dialogue-group')).map((g) => g.dataset.group);
  assert.deepEqual(groups, ['awaiting', 'sela', 'done'], JSON.stringify(groups));
  assert.ok(list.textContent.includes('等你') && list.textContent.includes('sela 处理中') && list.textContent.includes('已完成'));
  assert.equal(list.querySelector('[data-group="awaiting"] .dialogue-group-label .tnum').textContent, '2');
  assert.ok(list.querySelector('[data-group="awaiting"]').textContent.includes('Northwind 是不是已经排除了？'), list.textContent);
  assert.ok(doc.getElementById('dialogueOverview').textContent.includes('2'), doc.getElementById('dialogueOverview').textContent);
  assert.equal(doc.getElementById('dialogueNavCount').textContent, '2');

  // 对话是 Inbox 的页签，不是独立入口：导航里没有「对话」，它的数字并进 Inbox。
  const navPages = Array.from(doc.querySelectorAll('.nav-personal [data-nav-page]')).map((el) => el.dataset.navPage);
  assert.deepEqual(navPages, ['dashboard', 'inbox', 'customers'], JSON.stringify(navPages));
  assert.equal(doc.getElementById('inboxNavCount').textContent, '2');
  win.setInboxOtherCount(3);
  assert.equal(doc.getElementById('inboxNavCount').textContent, '5');
  assert.equal(doc.getElementById('inboxOtherCount').textContent, '3');
  win.setInboxOtherCount(0);

  // 保存过的导航顺序里没有的页面，留在默认位置，不能浮到最前。
  const nav = doc.querySelector('.nav-personal');
  const extra = doc.createElement('div');
  extra.className = 'nav-item';
  extra.dataset.navPage = 'brandnew';
  nav.querySelector('[data-nav-page="inbox"]').after(extra);
  win.applyNavOrder(['customers', 'dashboard']);
  const reordered = Array.from(nav.querySelectorAll('[data-nav-page]')).map((el) => el.dataset.navPage);
  assert.deepEqual(reordered, ['customers', 'dashboard', 'inbox', 'brandnew'], JSON.stringify(reordered));
  win.applyNavOrder([]);
  extra.remove();
  win.updateTodayDialogueEntry();
  assert.equal(doc.getElementById('todayDialogueEntry').hidden, false);
  assert.equal(doc.getElementById('todayDialogueCount').textContent, '2');

  // 默认选中最靠前的「等你」对话：消息含 sela 与 system，建议回复以可点击文字出现。
  assert.equal(win.dialogueSelectedId, A, '应默认打开第一条等你对话');
  const threadPane = doc.getElementById('dialogueThreadPane');
  assert.ok(threadPane.querySelector('.dialogue-msg--sela'), '应有 sela 消息');
  assert.ok(threadPane.querySelector('.dialogue-msg--system'), '应有 system 消息');
  const suggestions = threadPane.querySelectorAll('.dialogue-suggestion');
  assert.deepEqual(Array.from(suggestions).map((s) => s.textContent), ['是，排除这家竞品', '不是，保留它']);
  // 建议回复只填入回复框：点击后不产生任何发送请求。
  const beforePosts = replyPosts;
  win.fillDialogueSuggestion(suggestions[0]);
  assert.equal(doc.getElementById('dialogueReplyInput').value, '是，排除这家竞品');
  assert.equal(replyPosts, beforePosts, '点击建议回复不应发送');

  // 界面里没有按问题类型生成的表单分支。
  assert.equal(threadPane.querySelectorAll('[data-inbox-field]').length, 0);
  assert.equal(threadPane.querySelectorAll('form.dialogue-composer textarea').length, 1);

  // 回车提交：冲突时显示「有新消息，请先看」，不静默重试。
  replyMode = 'conflict';
  doc.getElementById('dialogueReplyInput').value = '是，排除这家竞品';
  await win.submitDialogueReply({ preventDefault() {} });
  assert.ok(threadPane.querySelector('.dialogue-conflict'), '冲突应显示提示横幅');
  assert.ok(threadPane.querySelector('.dialogue-conflict').textContent.includes('有新消息，请先看'));
  assert.ok(threadPane.querySelector('.dialogue-composer-error').textContent.includes('有新消息，请先看'));

  // 正常发送：提示「sela 已收到」，并自动跳到下一条「等你」。
  replyMode = 'success';
  doc.getElementById('dialogueReplyInput').value = '是，排除这家竞品';
  await win.submitDialogueReply({ preventDefault() {} });
  assert.ok(doc.getElementById('toastContainer').textContent.includes('sela 已收到'), '应提示 sela 已收到');
  assert.equal(win.dialogueSelectedId, B, '发送后应自动跳到下一条等你对话');
  assert.ok(doc.querySelector('.dialogue-row.is-selected[data-dialogue-id="' + B + '"]'));

  // 键盘 J/K 在列表内切换。
  win.dialogueKeydown({ key: 'k', target: doc.body, preventDefault() {}, defaultPrevented: false, metaKey: false, ctrlKey: false, altKey: false });
  await tick(5);
  assert.equal(win.dialogueSelectedId, A, 'K 应上移到前一条');
  win.dialogueKeydown({ key: 'j', target: doc.body, preventDefault() {}, defaultPrevented: false, metaKey: false, ctrlKey: false, altKey: false });
  await tick(5);
  assert.equal(win.dialogueSelectedId, B, 'J 应下移到后一条');

  // 关闭对话走确认流程（不在此检查里点“确认”按钮）。

  // 空列表显示明确空状态。
  listMode = 'empty';
  win.dialogueSelectedId = null;
  win.dialogueThread = null;
  await win.loadDialogue();
  assert.ok(doc.getElementById('dialogueList').textContent.includes('现在没有对话'), doc.getElementById('dialogueList').textContent);
  assert.ok(doc.getElementById('dialogueList').querySelector('[data-state="empty"]'));
  assert.ok(doc.getElementById('dialogueThreadPane').querySelector('[data-state="empty"]'));

  // 加载失败显示失败原因并给出重试，绝不显示成「没有数据」。
  listMode = 'error';
  win.dialogueListError = null;
  win.dialogueThreads = [];
  await win.loadDialogue();
  const failed = doc.getElementById('dialogueList');
  assert.ok(failed.querySelector('[data-state="error"]'), failed.textContent);
  assert.ok(failed.textContent.includes('失败'), failed.textContent);
  assert.ok(!failed.textContent.includes('现在没有对话'), failed.textContent);
  assert.ok(failed.querySelector('button'), '应提供重试按钮');

  // Inbox 页签：两个页签同属一个 Inbox，导航始终高亮 Inbox。
  win.switchPage('dialogue');
  await tick(50);
  assert.equal(doc.getElementById('inboxTabs').hidden, false);
  assert.ok(doc.getElementById('inboxTabDialogue').classList.contains('active'));
  assert.ok(doc.querySelector('.nav-item[data-page="inbox"]').classList.contains('active'), '对话页时导航应高亮 Inbox');
  win.switchPage('inbox');
  await tick(50);
  assert.ok(doc.getElementById('inboxTabOther').classList.contains('active'));
  win.syncInboxTabs('customers');
  assert.equal(doc.getElementById('inboxTabs').hidden, true);

  process.stdout.write('dialogue render regression: OK\n', () => process.exit(0));
})().catch((error) => {
  console.error(error && error.stack || error);
  process.exit(1);
});
