const $ = (id) => document.getElementById(id);
const app = $('app');
let capture = null;
let selected = null;
let lastMatch = null;
let apiBase = 'https://app.trosa.space';
const esc = (value) => String(value || '').replace(/[&<>"']/g, (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[char]));
const customerName = (customer) => customer?.company || customer?.name || '未命名客户';
const contactIdentity = (contact) => contact?.email || contact?.whatsapp || contact?.phone || '无邮箱/电话';
const getSettings = () => new Promise((resolve) => chrome.storage.local.get(['apiBase'], (value) => { apiBase = value.apiBase || apiBase; resolve(value); }));

async function api(path, options = {}) {
  const response = await fetch(`${apiBase}${path}`, { credentials: 'include', headers: { 'Content-Type': 'application/json' }, ...options });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) { const error = new Error(body.error || `请求失败（${response.status}）`); error.status = response.status; throw error; }
  return body;
}

function renderLogin() {
  app.innerHTML = $('login-template').innerHTML; $('api-base').value = apiBase;
  $('login').onclick = async () => {
    $('login-error').textContent = ''; apiBase = $('api-base').value.trim().replace(/\/$/, ''); await chrome.storage.local.set({ apiBase });
    try { await api('/api/auth/login', { method: 'POST', body: JSON.stringify({ user: $('user').value, pin: $('pin').value }) }); load(); }
    catch (error) { $('login-error').textContent = error.message; }
  };
}

const wait = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));
async function readPage() {
  let result = await window.TradeOSFrameReader.readPage(chrome);
  // Mail and WhatsApp often mount the current thread after the side panel opens.
  // Retry only a successful-but-empty read; hard failures remain immediately visible.
  for (let attempt = 0; attempt < 2 && result?.ok && !(result.data?.messages || []).length; attempt += 1) {
    await wait(550);
    result = await window.TradeOSFrameReader.readPage(chrome);
  }
  return result;
}
function summaryRows() { return `<div class="row"><span>渠道</span><span class="value">${esc(capture.platform)}</span></div><div class="row"><span>对象</span><span class="value">${esc(capture.email || capture.phone || capture.conversation_identity || '未识别')}</span></div><div class="row"><span>范围</span><span class="value">${esc(capture.extraction_scope)}</span></div><div class="row"><span>新增候选</span><span class="value">${capture.messages.length} 条</span></div>`; }

function renderCapture() {
  app.innerHTML = `<section class="panel"><div class="row"><strong>当前沟通</strong><span class="hint">${esc(capture.adapter_version)}</span></div>${summaryRows()}${capture.warnings?.length ? `<div class="warning">⚠ ${capture.warnings.map(esc).join('<br>⚠ ')}</div>` : ''}<h3>原始消息预览</h3><div class="message-list">${capture.messages.map((message) => `<div class="message"><time>${esc(message.time)} · ${esc(message.direction)}</time>${esc(message.text || message.raw_text || '[媒体消息]')}</div>`).join('') || '<span class="muted">没有可保存的消息</span>'}</div></section><section class="panel"><h2>归属与写入</h2><div id="match"></div></section>`;
  match();
}

function exactCustomer(contact, result) {
  return result.customers.find((customer) => customer.id === contact.customer_id) || { id: contact.customer_id, name: contact.customer_name, company: contact.company };
}

