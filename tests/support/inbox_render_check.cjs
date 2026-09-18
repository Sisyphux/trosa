// Inbox 页面回归检查：通过真实 loadInbox 数据流驱动 renderInbox。
//
//   * 过滤 chips 按统一动作类别动态渲染并带计数（新条目类型不会丢出入口）。
//   * 分类标签必须有中文名，不允许裸 'other' 这类内部键出现在页面。
//   * 条目行显示已归属客户的名称、国家与联系人上下文。
//   * 单国家小分组不再铺子组头与“今天跟进/导出邮箱”按钮。
//
// app.js 的顶层 let 绑定（inboxItems 等）不在 window 上，因此用 fetch 桩
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

const items = [
  { id: 1, item_type: 'gmail_capture', customer_id: 7, customer_name: 'PLEKSI PMMA', customer_company: '',
    country: '土耳其', contact_name: 'Hamid', capture_content: '请发最新报价单', title: 'x',
    created_at: '2026-09-18 09:00:00', dedupe_key: 'k1' },
  { id: 2, item_type: 'customer_reply', customer_id: 7, customer_name: 'PLEKSI PMMA', customer_company: 'PLEKSI',
    country: '土耳其', contact_name: '', content: '收到，请确认价格',
    created_at: '2026-09-17 08:00:00', dedupe_key: 'k2' },
  { id: 3, item_type: 'ai_suggestion', customer_id: 0, title: '旧签名条目',
    created_at: '2026-09-11 08:00:00', dedupe_key: 'k3' },
];

const respond = (payload) => ({ ok: true, status: 200, json: async () => payload });
let inboxPayload = { items, counts: { all: items.length } };
win.fetch = async (url, options) => {
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

  // chips 动态渲染且带计数
  const chips = Array.from(doc.querySelectorAll('#inboxFilters .inbox-filter'));
  const chipKeys = chips.map((chip) => chip.dataset.inboxFilter);
  assert.deepEqual(chipKeys, ['all', 'new_reply', 'capture', 'sela']);
  assert.equal(doc.querySelector('[data-inbox-filter="all"] .inbox-filter-count').textContent, '3');
  assert.equal(doc.querySelector('[data-inbox-filter="capture"] .inbox-filter-count').textContent, '1');

  // 分类标签不允许裸内部键
  const titles = Array.from(doc.querySelectorAll('.inbox-group-title')).map((el) => el.textContent);
  assert.ok(titles.includes('客户有新回复'), titles.join(','));
  assert.ok(titles.includes('待归属沟通'), titles.join(','));
  assert.ok(titles.includes('其他事项'), `裸分类键漏出: ${titles.join(',')}`);

  // 已归属条目显示客户名 + 上下文（国家 / 联系人 / 已归属标记）
  const captureArticle = doc.querySelector('article.inbox-gmail_capture');
  assert.ok(captureArticle, 'gmail capture 条目应渲染');
  const captureText = captureArticle.textContent;
  assert.ok(captureText.includes('PLEKSI PMMA'), captureText);
  assert.ok(captureText.includes('土耳其'), captureText);
  assert.ok(captureText.includes('Hamid'), captureText);
  assert.ok(captureText.includes('已归属'), captureText);
  assert.ok(captureArticle.querySelector('.inbox-item-type-chip'), captureText);

  // 单国家小分组不再铺国家组头和批量按钮
  assert.equal(doc.querySelectorAll('#page-inbox .inbox-country-header').length, 0);

  // 切换过滤器后只显示该动作类别的条目
  win.setInboxFilter('new_reply');
  const filterText = doc.getElementById('inboxList').textContent;
  assert.ok(filterText.includes('客户有新回复'), filterText);
  assert.ok(!filterText.includes('待归属沟通'), filterText);
  win.setInboxFilter('all');

  // 空列表显示明确的空状态而不是“没有数据”
  setInboxPayload({ items: [], counts: { all: 0 } });
  await win.loadInbox();
  assert.ok(doc.getElementById('inboxList').textContent.includes('Inbox 已清空'));

  console.log('inbox render regression: OK');
})().catch((error) => {
  console.error(error && error.stack || error);
  process.exit(1);
});
