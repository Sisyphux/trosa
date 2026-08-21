const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { JSDOM } = require('jsdom');

const extensionRoot = path.resolve(__dirname, '..');
const fixture = (name) => fs.readFileSync(path.join(__dirname, 'fixtures', name), 'utf8');
const source = (name) => fs.readFileSync(path.join(extensionRoot, name), 'utf8');

function adapterDom(html, url, adapter, referrer) {
  const dom = new JSDOM(html, { url, referrer, runScripts: 'outside-only', pretendToBeVisual: true });
  dom.window.eval(source('adapters/common.js'));
  dom.window.eval(source(`adapters/${adapter}.js`));
  return dom;
}

function invokeContentScript(dom) {
  const listeners = [];
  dom.window.chrome = { runtime: { onMessage: { addListener(callback) { listeners.push(callback); } } } };
  dom.window.eval(source('content.js'));
  dom.window.eval(source('content.js'));
  assert.equal(listeners.length, 1, 'recovery injection must not install duplicate message listeners');
  let response;
  listeners[0]({ type: 'tradeos-extract' }, {}, (payload) => { response = payload; });
  return response;
}

{
  const dom = adapterDom(fixture('netease-enterprise.html'), 'https://mailh.qiye.163.com/read/1', 'netease');
  const adapter = dom.window.TradeOSAdapters.netease;
  assert.equal(adapter.detectPage(), true);
  const result = adapter.extractLoadedMessages();
  assert.equal(result.messages.length, 1);
  assert.match(result.messages[0].text, /revised lead time/);
  assert.equal(result.email, 'purchasing@factory.example');
  assert.equal(invokeContentScript(dom).data.messages.length, 1);
}

{
  const dom = adapterDom(fixture('netease-enterprise.html'), 'about:blank', 'netease', 'https://mailh.qiye.163.com/static/sirius-web/#mailbox');
  const adapter = dom.window.TradeOSAdapters.netease;
  assert.equal(adapter.detectPage(), true, 'about:blank mail frame should inherit the parent NetEase origin');
  assert.equal(adapter.extractLoadedMessages().messages.length, 1);
}

{
  const dom = adapterDom('<div id="mail-host"></div>', 'https://mailh.qiye.163.com/static/sirius-web/#mailbox', 'netease');
  const shadow = dom.window.document.getElementById('mail-host').attachShadow({ mode: 'open' });
  shadow.innerHTML = '<section data-message-id="shadow-mail"><span class="sender">shadowbuyer@example.com</span><time>2026-08-10 11:00</time><div data-message-body>Message stored inside an open Shadow DOM.</div></section>';
  const result = dom.window.TradeOSAdapters.netease.extractLoadedMessages();
  assert.equal(result.messages.length, 1, 'NetEase reader must scan open Shadow DOM');
  assert.equal(result.email, 'shadowbuyer@example.com');
  assert.match(result.messages[0].text, /Shadow DOM/);
}

{
  const dom = adapterDom('<div class="message-card">添加邮箱 高效管理添加您的工作邮箱，体验高效邮件管理工具</div>', 'https://mailh.qiye.163.com/static/sirius-web/#mailbox', 'netease');
  assert.equal(dom.window.TradeOSAdapters.netease.extractLoadedMessages().messages.length, 0);
}

{
  const dom = adapterDom(fixture('whatsapp-single.html'), 'https://web.whatsapp.com/', 'whatsapp');
  const result = dom.window.TradeOSAdapters.whatsapp.extractLoadedMessages();
  assert.equal(result.messages.length, 1);
  assert.equal(result.phone, '+8613800000000');
  assert.equal(result.phone_source, 'conversation_jid');
  assert.equal(result.is_group, false);
  const response = invokeContentScript(dom);
  assert.equal(response.ok, true);
  assert.equal(response.data.phone, '+8613800000000');
}

{
  const dom = adapterDom('<header><span title="Fallback Buyer">Fallback Buyer</span></header><div data-id="false_8613900000000@c.us_ABC"><span dir="auto">Message found from a WhatsApp data-id container.</span></div>', 'https://web.whatsapp.com/', 'whatsapp');
  const result = dom.window.TradeOSAdapters.whatsapp.extractLoadedMessages();
  assert.equal(result.phone, '+8613900000000');
  assert.equal(result.messages.length, 1, 'WhatsApp data-id fallback must capture a message without msg-container');
  assert.match(result.messages[0].text, /data-id container/);
}

