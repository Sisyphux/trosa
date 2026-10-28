// Regression harness: the Inbox reply box must not send while an IME is composing.
//
// Pressing Enter to confirm a pinyin / kana candidate is not "send" (Safari reports it as
// keyCode 229 with isComposing already false); a plain Enter sends, Shift+Enter does not.
//
// Run standalone (needs node + jsdom):
//   node tests/support/dialogue_reply_ime_check.cjs
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
win.fetch = async () => ({ ok: true, status: 200, json: async () => ({}) });
win.eval(script);

win.eval('var __sent = 0; submitDialogueReply = function() { __sent += 1; };');
const sent = () => win.eval('__sent');
const press = (init) => {
  const event = new win.KeyboardEvent('keydown', Object.assign({ key: 'Enter', bubbles: true, cancelable: true }, init));
  const result = win.dialogueReplyKeydown(event);
  return { result, prevented: event.defaultPrevented };
};

let outcome = press({ isComposing: true });
assert.equal(sent(), 0, 'Enter while composing must not send');
assert.equal(outcome.prevented, false, 'Enter while composing must reach the IME');

outcome = press({ keyCode: 229 });
assert.equal(sent(), 0, 'Safari IME Enter (keyCode 229) must not send');

press({ shiftKey: true });
assert.equal(sent(), 0, 'Shift+Enter is a newline');

press({});
assert.equal(sent(), 1, 'a plain Enter still sends');

console.log('dialogue reply IME regression: OK');