function renderAssignmentChooser(box, result = lastMatch) {
  selected = null;
  const models = [];
  (result?.contacts || []).forEach((contact) => models.push({ customer: exactCustomer(contact, result), contact, reason: result.exact_reason || '邮箱或手机号与现有联系人精确一致', confidence: contact.confidence || 'high', danger: result.match_state === 'identity_conflict' }));
  (result?.domain_candidates || []).forEach((candidate) => models.push({ customer: candidate.customer, contact: null, reason: candidate.reason, confidence: candidate.confidence || 'medium', danger: false }));
  (result?.name_candidates || []).forEach((candidate) => models.push({ customer: candidate.customer, contact: candidate.contact, reason: `${candidate.reason}（弱线索，请人工核对）`, confidence: candidate.confidence || 'low', danger: false }));
  const confidenceLabel = { high: '高可信', medium: '中可信', low: '低可信' };
  const candidates = models.map((item, index) => `<button class="candidate ${item.danger ? 'danger' : ''}" data-suggestion="${index}">${esc(customerName(item.customer))}<small>${item.contact ? `${esc(item.contact.name || '未命名联系人')} · ${esc(contactIdentity(item.contact))}` : '公司候选；下一步单独选择联系人'}</small><small class="evidence">${confidenceLabel[item.confidence] || '待核对'} · ${esc(item.reason)}</small></button>`).join('');
  box.innerHTML = `${result?.match_warning ? `<p class="warning">⚠ ${esc(result.match_warning)}</p>` : ''}<div class="assignment-head"><strong>确认沟通归属</strong><span class="confidence">自动结果仅作建议</span></div>${candidates ? `<p class="hint">请先确认一个智能候选，或搜索全部客户。不会自动把沟通写入任何客户。</p>${candidates}` : '<p class="hint">没有可靠的自动候选，请搜索正确客户。</p>'}<h3>搜索全部客户</h3><label>公司或客户名称<input id="customer-search" placeholder="输入至少 2 个字"></label><div id="search-results"></div><button id="new-customer" class="secondary">新建客户和联系人</button><button id="unassigned" class="secondary">暂存为待归属沟通</button>`;
  box.querySelectorAll('[data-suggestion]').forEach((button) => { button.onclick = () => { const item = models[Number(button.dataset.suggestion)]; if (item.contact) { selected = { customer: item.customer, contact: item.contact }; renderEditor(box, item.reason); } else showContactPicker(box, item.customer); }; });
  $('customer-search').oninput = async (event) => {
    const query = event.target.value.trim(); if (query.length < 2) { $('search-results').innerHTML = ''; return; }
    try {
      const response = await api(`/api/customers?search=${encodeURIComponent(query)}&per_page=12`); const customers = response.customers || [];
      $('search-results').innerHTML = customers.length ? customers.map((customer, index) => `<button class="candidate" data-customer-result="${index}">${esc(customerName(customer))}<small>${esc(customer.country || customer.website || '点击后选择联系人')}</small></button>`).join('') : '<p class="hint">没有找到客户，可新建客户。</p>';
      $('search-results').querySelectorAll('[data-customer-result]').forEach((button) => { button.onclick = () => showContactPicker(box, customers[Number(button.dataset.customerResult)]); });
    } catch (error) { $('search-results').innerHTML = `<p class="error">${esc(error.message)}</p>`; }
  };
  $('new-customer').onclick = () => renderNewCustomer(box);
  $('unassigned').onclick = async () => { try { await api('/api/extension/unassigned', { method: 'POST', body: JSON.stringify(capture) }); box.innerHTML = '<p class="success">已暂存到 Inbox 待归属沟通。</p>'; } catch (error) { box.innerHTML = `<p class="error">${esc(error.message)}</p>`; } };
}

async function showContactPicker(box, customer) {
  selected = { customer, contact: null };
  box.innerHTML = `<div class="assignment-head"><strong>${esc(customerName(customer))}</strong><button id="change-customer" class="text-button">更换客户</button></div><p class="hint">正在读取该客户的联系人…</p>`;
  $('change-customer').onclick = () => renderAssignmentChooser(box);
  try {
    const contacts = await api(`/api/customers/${customer.id}/contacts`);
    box.innerHTML = `<div class="assignment-head"><strong>${esc(customerName(customer))}</strong><button id="change-customer" class="text-button">更换客户</button></div><p class="hint">请选择联系人。公司候选不会自动替你锁定联系人。</p>${contacts.map((contact, index) => `<button class="candidate" data-contact-result="${index}">${esc(contact.name || '未命名联系人')}<small>${esc(contactIdentity(contact))}</small></button>`).join('') || '<p class="hint">该客户还没有联系人。</p>'}<button id="no-contact" class="secondary">暂不关联联系人</button><button id="add-contact" class="secondary">在此客户下新建联系人</button><div id="contact-form"></div>`;
    $('change-customer').onclick = () => renderAssignmentChooser(box);
    box.querySelectorAll('[data-contact-result]').forEach((button) => { button.onclick = () => { selected.contact = contacts[Number(button.dataset.contactResult)]; renderEditor(box); }; });
    $('no-contact').onclick = () => renderEditor(box); $('add-contact').onclick = () => renderContactForm(box);
  } catch (error) { box.innerHTML += `<p class="error">${esc(error.message)}</p>`; }
}

