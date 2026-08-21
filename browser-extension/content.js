(function () {
  if (window.__tradeOSContentListenerInstalled) return;
  window.__tradeOSContentListenerInstalled = true;
  function activeAdapter() {
    return Object.values(window.TradeOSAdapters || {}).find((adapter) => adapter.detectPage());
  }
  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (message?.type !== 'tradeos-extract') return;
    try {
      const adapter = activeAdapter();
      if (!adapter) return sendResponse({ ok: false, error: '当前页面不在支持范围内。' });
      sendResponse({ ok: true, data: adapter.extractLoadedMessages() });
    } catch (error) { sendResponse({ ok: false, error: `提取失败：${error.message}` }); }
    return true;
  });
})();
