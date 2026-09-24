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
    why: '系统缺少作出安全判断所需的信息。', customer: null,
    suggested_customer: null, known_facts: ['尚未关联任何客户'],
    sela_review: { candidates: [
      { customer_id: 7, company: 'Existing A', website: 'https://a.example', matched_by: ['email'] },
      { customer_id: 8, company: 'Existing B', matched_by: ['domain'] },
    ] },
    evidence: [{
      item_id: 9, source_label: 'Sela', date: '2026-09-12', detail: 'MULTIPLE_TROSA_MATCHES',
      structured: {
        kind: '多个客户都匹配到这个来源', company: 'Acrílicos', fields: [],
        candidates: [
          { customer_id: 7, company: 'Existing A', website: 'https://a.example', matched_by: ['email'] },
          { customer_id: 8, company: 'Existing B', matched_by: ['domain'] },
        ],
      },
    }],
    response_schema: {
      fields: [
        { key: 'decision', label: '判断结果', input_type: 'choice', required: true,
          choices: [{ value: 'same', label: '是同一主体' }, { value: 'different', label: '不是同一主体' }] },
        { key: 'customer_id', label: '同一主体对应的客户', input_type: 'customer_picker',
          required: false, validation: { options_source: 'customers' }, help: '' },
      ],
      attachments: { allowed: false },
    },
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

  assert.equal(doc.getElementById('inboxSelaRuns').hidden, true);

  // 总量只在摘要出现一次；类别筛选保留各自计数
  const chips = Array.from(doc.querySelectorAll('#inboxFilters .inbox-filter'));
  const chipKeys = chips.map((chip) => chip.dataset.inboxFilter);
  assert.deepEqual(chipKeys, ['all', 'identity', 'identity_review']);
  assert.equal(doc.querySelector('#inboxOverview strong').textContent, '2');
  assert.equal(doc.querySelector('[data-inbox-filter="all"] .inbox-filter-count'), null);
  assert.equal(doc.querySelector('[data-inbox-filter="identity"] .inbox-filter-count').textContent, '1');

  // 队列以“我需要决定什么”为标题，而不是技术来源
  const headline = doc.getElementById('inboxList').textContent;
  assert.ok(headline.includes('这可能属于 TEXFIRE'), headline);
  assert.ok(headline.includes('确认这是否是同一个业务主体'), headline);
  assert.ok(!headline.includes('系统缺少作出安全判断所需的信息。'), headline);

  // 展开后形成队列、连续证据、前置回答三个真实文档区域。
  win.openInboxQuestion(String(questions[0].primary_item_id));
  const detail = doc.querySelector('.inbox-question-active');
  assert.ok(detail, '展开后应有问题工作台');
  assert.ok(doc.getElementById('page-inbox').classList.contains('inbox-question-expanded'));
  assert.ok(detail.querySelector('.inbox-question-queue').textContent.includes('为什么需要你'));
  assert.ok(detail.querySelector('.inbox-question-evidence').textContent.includes('2.8mm clear acrylic sheets'));
  assert.ok(detail.querySelector('.inbox-question-decision'), '回答区域应前置');
  win.closeInboxQuestion();
  assert.ok(!doc.getElementById('page-inbox').classList.contains('inbox-question-expanded'));
  win.openInboxQuestion(String(questions[1].primary_item_id));
  const genericReasonDetail = doc.querySelector('.inbox-question-active .inbox-question-queue');
  assert.ok(genericReasonDetail, 'generic-reason question should still open');
  assert.ok(!genericReasonDetail.textContent.includes('为什么需要你'));
  assert.ok(!genericReasonDetail.textContent.includes('系统缺少作出安全判断所需的信息。'));
  // 人工判断“是否同一主体”前必须看到具体匹配到哪些客户及依据。
  const reviewEvidence = doc.querySelector('.inbox-question-active .inbox-question-evidence').textContent;
  assert.ok(reviewEvidence.includes('匹配到的 Trosa 客户'), reviewEvidence);
  assert.ok(reviewEvidence.includes('Existing A') && reviewEvidence.includes('Existing B'), reviewEvidence);
  assert.ok(reviewEvidence.includes('匹配依据：邮箱完全一致'), reviewEvidence);
  assert.ok(reviewEvidence.includes('匹配依据：官网域名一致'), reviewEvidence);
  // 客户选择器直接列出候选，而不是只给一个空下拉框。
  const candidateSelect = doc.querySelector('.inbox-question-active select[data-inbox-picker="customers"]');
  const candidateValues = candidateSelect ? Array.from(candidateSelect.options).map(function(o) { return o.value; }) : [];
  assert.deepEqual(candidateValues, ['', '7', '8']);
  win.closeInboxQuestion();

  // Sela 的 JSON 上下文渲染成带标签的字段，而不是原始 JSON 墙。
  const selaHtml = win.inboxEvidenceHtml({
    source_label: 'Sela',
    structured: {
      company: 'Boomart', kind: 'SEND_APPROVAL', severity: 'AMBER',
      proposal: 'Hello Boomart team,',
      context: '{"action":"send_first_outreach"}',
      fields: [
        { key: 'email', label: '收件人', value: 'plastics@boomart.com.au' },
        { key: 'subject', label: '主题', value: 'Acrylic sheet supply' },
      ],
    },
  }, 0);
  assert.ok(selaHtml.includes('收件人') && selaHtml.includes('plastics@boomart.com.au'), selaHtml);
  assert.ok(selaHtml.includes('主题') && selaHtml.includes('Acrylic sheet supply'), selaHtml);
  assert.ok(!selaHtml.includes('{"action"'), selaHtml);

  // 按问题类别过滤
  win.setInboxQuestionFilter('identity_review');
  const filtered = doc.getElementById('inboxList').textContent;
  assert.ok(filtered.includes('确认这是否是同一个业务主体'), filtered);
  assert.ok(!filtered.includes('TEXFIRE'), filtered);
  win.setInboxQuestionFilter('all');

  const selaQuestion = {
    id: '13', kind: 'sela_request', headline: '补充产品方向', summary: '补充产品方向',
    why_human: 'Sela 需要优先产品线，回答后会继续公开研究。',
    subject: { customer_id: null, company: 'Audit Plastics Co', label: 'Sela prospect' },
    evidence_count: 1, primary_item_id: 13, created_at: '2026-09-23 09:00:00',
    response_schema: { fields: [], attachments: { allowed: false } },
    completion_effects: [], will_not_do: [], evidence: [],
  };
  setInboxPayload({ items: [], questions: [selaQuestion], counts: { all: 1, sela_request: 1 } });
  await win.loadInbox();
  const selaRow = doc.querySelector('.inbox-question-open').textContent;
  assert.ok(selaRow.includes('Sela prospect') && selaRow.includes('Audit Plastics Co'), selaRow);
  win.openInboxQuestion('13');
  const selaDetail = doc.querySelector('.inbox-question-active');
  assert.ok(selaDetail.querySelector('.inbox-question-subject').textContent.includes('尚未关联 Trosa 客户'));
  assert.equal(selaDetail.querySelector('.inbox-question-queue').textContent.match(/补充产品方向/g).length, 1,
    '相同标题和摘要不应在展开区重复');
  win.closeInboxQuestion();

  // Normal background states stay out of the human queue; only exceptions appear.
  win.inboxState.selaRuns = [{ inbox_id: 13, company: 'Audit Plastics Co', status: 'queued', summary: '', updated_at: new Date().toISOString() }];
  win.renderSelaInboxRuns(win.inboxState.selaRuns);
  assert.equal(doc.getElementById('inboxSelaRuns').hidden, true);
  win.renderSelaInboxRuns([{ inbox_id: 13, status: 'completed', updated_at: new Date().toISOString() }]);
  assert.equal(doc.getElementById('inboxSelaRuns').hidden, true);
  win.renderSelaInboxRuns([{ inbox_id: 13, company: 'Audit Plastics Co', status: 'queued', summary: '', updated_at: '2000-01-01 00:00:00' }]);
  assert.equal(doc.getElementById('inboxSelaRuns').hidden, false);
  assert.ok(doc.querySelector('#inboxSelaRuns .inbox-sela-run').textContent.includes('超过 2 分钟未开始'));
  win.renderSelaInboxRuns([{ inbox_id: 13, company: 'Audit Plastics Co', status: 'needs_review', summary: '请复核原因' }]);
  assert.ok(doc.getElementById('inboxSelaRuns').textContent.includes('请复核原因'));

  // Legacy send approvals are kept accessible, but placed after actionable
  // questions and explained once instead of repeating a long warning per row.
  const retiredRequest = Object.assign({}, selaQuestion, {
    id: 'legacy-send', key: 'legacy-send', primary_item_id: 14,
    headline: '发送确认：Legacy Prospect',
    response_schema: { retired_send_approval: true, fields: [], attachments: { allowed: false } },
  });
  setInboxPayload({ items: [], questions: [selaQuestion, retiredRequest], counts: { all: 2, sela_request: 2 } });
  await win.loadInbox();
  assert.equal(doc.querySelectorAll('#inboxList > .inbox-workspace .inbox-question-row').length, 1);
  assert.equal(doc.querySelectorAll('#inboxList .inbox-retired-requests .inbox-question-row').length, 1);
  assert.ok(doc.querySelector('#inboxList .inbox-retired-requests').textContent.includes('关闭后不会发送邮件或启动 Sela'));
  assert.equal(doc.querySelector('#inboxOverview strong').textContent, '1');
  assert.equal(doc.getElementById('inboxNavCount').textContent, '1');
  assert.equal(doc.querySelector('[data-inbox-filter="sela_request"] .inbox-filter-count').textContent, '1');

  // 空列表显示明确空状态而不是“没有数据”
  setInboxPayload({ items: [], questions: [], counts: { all: 0, questions: 0 } });
  await win.loadInbox();
  assert.ok(doc.getElementById('inboxList').textContent.includes('当前没有需要你判断的问题'));

  console.log('inbox render regression: OK');
})().catch((error) => {
  console.error(error && error.stack || error);
  process.exit(1);
});