function renderContactForm(box) {
  $('contact-form').innerHTML = `<h3>新建联系人</h3><label>联系人姓名<input id="contact-name" value="${esc(capture.conversation_identity)}"></label><label>邮箱<input id="contact-email" value="${esc(capture.email)}"></label><label>手机号<input id="contact-phone" value="${esc(capture.phone)}"></label><button id="contact-save" class="secondary">保存并选择此联系人</button><p id="contact-error" class="error"></p>`;
  $('contact-save').onclick = async () => {
    try {
      const name = $('contact-name').value.trim(); const email = $('contact-email').value.trim(); const phone = $('contact-phone').value.trim();
      const result = await api(`/api/customers/${selected.customer.id}/contacts`, { method: 'POST', body: JSON.stringify({ name, email, phone, whatsapp: capture.channel === 'whatsapp' ? phone : '' }) });
      selected.contact = { id: result.contact_id, name, email, phone }; renderEditor(box);
    } catch (error) { $('contact-error').textContent = error.message; }
  };
}

function renderNewCustomer(box) {
  box.innerHTML = `<h3>新建客户和联系人</h3><p class="hint">公司名称由你确认；系统不会把联系人昵称当成公司名。</p><label>公司名称<input id="new-company"></label><label>联系人姓名<input id="new-name" value="${esc(capture.conversation_identity)}"></label><label>邮箱<input id="new-email" value="${esc(capture.email)}"></label><label>手机号<input id="new-phone" value="${esc(capture.phone)}"></label><button id="create" class="primary">创建后继续预览</button><button id="cancel-create" class="secondary">返回选择已有客户</button><p id="create-error" class="error"></p>`;
  $('cancel-create').onclick = () => renderAssignmentChooser(box);
  $('create').onclick = async () => {
    try {
      const company = $('new-company').value.trim(); const name = $('new-name').value.trim(); const email = $('new-email').value.trim(); const phone = $('new-phone').value.trim();
      const result = await api('/api/customers', { method: 'POST', body: JSON.stringify({ company, name, contacts: [{ name, email, phone, whatsapp: capture.channel === 'whatsapp' ? phone : '' }] }) });
      await showContactPicker(box, { id: result.id, company });
    } catch (error) { $('create-error').textContent = error.message; }
  };
}

async function match() {
  const box = $('match');
  try {
    lastMatch = await api('/api/extension/match', { method: 'POST', body: JSON.stringify({ email: capture.email, phone: capture.phone, name: capture.conversation_identity }) });
    renderAssignmentChooser(box, lastMatch);
  } catch (error) { if (error.status === 401) renderLogin(); else box.innerHTML = `<p class="error">${esc(error.message)}</p>`; }
}

function renderEditor(box, matchReason = '') {
  const original = capture.messages.map((message) => [message.time, message.sender || message.direction, message.text || message.raw_text || '[媒体消息]'].filter(Boolean).join(' · ')).join('\n\n');
  box.innerHTML = `<div class="assignment-card"><div class="row"><span>客户</span><span class="value">${esc(customerName(selected.customer))}</span></div><div class="row"><span>联系人</span><span class="value">${esc(selected.contact?.name || '未关联联系人')}</span></div>${matchReason ? `<p class="evidence">自动判断依据：${esc(matchReason)}。请核对后保存。</p>` : ''}<button id="change-assignment" class="secondary">修改客户或联系人</button></div><label>发生了什么<textarea id="content">${esc(original)}</textarea></label><button id="summarize" class="secondary">AI 帮我整理这段沟通</button><p id="summary-status" class="hint">原文会保留；整理结果只会填入“沟通结果”，保存前仍可编辑。</p><label>沟通结果（可选）<textarea id="result" placeholder="例如：客户询问 MOQ，等待我方确认"></textarea></label><label>当前等待（可选）<textarea id="waiting" placeholder="例如：等待对方确认样品数量"></textarea></label><label>下一步动作（可选）<input id="next" placeholder="填写后必须选择日期"></label><label id="date-label" hidden>下一步日期<input id="date" type="date"></label><p class="hint">原始消息、清理正文、来源 URL 与提取告警会单独保存。</p><button id="save" class="primary">确认存入 Trade OS</button><p id="save-error" class="error"></p>`;
  $('change-assignment').onclick = () => renderAssignmentChooser(box); $('summarize').onclick = summarize; $('next').oninput = () => { $('date-label').hidden = !$('next').value.trim(); }; $('save').onclick = save;
}

