// Inbox 页面回归检查：通过真实 loadInbox 数据流驱动 renderInbox（问题模型）。
//
//   * 过滤 chips 按问题类别动态渲染并带计数（all/identity/reply/approval/identity_review）。
//   * 问题卡片显示客户/发件人上下文与“我需要决定什么”的标题。
//   * 展开后显示完整上下文：为什么需要你、系统已知、证据与选项。
//   * 空列表显示明确空状态。
//
// app.js 的顶层 let 绑定（inboxQuestions 等）不在 window 上，因此用 fetch 桩
// 走真实的 loadInbox() 数据路径填充状态，而不是直接给 window 赋值。
//
// Run standalone (needs node + jsdom):
//   node tests/support/inbox_render_check.cjs
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

const questions = [
  {
    key: 'identity:chris@texfireco.com', kind: 'identity', kind_label: '待归属',
    question: '这条沟通属于哪个客户？', headline: '这可能属于 TEXFIRE，请确认归属',
    why: '系统无法从发件邮箱唯一确定客户。', customer: null,
    suggested_customer: { customer_id: 7, company: 'TEXFIRE', reason: '官网域名' },
    known_facts: ['来源身份：chris@texfireco.com', '尚未关联任何客户'],
    evidence: [{ item_id: 4, source_label: 'Gmail', date: '2026-09-13', identity: 'chris@texfireco.com', detail: 'Hi, we need 2.8mm clear acrylic sheets.' }],
    options: [
      { key: 'assign_suggested', action: 'record', style: 'primary', label: '归到 TEXFIRE' },
      { key: 'assign_other', action: 'record', label: '选择其他客户并记录' },
      { key: 'archive', action: 'archive', style: 'text', label: '不是客户沟通' },
    ],
    primary_item_id: 4, item_ids: [4], source_type: 'gmail', source_label: 'Gmail',
    created_at: '2026-09-13 08:00:00',
  },
  {
    key: 'sela:prospect-review:p1', kind: 'identity_review', kind_label: '身份待确认',
    question: '这两条身份记录是不是同一个业务主体？', headline: '确认这是否是同一个业务主体',
    why: '仅凭名称或来源无法安全判定是否为同一主体。', customer: null,
    suggested_customer: null, known_facts: ['尚未关联任何客户'],
    evidence: [{ item_id: 9, source_label: 'Sela', date: '2026-09-12', detail: 'MULTIPLE_TROSA_MATCHES' }],
    options: [
      { key: 'same', action: 'identity_same', style: 'primary', label: '是同一主体' },
      { key: 'different', action: 'identity_different', style: 'text', label: '不是同一主体' },
    ],
    primary_item_id: 9, item_ids: [9], source_type: 'sela', source_label: 'Sela',
    created_at: '2026-09-12 08:00:00',
  },
];

const respond = (payload) => ({ ok: true, status: 200, json: async () => payload });
let inboxPayload = {
  items: [],
  questions,
  counts: { all: 2, questions: 2, identity: 1, identity_review: 1, reply: 0, approval: 0, capture: 1, customer_reply: 0 },
};
win.fetch = async (url) => {
  const target = String(url);
  if (target.indexOf('/api/inbox') !== -1 && target.indexOf('capture-matches') === -1 && target.indexOf('counts') === -1) {
    return respond(inboxPayload);
  }
  return respond({});
};
win.eval(script);

const doc = win.document;
const setInboxPayload = (payload) => { inboxPayload = payload; };

(async () => {
  await win.loadInbox();

  // chips 动态渲染且带计数，只显示存在问题的问题类别
  const chips = Array.from(doc.querySelectorAll('#inboxFilters .inbox-filter'));
  const chipKeys = chips.map((chip) => chip.dataset.inboxFilter);
  assert.deepEqual(chipKeys, ['all', 'identity', 'identity_review']);
  assert.equal(doc.querySelector('[data-inbox-filter="all"] .inbox-filter-count').textContent, '2');
  assert.equal(doc.querySelector('[data-inbox-filter="identity"] .inbox-filter-count').textContent, '1');

  // 队列以“我需要决定什么”为标题，而不是技术来源
  const headline = doc.getElementById('inboxList').textContent;
  assert.ok(headline.includes('这可能属于 TEXFIRE'), headline);
  assert.ok(headline.includes('确认这是否是同一个业务主体'), headline);

  // 展开后形成队列、连续证据、前置回答三个真实文档区域。
  win.openInboxQuestion(String(questions[0].primary_item_id));
  const detail = doc.querySelector('.inbox-question-active');
  assert.ok(detail, '展开后应有问题工作台');
  assert.ok(detail.querySelector('.inbox-question-queue').textContent.includes('为什么需要你'));
  assert.ok(detail.querySelector('.inbox-question-evidence').textContent.includes('2.8mm clear acrylic sheets'));
  assert.ok(detail.querySelector('.inbox-question-decision'), '回答区域应前置');
  win.closeInboxQuestion();

  // 按问题类别过滤
  win.setInboxQuestionFilter('identity_review');
  const filtered = doc.getElementById('inboxList').textContent;
  assert.ok(filtered.includes('确认这是否是同一个业务主体'), filtered);
  assert.ok(!filtered.includes('TEXFIRE'), filtered);
  win.setInboxQuestionFilter('all');

  // 空列表显示明确空状态而不是“没有数据”
  setInboxPayload({ items: [], questions: [], counts: { all: 0, questions: 0 } });
  await win.loadInbox();
  assert.ok(doc.getElementById('inboxList').textContent.includes('当前没有需要你判断的问题'));

  console.log('inbox render regression: OK');
})().catch((error) => {
  console.error(error && error.stack || error);
  process.exit(1);
});
