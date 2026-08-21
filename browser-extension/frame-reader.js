(function () {
  const CONTENT_SCRIPT_FILES = [
    'adapters/common.js',
    'adapters/netease.js',
    'adapters/whatsapp.js',
    'content.js',
  ];
  const SUPPORTED_PAGE = /^https:\/\/(?:mail\.(?:163\.com|126\.com|yeah\.net)|(?:[^/]+\.)*qiye\.163\.com|web\.whatsapp\.com)(?:[/:]|$)/i;

  function queryActiveTab(api) {
    return new Promise((resolve) => api.tabs.query({ active: true, currentWindow: true }, (tabs) => resolve(tabs?.[0] || null)));
  }
  function isMissingReceiver(error) {
    return /Receiving end does not exist|Could not establish connection|接收端不存在|无法建立连接/i.test(error || '');
  }
  function injectFrame(api, tabId, frameId) {
    return new Promise((resolve) => {
      if (!api.scripting?.executeScript) {
        resolve({ ok: false, error: '浏览器不支持自动加载内容脚本。' });
        return;
      }
      api.scripting.executeScript({
        target: { tabId, frameIds: [frameId] },
        files: CONTENT_SCRIPT_FILES,
      }, () => {
        const error = api.runtime.lastError;
        resolve(error ? { ok: false, error: error.message } : { ok: true });
      });
    });
  }
  async function readFrameWithRecovery(api, tabId, frameId, useDefaultTarget = false) {
    let result = await readFrame(api, tabId, frameId, useDefaultTarget);
    if (!isMissingReceiver(result.error)) return result;
    const injected = await injectFrame(api, tabId, frameId);
    if (!injected.ok) return { frameId, error: `自动加载采集脚本失败：${injected.error}`, recovery: injected };
    result = await readFrame(api, tabId, frameId, useDefaultTarget);
    result.recovery = injected;
    return result;
  }
  function readFrame(api, tabId, frameId, useDefaultTarget = false) {
    return new Promise((resolve) => {
      const callback = (result) => {
        const error = api.runtime.lastError;
        if (error) resolve({ frameId, error: error.message });
        else if (result?.ok) resolve({ frameId, data: result.data });
        else resolve({ frameId, error: result?.error || '页面没有返回提取结果' });
      };
      if (useDefaultTarget) api.tabs.sendMessage(tabId, { type: 'tradeos-extract' }, callback);
      else api.tabs.sendMessage(tabId, { type: 'tradeos-extract' }, { frameId }, callback);
    });
  }
  function listFrames(api, tabId) {
    return new Promise((resolve) => {
      if (!api.webNavigation?.getAllFrames) { resolve([{ frameId: 0 }]); return; }
      api.webNavigation.getAllFrames({ tabId }, (frames) => {
        if (api.runtime.lastError || !frames?.length) resolve([{ frameId: 0 }]);
        else resolve(frames);
      });
    });
  }
  function diagnosticFrame(result) {
    return {
      frame_id: result.frameId,
      channel: result.data?.channel || '',
      message_count: result.data?.messages?.length || 0,
      recovered: Boolean(result.recovery?.ok),
      error: result.error || '',
    };
  }
  function withDiagnostics(result, tab, frameResults) {
    return {
      ...result,
      diagnostics: {
        host: (() => { try { return new URL(tab.url).hostname; } catch (_) { return ''; } })(),
        supported: SUPPORTED_PAGE.test(tab.url || ''),
        frame_count: frameResults.length,
        frames: frameResults.map(diagnosticFrame),
        successful_frame_count: frameResults.filter((frame) => frame.data).length,
        recovered_frame_count: frameResults.filter((frame) => frame.recovery?.ok).length,
      },
    };
  }
  async function readPage(api) {
    const tab = await queryActiveTab(api);
    if (!tab?.id) throw new Error('无法取得当前标签页。');
    if (!SUPPORTED_PAGE.test(tab.url || '')) {
      return withDiagnostics({ ok: false, error: `当前标签页不在支持范围内：${tab.url || '未知地址'}` }, tab, []);
    }
    const top = await readFrameWithRecovery(api, tab.id, 0, true);
    if (top.data?.channel === 'whatsapp' && (top.data.messages || []).length) {
      return withDiagnostics({ ok: true, data: top.data }, tab, [top]);
    }
    const frames = await listFrames(api, tab.id);
    const children = await Promise.all(frames.filter((frame) => frame.frameId !== 0)
      .map((frame) => readFrameWithRecovery(api, tab.id, frame.frameId)));
    return withDiagnostics(window.TradeOSCaptureUtils.mergeFrameCaptures([top, ...children]), tab, [top, ...children]);
  }
  window.TradeOSFrameReader = { readPage };
})();