async function summarize() {
  const button = $('summarize'); button.disabled = true; $('summary-status').textContent = '正在整理；原文仍然保留…';
  try {
    const response = await api('/api/inbox/analyze-reply', { method: 'POST', body: JSON.stringify({ content: $('content').value, direction: capture.direction || 'auto', customer_id: selected.customer.id, customer_name: customerName(selected.customer) }) }); const analysis = response.analysis || {};
    $('result').value = analysis.summary || $('result').value; if (analysis.needs?.length) $('waiting').value = analysis.needs.join('；'); $('summary-status').textContent = `${analysis.ai_available === false ? '未配置模型，已使用原文保留式摘要。' : '已生成摘要草稿，可继续修改。'}${analysis.key_facts?.length ? ` 关键事实：${analysis.key_facts.join('；')}` : ''}`;
  } catch (_error) { $('summary-status').textContent = '整理暂时不可用，原文仍可直接保存。'; } finally { button.disabled = false; }
}

async function save() {
  const next = $('next').value.trim(); if (next && !$('date').value) { $('save-error').textContent = '填写下一步动作后请选择日期。'; return; } $('save').disabled = true;
  try {
    const result = await api('/api/extension/communications', { method: 'POST', body: JSON.stringify({ customer_id: selected.customer.id, contact_id: selected.contact?.id, channel: capture.channel, source_url: capture.source_url, account: capture.account, conversation_identity: capture.conversation_identity, adapter_version: capture.adapter_version, extraction_scope: capture.extraction_scope, warnings: capture.warnings, messages: capture.messages, content: $('content').value, result: $('result').value, waiting: $('waiting').value, next_plan: next, follow_date: (capture.end_time || '').slice(0, 10), direction: capture.direction }) });
    app.innerHTML = `<section class="panel"><p class="success">已写入 ${result.new_message_count || 0} 条新增消息。</p><p class="muted">原始来源与指纹已保存，可在客户时间线核对。</p><div class="toolbar"><button id="open-customer" class="primary">打开客户</button><button id="undo" class="secondary">撤销本次写入</button></div><p id="done" class="hint"></p></section>`;
    $('open-customer').onclick = () => chrome.tabs.create({ url: `${apiBase}/?page=customers&customer=${selected.customer.id}` }); $('undo').onclick = async () => { try { await api(`/api/undo/${result.undo_token}`, { method: 'POST' }); $('done').textContent = '已撤销本次写入。'; } catch (error) { $('done').textContent = error.message; } };
  } catch (error) { $('save-error').textContent = error.message; $('save').disabled = false; }
}

function diagnosticItem(ok, label, detail) {
  return `<li class="${ok ? 'diagnostic-pass' : 'diagnostic-fail'}">${ok ? '✓' : '×'} ${esc(label)}<small>${esc(detail)}</small></li>`;
}

