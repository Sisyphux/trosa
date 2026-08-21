(function () {
  const C = window.TradeOSAdapterCommon;
  const phoneFromCurrentChat = (root, headerText) => {
    const visible = (headerText.match(/\+\s*[0-9][0-9 ()-]{6,}/) || [''])[0];
    if (visible) return { phone: C.normalizePhone(visible), source: 'header', isGroup: false };
    let phone = ''; let isGroup = false;
    const nodes = C.queryAllDeep(root, '[data-id],[data-chat-id],[data-jid],[data-peer-id],[title]');
    for (const node of nodes) {
      const values = ['data-id','data-chat-id','data-jid','data-peer-id','title'].map(name=>node.getAttribute(name)||'');
      for (const value of values) {
        if (/@g\.us(?:_|$)/i.test(value)) isGroup = true;
        const jid = value.match(/(\d{7,15})@(?:c\.us|s\.whatsapp\.net)(?:_|$)/i);
        if (jid) { phone = `+${jid[1]}`; break; }
        const waLink = value.match(/(?:wa\.me\/|phone=)(\d{7,15})/i);
        if (waLink) { phone = `+${waLink[1]}`; break; }
      }
      if (phone) break;
    }
    return { phone, source: phone ? 'conversation_jid' : '', isGroup };
  };
  function extract() {
    const root = document;
    const header = C.queryOneDeep(root, 'header') || root.body;
    const headerText = C.clean(header?.innerText || '');
    const identity = phoneFromCurrentChat(root, headerText);
    const phone = identity.phone;
    const name = C.clean(C.queryOneDeep(header, 'span[title], [data-testid="conversation-info-header-chat-title"]')?.getAttribute('title') || headerText.split('\n')[0]);
    const nodes = [...new Set([
      ...C.queryAllDeep(root, '[data-pre-plain-text], [data-testid="msg-container"]'),
      ...C.queryAllDeep(root, '[data-id]').filter((node) => /^(?:true|false)_.+@(?:c\.us|s\.whatsapp\.net|g\.us)/i.test(node.getAttribute('data-id') || '')),
    ])];
    const seenFingerprints = new Set();
    const messages = nodes.map((node) => {
      const pre = node.getAttribute('data-pre-plain-text') || C.queryOneDeep(node, '[data-pre-plain-text]')?.getAttribute('data-pre-plain-text') || '';
      const time = (pre.match(/\[(.*?)\]/) || [,''])[1] || C.clean(node.querySelector('time')?.textContent);
      const text = C.clean(C.queryOneDeep(node, '[data-testid="selectable-text"], span[dir="auto"]')?.textContent || node.innerText);
      const messageNode = node.closest('[data-id]') || node;
      const messageId = messageNode.getAttribute('data-id') || '';
      const outgoing = /^true_/i.test(messageId) || C.queryOneDeep(node, '[data-icon="msg-check"], [data-icon="msg-dblcheck"], [data-icon="msg-time"]') || node.closest('.message-out');
      const media = C.queryOneDeep(node, '[data-testid*="image"], [data-testid*="video"], [data-testid*="audio"], [data-testid*="document"]');
      const item = { message_id: messageId, time, direction: outgoing ? 'outbound' : 'inbound', text: text || (media ? `[${media.getAttribute('data-testid') || '媒体消息'}]` : ''), raw_text: node.innerText || '', type: media ? 'media' : 'text', sender: '' };
      item.fingerprint = C.fingerprint(item); return item;
    }).filter((item) => {
      if (!item.text && item.type !== 'media') return false;
      if (seenFingerprints.has(item.fingerprint)) return false;
      seenFingerprints.add(item.fingerprint);
      return true;
    });
    const warnings = [];
    if (!phone) warnings.push('未可靠取得手机号，只能按显示名称提供候选，不能自动归属。');
    const isGroup = identity.isGroup || headerText.includes('群') || /group/i.test(headerText);
    if (isGroup) warnings.push('群聊第一版不会自动归属客户，请手工选择目标客户。');
    if (!messages.length) warnings.push('没有找到当前页面已加载的消息，可能需要先打开单聊或页面结构已变化。');
    return { channel: 'whatsapp', platform: 'WhatsApp Web', phone: C.normalizePhone(phone), phone_source: identity.source, conversation_identity: name, title: name, is_group: isGroup, messages, extraction_scope: `只读取当前页面已加载的消息，共 ${messages.length} 条`, warnings, adapter_version: 'whatsapp-0.3', source_url: location.href, direction: messages.some((m) => m.direction === 'outbound') && messages.some((m) => m.direction === 'inbound') ? 'two_way' : (messages[0]?.direction || 'unknown') };
  }
  window.TradeOSAdapters = window.TradeOSAdapters || {};
  window.TradeOSAdapters.whatsapp = { detectPage: () => location.hostname === 'web.whatsapp.com', detectAccount: extract, extractConversationIdentity: extract, extractLoadedMessages: extract };
})();
