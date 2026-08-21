const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const puppeteer = require('puppeteer-core');

const fixtureDir = path.join(__dirname, 'fixtures');
const fixture = (name) => fs.readFileSync(path.join(fixtureDir, name), 'utf8');

async function extensionWorker(browser) {
  let target = browser.targets().find((item) => item.type() === 'service_worker' && item.url().endsWith('/background.js'));
  if (!target) {
    const extensionsPage = await browser.newPage();
    await extensionsPage.goto('edge://extensions/');
    await new Promise((resolve) => setTimeout(resolve, 700));
    const installed = await extensionsPage.evaluate(() => {
      const manager = document.querySelector('extensions-manager');
      const list = manager?.shadowRoot?.querySelector('extensions-item-list');
      return [...(list?.shadowRoot?.querySelectorAll('extensions-item') || [])].map((item) => ({
        id: item.id,
        name: item.shadowRoot?.querySelector('#name')?.textContent?.trim() || '',
      }));
    });
    const extension = installed.find((item) => item.name === 'Trade OS 沟通采集');
    assert.ok(extension, `Trade OS extension was not present in Edge: ${JSON.stringify(installed)}`);
    await extensionsPage.goto(`chrome-extension://${extension.id}/sidepanel.html`);
    await new Promise((resolve) => setTimeout(resolve, 500));
    target = browser.targets().find((item) => item.type() === 'service_worker' && item.url().endsWith('/background.js'));
  }
  assert.ok(target, 'Trade OS extension service worker did not wake after opening its side panel');
  const worker = await target.worker();
  const manifest = await worker.evaluate(() => chrome.runtime.getManifest());
  assert.equal(manifest.name, 'Trade OS 沟通采集');
  assert.ok(manifest.permissions.includes('webNavigation'));
  return worker;
}

async function interceptedPage(browser, url, html) {
  const page = await browser.newPage();
  await page.setRequestInterception(true);
  page.on('request', (request) => {
    if (request.isNavigationRequest() && request.frame() === page.mainFrame()) {
      request.respond({ status: 200, contentType: 'text/html; charset=utf-8', body: html });
    } else {
      request.abort();
    }
  });
  await page.goto(url, { waitUntil: 'domcontentloaded' });
  await new Promise((resolve) => setTimeout(resolve, 700));
  return page;
}

async function extractFromTab(worker, urlPattern) {
  return worker.evaluate(async (pattern) => {
    const tabs = await chrome.tabs.query({ url: pattern });
    if (!tabs.length) throw new Error(`No test tab for ${pattern}`);
    const frames = await chrome.webNavigation.getAllFrames({ tabId: tabs[0].id });
    const responses = [];
    for (const frame of frames) {
      try {
        responses.push({ frameId: frame.frameId, response: await chrome.tabs.sendMessage(tabs[0].id, { type: 'tradeos-extract' }, { frameId: frame.frameId }) });
      } catch (error) {
        responses.push({ frameId: frame.frameId, error: error.message });
      }
    }
    return responses;
  }, urlPattern);
}

(async () => {
  const browser = await puppeteer.connect({ browserURL: 'http://127.0.0.1:9333', defaultViewport: null });
  const worker = await extensionWorker(browser);

  const whatsappPage = await interceptedPage(browser, 'https://web.whatsapp.com/', fixture('whatsapp-single.html'));
  const whatsappFrames = await extractFromTab(worker, 'https://web.whatsapp.com/*');
  const whatsapp = whatsappFrames.find((item) => item.response?.ok && item.response.data.messages.length)?.response.data;
  assert.ok(whatsapp, JSON.stringify(whatsappFrames));
  assert.equal(whatsapp.phone, '+8613800000000');
  assert.equal(whatsapp.messages[0].text, 'Can you share the MOQ?');

  const mailFixture = fixture('netease-enterprise.html').replace(/&/g, '&amp;').replace(/"/g, '&quot;');
  const mailPage = await interceptedPage(browser, 'https://mailh.qiye.163.com/static/sirius-web/#mailbox', `<iframe srcdoc="${mailFixture}"></iframe>`);
  const mailFrames = await extractFromTab(worker, 'https://mailh.qiye.163.com/*');
  const mail = mailFrames.find((item) => item.response?.ok && item.response.data.messages.length)?.response.data;
  assert.ok(mail, JSON.stringify(mailFrames));
  assert.equal(mail.email, 'purchasing@factory.example');
  assert.match(mail.messages[0].text, /revised lead time/);

  await whatsappPage.close();
  await mailPage.close();
  await browser.disconnect();
  console.log('Edge MV3 end-to-end: NetEase iframe + WhatsApp JID extraction OK');
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
