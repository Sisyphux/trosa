(function () {
  function mergeFrameCaptures(results) {
    const captures = (results || []).filter((item) => item?.data);
    if (!captures.length) {
      const details = [...new Set((results || []).map((item) => item?.error).filter(Boolean))].join('；');
      return { ok: false, error: details || '当前页面不支持读取，或内容脚本尚未加载。请刷新邮箱/WhatsApp 页面后重试。' };
    }
    const scored = [...captures].sort((a, b) => {
      const messageDelta = (b.data.messages || []).length - (a.data.messages || []).length;
      return messageDelta || JSON.stringify(b.data.messages || []).length - JSON.stringify(a.data.messages || []).length;
    });
    const best = scored[0].data;
    const top = captures.find((item) => item.frameId === 0)?.data || {};
    const allMessages = [];
    const seen = new Set();
    captures.forEach((item) => (item.data.messages || []).forEach((message) => {
      const key = message.fingerprint || JSON.stringify(message);
      if (!seen.has(key)) { seen.add(key); allMessages.push(message); }
    }));
    const warnings = [...new Set(captures.flatMap((item) => item.data.warnings || [])
      .filter((warning) => !warning.includes('当前结果仅来自可访问的页面内容')))];
    if (captures.length > 1) warnings.push(`已检查当前页面的 ${captures.length} 个可访问区域。`);
    const directions = new Set(allMessages.map((item) => item.direction).filter(Boolean));
    const direction = directions.has('inbound') && directions.has('outbound') ? 'two_way' : (best.direction || top.direction || 'unknown');
    return { ok: true, data: {
      ...top, ...best,
      email: best.email || top.email || captures.map((item) => item.data.email).find(Boolean) || '',
      phone: best.phone || top.phone || captures.map((item) => item.data.phone).find(Boolean) || '',
      account: top.account || best.account || '',
      source_url: top.source_url || best.source_url,
      messages: allMessages, warnings, direction,
      extraction_scope: `当前页面已加载范围，共 ${allMessages.length} 条可识别消息`,
    }};
  }
  window.TradeOSCaptureUtils = { mergeFrameCaptures };
})();