async function showDiagnostics() {
  app.innerHTML = '<section class="state"><div class="pulse"></div><p>正在执行当前页面验收…</p></section>';
  let pageResult;
  let authResult;
  let pageError = '';
  let authError = '';
  try { pageResult = await readPage(); } catch (error) { pageError = error.message; }
  try { authResult = await api('/api/auth/me'); } catch (error) { authError = error.message; }
  const diagnostics = pageResult?.diagnostics || { host: '', frame_count: 0, frames: [] };
  const messageCount = pageResult?.data?.messages?.length || 0;
  const manifest = chrome.runtime.getManifest?.() || { version: '未知' };
  const checks = [
    { ok: Boolean(pageResult?.ok), label: '当前页面可读取', detail: pageResult?.ok ? `${diagnostics.host || '已授权站点'} · ${diagnostics.successful_frame_count || 0}/${diagnostics.frame_count || 0} 个区域可通信` : (pageResult?.error || pageError || '读取失败') },
    { ok: messageCount > 0, label: '已取得可保存消息', detail: messageCount ? `当前已加载 ${messageCount} 条` : '请打开具体邮件线程或 WhatsApp 单聊，并等待消息加载完成' },
    { ok: !authError && Boolean(authResult?.logged_in), label: 'Trade OS 已登录', detail: !authError && authResult?.logged_in ? '后端接口可用' : (authError || '请在侧栏重新登录') },
    { ok: true, label: '归属可人工修改', detail: '自动候选仅作建议；保存前可更换客户和联系人' },
  ];
  if (diagnostics.recovered_frame_count) checks.splice(1, 0, { ok: true, label: '内容脚本已自动恢复', detail: `已自动注入并重试 ${diagnostics.recovered_frame_count} 个页面区域` });
  const report = {
    schema: 'trade-os-capture-diagnostic-v1',
    extension_version: manifest.version,
    page_host: diagnostics.host || '',
    page_supported: Boolean(diagnostics.supported),
    frame_count: diagnostics.frame_count || 0,
    successful_frame_count: diagnostics.successful_frame_count || 0,
    recovered_frame_count: diagnostics.recovered_frame_count || 0,
    frame_results: (diagnostics.frames || []).map((frame) => ({ frame_id: frame.frame_id, channel: frame.channel, message_count: frame.message_count, recovered: frame.recovered, error: frame.error || '' })),
    extracted_message_count: messageCount,
    backend_logged_in: Boolean(authResult?.logged_in),
    result: pageResult?.ok ? 'pass' : 'fail',
  };
  app.innerHTML = `<section class="panel"><h2>当前页验收</h2><p class="hint">不包含邮件正文、邮箱或手机号；复制后可直接用于定位问题。</p><ul class="diagnostic-list">${checks.map((check) => diagnosticItem(check.ok, check.label, check.detail)).join('')}</ul><label>匿名诊断报告<textarea id="diagnostic-copy" class="diagnostic-copy" readonly>${esc(JSON.stringify(report, null, 2))}</textarea></label><button id="copy-diagnostics" class="secondary">复制诊断报告</button><button id="back-to-capture" class="primary">返回沟通采集</button><p id="diagnostic-status" class="hint"></p></section>`;
  $('copy-diagnostics').onclick = async () => {
    const text = $('diagnostic-copy').value;
    try {
      if (navigator.clipboard?.writeText) await navigator.clipboard.writeText(text);
      else { $('diagnostic-copy').select(); document.execCommand('copy'); }
      $('diagnostic-status').textContent = '已复制匿名诊断报告。';
    } catch (_error) {
      $('diagnostic-copy').select();
      $('diagnostic-status').textContent = '请手工复制上方报告。';
    }
  };
  $('back-to-capture').onclick = load;
}

async function load() {
  app.innerHTML = '<section class="state"><div class="pulse"></div><p>正在识别当前页面…</p></section>';
  try {
    const me = await api('/api/auth/me'); if (!me.logged_in) { renderLogin(); return; } const result = await readPage();
    if (!result.ok) { app.innerHTML = `<section class="panel"><h2>不支持当前页面</h2><p class="error">${esc(result.error)}</p><button class="secondary" id="retry">重新读取</button></section>`; $('retry').onclick = load; return; }
    capture = result.data; if (!capture.messages.length) { app.innerHTML = '<section class="panel"><h2>没有新增内容</h2><p class="muted">当前页面未提取到可确认的消息。请打开具体邮件线程或 WhatsApp 单聊后重试。</p></section>'; return; } renderCapture();
  } catch (error) { if (error.status === 401) renderLogin(); else app.innerHTML = `<section class="panel"><p class="error">${esc(error.message)}</p><button class="secondary" id="retry">重试</button></section>`; }
}

getSettings().then(load); $('refresh').onclick = load; $('diagnostics').onclick = showDiagnostics;