(async () => {
  const dom = new JSDOM('', { runScripts: 'outside-only' });
  dom.window.eval(source('capture-utils.js'));
  const merged = dom.window.TradeOSCaptureUtils.mergeFrameCaptures([
    { frameId: 0, data: { channel: 'netease', email: 'buyer@example.com', messages: [], warnings: [] } },
    { frameId: 7, data: { channel: 'netease', messages: [{ fingerprint: 'm1', text: 'Mail body', direction: 'inbound' }], warnings: [] } },
  ]);
  assert.equal(merged.ok, true);
  assert.equal(merged.data.email, 'buyer@example.com');
  assert.equal(merged.data.messages.length, 1);
  assert.equal(merged.data.messages[0].text, 'Mail body');

  dom.window.eval(source('frame-reader.js'));
  let whatsappFrameEnumeration = 0;
  const whatsappApi = {
    runtime: { lastError: null },
    tabs: {
      query(_query, callback) { callback([{ id: 21, url: 'https://web.whatsapp.com/' }]); },
      sendMessage(_tabId, _message, callback) { callback({ ok: true, data: { channel: 'whatsapp', phone: '+8613800000000', messages: [{ fingerprint: 'wa1', text: 'hello' }] } }); },
    },
    webNavigation: { getAllFrames() { whatsappFrameEnumeration += 1; } },
  };
  const whatsappDirect = await dom.window.TradeOSFrameReader.readPage(whatsappApi);
  assert.equal(whatsappDirect.data.phone, '+8613800000000');
  assert.equal(whatsappDirect.diagnostics.successful_frame_count, 1);
  assert.equal(whatsappDirect.diagnostics.frame_count, 1);
  assert.equal(whatsappFrameEnumeration, 0, 'WhatsApp success must not enter NetEase frame enumeration');

  const mailApi = {
    runtime: { lastError: null },
    tabs: {
      query(_query, callback) { callback([{ id: 22, url: 'https://mailh.qiye.163.com/static/sirius-web/#mailbox' }]); },
      sendMessage(_tabId, _message, options, callback) {
        if (typeof options === 'function') options({ ok: true, data: { channel: 'netease', email: 'buyer@example.com', messages: [] } });
        else callback({ ok: true, data: { channel: 'netease', messages: [{ fingerprint: 'mail1', text: 'iframe mail' }] } });
      },
    },
    webNavigation: { getAllFrames(_details, callback) { callback([{ frameId: 0 }, { frameId: 9 }]); } },
  };
  const mailFrames = await dom.window.TradeOSFrameReader.readPage(mailApi);
  assert.equal(mailFrames.data.email, 'buyer@example.com');
  assert.equal(mailFrames.data.messages[0].text, 'iframe mail');
  assert.equal(mailFrames.diagnostics.frame_count, 2);
  assert.equal(mailFrames.diagnostics.successful_frame_count, 2);

  let recoverySendCount = 0;
  let injectedTarget;
  const recoveredApi = {
    runtime: { lastError: null },
    tabs: {
      query(_query, callback) { callback([{ id: 23, url: 'https://web.whatsapp.com/' }]); },
      sendMessage(_tabId, _message, callback) {
        recoverySendCount += 1;
        if (recoverySendCount === 1) {
          recoveredApi.runtime.lastError = { message: '无法建立连接。接收端不存在。' };
          callback();
          recoveredApi.runtime.lastError = null;
        } else {
          callback({ ok: true, data: { channel: 'whatsapp', phone: '+8613800000000', messages: [{ fingerprint: 'wa2', text: 'recovered' }] } });
        }
      },
    },
    scripting: {
      executeScript(details, callback) { injectedTarget = details.target; callback([]); },
    },
    webNavigation: { getAllFrames(_details, callback) { callback([{ frameId: 0 }]); } },
  };
  const recovered = await dom.window.TradeOSFrameReader.readPage(recoveredApi);
  assert.equal(recovered.ok, true);
  assert.equal(recovered.data.messages[0].text, 'recovered');
  assert.equal(recovered.diagnostics.recovered_frame_count, 1);
  assert.equal(injectedTarget.tabId, 23);
  assert.equal(injectedTarget.frameIds.length, 1);
  assert.equal(injectedTarget.frameIds[0], 0);
  assert.equal(recoverySendCount, 2, 'missing receiver should inject once and retry once');

  let mailChildReads = 0;
  let injectedMailFrame = -1;
  const recoveredMailApi = {
    runtime: { lastError: null },
    tabs: {
      query(_query, callback) { callback([{ id: 25, url: 'https://mailh.qiye.163.com/static/sirius-web/#mailbox' }]); },
      sendMessage(_tabId, _message, options, callback) {
        if (typeof options === 'function') {
          options({ ok: true, data: { channel: 'netease', email: 'buyer@example.com', messages: [] } });
          return;
        }
        mailChildReads += 1;
        if (mailChildReads === 1) {
          recoveredMailApi.runtime.lastError = { message: 'Could not establish connection. Receiving end does not exist.' };
          callback();
          recoveredMailApi.runtime.lastError = null;
        } else {
          callback({ ok: true, data: { channel: 'netease', messages: [{ fingerprint: 'mail2', text: 'recovered iframe mail' }] } });
        }
      },
    },
    scripting: {
      executeScript(details, callback) { injectedMailFrame = details.target.frameIds[0]; callback([]); },
    },
    webNavigation: { getAllFrames(_details, callback) { callback([{ frameId: 0 }, { frameId: 5 }]); } },
  };
  const recoveredMail = await dom.window.TradeOSFrameReader.readPage(recoveredMailApi);
  assert.equal(recoveredMail.ok, true);
  assert.equal(recoveredMail.data.email, 'buyer@example.com');
  assert.equal(recoveredMail.data.messages[0].text, 'recovered iframe mail');
  assert.equal(injectedMailFrame, 5);
  assert.equal(mailChildReads, 2, 'missing NetEase child receiver should inject and retry');
  assert.equal(recoveredMail.diagnostics.recovered_frame_count, 1);

  const unsupportedApi = {
    runtime: { lastError: null },
    tabs: { query(_query, callback) { callback([{ id: 24, url: 'https://example.com/' }]); } },
  };
  const unsupported = await dom.window.TradeOSFrameReader.readPage(unsupportedApi);
  assert.equal(unsupported.ok, false);
  assert.match(unsupported.error, /不在支持范围/);
  assert.equal(unsupported.diagnostics.supported, false);
})().then(() => {
  console.log('browser-extension runtime adapters, content messages and frame routing: OK');
}).catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
