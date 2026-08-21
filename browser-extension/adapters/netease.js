(function () {
  const C = window.TradeOSAdapterCommon;
  const SELECTORS = {
    subjects: ['[class*="subject"]', '[class*="Subject"]', '[data-subject]'],
    messages: ['[data-message-id]', '[data-mid]', '.mailItem', '.message-item', '[class*="mail-detail"]', '[class*="mailDetail"]'],
    body: ['[data-message-body]', '.mail_content', '.mailcontent', '[class*="mail-content"]', '[class*="mailBody"]', '[class*="mail-body"]'],
    participants: ['[data-email]', '[data-address]', '[class*="sender"]', '[class*="from"]', '[class*="to"]', '[class*="cc"]']
  };
  const firstText = (root, selectors) => {
    for (const selector of selectors) { const node = C.queryOneDeep(root, selector); if (node) return C.clean(node.textContent); }
    return '';
  };
  const emailFromText = (text) => (String(text).match(/[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}/ig) || []).map(C.normalizeEmail);
  const effectiveHostname = () => {
    if (location.hostname) return location.hostname;
    try { return new URL(document.referrer).hostname; } catch (_) { return ''; }
  };
  const UI_NOISE = /添加邮箱|高效管理|工作邮箱|邮件管理工具|扫码登录|下载客户端/;
  const looksLikeMessage = (node, body) => {
    const text = C.clean(node.innerText || node.textContent || '');
    if (!body || body.length < 8 || UI_NOISE.test(text)) return false;
    if (node.hasAttribute('data-message-id') || node.hasAttribute('data-mid') || C.queryOneDeep(node, '[data-message-body]')) return true;
    const hasAddress = emailFromText(text).length > 0;
    const hasHeader = /发件人|收件人|抄送|主题|From\s*:|To\s*:|Subject\s*:/i.test(text);
    const hasTime = Boolean(C.queryOneDeep(node, 'time,[datetime],[class*="date"],[class*="time"]'));
    return body.length >= 20 && (hasAddress || hasHeader) && hasTime;
  };
  function extract() {
    const root = document;
    const roots = C.accessibleDocuments(root);
    const allText = roots.map((item) => item.body?.innerText || item.body?.textContent || '').join('\n');
    const emails = [...new Set(emailFromText(allText))];
    const account = emailFromText(roots.map((item) => firstText(item, ['[class*="account"]', '[class*="user"]', '.nui-userinfo'])).join(' '))[0] || '';
    const subject = roots.map((item) => firstText(item, SELECTORS.subjects)).find(Boolean) || document.title.replace(/[-|].*$/, '').trim();
    const candidates = [];
    for (const scanRoot of roots) for (const selector of SELECTORS.messages) {
      C.queryAllDeep(scanRoot, selector).forEach((node) => {
        const nodeText = node.innerText || node.textContent || '';
        const body = firstText(node, SELECTORS.body) || C.clean(nodeText);
        if (!looksLikeMessage(node, body) || candidates.some((item) => item.node === node)) return;
        const time = firstText(node, ['time', '[datetime]', '[class*="date"]', '[class*="time"]']);
        const sender = emailFromText(nodeText)[0] || '';
        const message = { message_id: node.getAttribute('data-message-id') || node.getAttribute('data-mid') || '', time, sender, direction: sender && sender === account ? 'outbound' : 'inbound', text: body, raw_text: nodeText, type: 'email' };
        message.fingerprint = C.fingerprint(message); candidates.push({ node, ...message });
      });
      if (candidates.length) break;
    }
    const messageEmails = candidates.map((item) => item.sender).filter(Boolean);
    const otherEmails = [...new Set([...messageEmails, ...emails])].filter((email) => email !== account);
    if (!candidates.length) {
      const fallbackRoots = roots;
      const frameBody = fallbackRoots.map((item) => item.body).find((bodyNode) => {
        const text = C.clean(bodyNode?.innerText || bodyNode?.textContent || '');
        return text.length >= 40 && !UI_NOISE.test(text) && (/发件人|收件人|From\s*:|To\s*:/i.test(text) || emailFromText(text).length > 0);
      });
      if (frameBody) { const rawBody = frameBody.innerText || frameBody.textContent || ''; const body = C.clean(rawBody); const message = { time: '', sender: emailFromText(body)[0] || otherEmails[0] || '', direction: 'unknown', text: body, raw_text: rawBody, type: 'email' }; message.fingerprint = C.fingerprint(message); candidates.push(message); }
    }
    const warnings = [];
    if (!otherEmails.length) warnings.push('未可靠取得对方邮箱，请手工选择客户；不会根据显示名称自动归属。');
    if (!candidates.length) warnings.push('没有找到当前打开线程的正文，页面结构可能已变化。');
    if (C.queryAllDeep(root, 'iframe').length) warnings.push('页面包含 iframe，当前结果仅来自可访问的页面内容。');
    const times = candidates.map((item) => item.time).filter(Boolean).sort();
    return { channel: 'netease', platform: '网易邮箱', account, email: otherEmails[0] || '', conversation_identity: subject, title: subject, participants: otherEmails, messages: candidates.map(({ node, ...item }) => item), extraction_scope: `当前页面线程，共 ${candidates.length} 个可识别邮件`, warnings, adapter_version: 'netease-0.5', source_url: location.href, start_time: times[0] || '', end_time: times[times.length - 1] || '', direction: candidates.some((m) => m.direction === 'outbound') && candidates.some((m) => m.direction === 'inbound') ? 'two_way' : (candidates[0]?.direction || 'unknown') };
  }
  window.TradeOSAdapters = window.TradeOSAdapters || {};
  window.TradeOSAdapters.netease = { detectPage: () => /(^|\.)(mail\.(163|126)\.com|yeah\.net|qiye\.163\.com)$/i.test(effectiveHostname()), detectAccount: extract, extractConversationIdentity: extract, extractLoadedMessages: extract };
})();
