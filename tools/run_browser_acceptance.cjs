// Run the user-shaped Trosa acceptance programs in the Chromium build pinned by
// browser-extension/package-lock.json.  Every invocation launches its own
// headless Chromium context, so no desktop browser (Tabbit) and no persistent
// browser task is part of the gate -- the same entry the CI release pipeline can
// run, on a machine that has never seen a developer's browser.
//
// The programs (tools/browser_acceptance.js, tools/inbox_browser_acceptance.js)
// run in Node with the injected `page`/`context` handles, exactly as they did
// under the Tabbit runtime, so the assertions themselves stay unchanged.
//
// Exit codes (consumed by deploy/cloud/lib-release-gate.sh):
//   0   pass
//   20  assertion / product failure  -- never retried, so a real defect cannot
//                                       be washed green by a rerun
//   21  infrastructure failure (server, browser, transport, missing dependency)
//                                    -- the gate retries once and records a
//                                       flake event
'use strict';

const fs = require('fs');
const path = require('path');

const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
const ROOT = path.resolve(__dirname, '..');

const EXIT_ASSERTION = 20;
const EXIT_INFRA = 21;

const KIND = process.argv[2] || 'core';
const RUN_ID = (process.env.TROSA_BROWSER_ACCEPTANCE_RUN_ID || '')
  || `${KIND}-${Date.now()}-${process.pid}`;

const SCRIPTS = {
  core: 'browser_acceptance.js',
  inbox: 'inbox_browser_acceptance.js',
  dialogue: 'dialogue_browser_acceptance.js',
};

// Only unambiguous transport/launch/runtime failures count as infrastructure.
// Everything else -- including a bare locator timeout with no transport signal --
// is a product assertion, so a genuine defect is never retried into green.
const INFRA_PATTERNS = [
  /ECONNREFUSED/,
  /ECONNRESET/,
  /EPIPE/,
  /ETIMEDOUT/,
  /EHOSTUNREACH/,
  /ERR_CONNECTION/,
  /ERR_NAME_NOT_RESOLVED/,
  /ERR_NETWORK_CHANGED/,
  /ERR_EMPTY_RESPONSE/,
  /ERR_ADDRESS_UNREACHABLE/,
  /ERR_INTERNET_DISCONNECTED/,
  /ERR_SSL/,
  /ERR_CERT/,
  /net::ERR_/,
  /NS_ERROR_/,
  /socket hang up/i,
  /Target (page, )?(context or browser )?(has been )?closed/i,
  /browser has (been )?(closed|disconnected)/i,
  /Protocol error/i,
  /Failed to launch/i,
  /Executable doesn't exist/i,
  /Cannot find module/,
  /ENOENT/,
];

function classify(error) {
  if (error && error.acceptanceClass === 'assertion') return 'assertion';
  if (error && error.acceptanceClass === 'infra') return 'infra';
  const name = (error && error.name) || '';
  const message = (error && error.message) || String(error || '');
  const text = `${name}: ${message}`;
  if (INFRA_PATTERNS.some((pattern) => pattern.test(text))) return 'infra';
  // Fail closed: an unrecognised failure is a product failure and is not retried.
  return 'assertion';
}

function loadChromium() {
  try {
    return require(path.join(ROOT, 'browser-extension/node_modules/playwright')).chromium;
  } catch (error) {
    error.acceptanceClass = 'infra';
    throw error;
  }
}

function readProgramSource(kind) {
  let source = fs.readFileSync(path.join(__dirname, SCRIPTS[kind]), 'utf8');
  if (kind === 'inbox') {
    // The Inbox program carries placeholders instead of importing shell state;
    // the launcher passes the concrete isolated values through the environment.
    const values = {
      __TROSA_INBOX_BROWSER_URL__: process.env.TROSA_INBOX_BROWSER_URL,
      __TROSA_INBOX_BROWSER_SAMPLES__: process.env.TROSA_INBOX_BROWSER_SAMPLES,
      __TROSA_INBOX_BROWSER_CONTACT_ID__: process.env.TROSA_INBOX_BROWSER_CONTACT_ID,
    };
    for (const [placeholder, value] of Object.entries(values)) {
      if (!value) {
        const error = new Error(`缺少 ${placeholder}：Inbox 验收启动器未提供环境`);
        error.acceptanceClass = 'infra';
        throw error;
      }
      source = source.replaceAll(`'${placeholder}'`, JSON.stringify(value));
    }
  }
  if (kind === 'dialogue') {
    // The dialogue program carries placeholders instead of importing shell
    // state; the launcher passes the concrete isolated values through the
    // environment (the URL, the seeded customer id, a sample upload path, the
    // screenshot directory, and a JSON map of sample slug -> seeded thread id).
    const values = {
      __TROSA_DIALOGUE_BROWSER_URL__: process.env.TROSA_DIALOGUE_BROWSER_URL,
      __TROSA_DIALOGUE_BROWSER_CUSTOMER_ID__: process.env.TROSA_DIALOGUE_BROWSER_CUSTOMER_ID,
      __TROSA_DIALOGUE_BROWSER_SAMPLES__: process.env.TROSA_DIALOGUE_BROWSER_SAMPLES,
      __TROSA_DIALOGUE_BROWSER_SHOTS__: process.env.TROSA_DIALOGUE_BROWSER_SHOTS,
      __TROSA_DIALOGUE_BROWSER_THREADS__: process.env.TROSA_DIALOGUE_BROWSER_THREADS,
    };
    for (const [placeholder, value] of Object.entries(values)) {
      if (!value) {
        const error = new Error(`缺少 ${placeholder}：对话验收启动器未提供环境`);
        error.acceptanceClass = 'infra';
        throw error;
      }
      source = source.replaceAll(`'${placeholder}'`, JSON.stringify(value));
    }
  }
  return source;
}

let pageRef = null;
const consoleErrors = [];
const requestFailures = [];

async function captureArtifacts(error) {
  const dir = process.env.TROSA_BROWSER_ACCEPTANCE_ARTIFACTS
    || path.join(ROOT, '.browser-artifacts');
  const base = path.join(dir, `${KIND}-${RUN_ID}`);
  const report = {
    kind: KIND,
    runId: RUN_ID,
    error: { name: (error && error.name) || null, message: (error && error.message) || String(error || '') },
    consoleErrors: consoleErrors.slice(-40),
    requestFailures: requestFailures.slice(-40),
  };
  const page = pageRef;
  try {
    if (page && !page.isClosed()) {
      report.url = page.url();
      report.title = await page.title();
      report.activeSection = await page.evaluate(() => {
        const active = document.querySelector('.page-section.active');
        return active ? active.id : null;
      });
      report.activeElement = await page.evaluate(() => {
        const el = document.activeElement;
        if (!el) return null;
        return {
          id: el.id || null,
          tag: el.tagName,
          className: (el.className && String(el.className)) || null,
        };
      });
      report.visibleModals = await page.evaluate(() =>
        Array.from(document.querySelectorAll('.modal-overlay.show')).map((el) => el.id));
      report.toasts = await page.locator('#toastContainer .toast').allInnerTexts().catch(() => []);
    }
  } catch (_) {
    // Best effort: the exit code and classification must stand even if the page
    // is already gone.
  }
  try {
    fs.mkdirSync(dir, { recursive: true });
    fs.writeFileSync(`${base}.state.json`, `${JSON.stringify(report, null, 2)}\n`);
    if (page && !page.isClosed()) {
      await page.screenshot({ path: `${base}.png`, fullPage: true });
    }
  } catch (_) {
    // Artifacts are diagnostics, never a gate input.
  }
  return { report, state: `${base}.state.json`, screenshot: `${base}.png` };
}

async function main() {
  if (!Object.hasOwn(SCRIPTS, KIND)) {
    const error = new Error(`未知的浏览器验收类型：${KIND}（期望 core|inbox|dialogue）`);
    error.acceptanceClass = 'assertion';
    throw error;
  }
  const source = readProgramSource(KIND);
  const chromium = loadChromium();
  const browser = await chromium.launch({ headless: true });
  try {
    const context = await browser.newContext({ acceptDownloads: true });
    const page = await context.newPage();
    pageRef = page;
    page.on('console', (message) => {
      if (message.type() === 'error') consoleErrors.push(message.text());
    });
    page.on('pageerror', (error) => consoleErrors.push(`pageerror: ${error.message}`));
    page.on('requestfailed', (request) => {
      const failure = request.failure();
      const detail = failure ? failure.errorText : '';
      requestFailures.push(`${request.method()} ${request.url()} ${detail}`.trim());
    });
    await new AsyncFunction('page', 'context', source)(page, context);
    console.log(`browser acceptance: PASS kind=${KIND} run=${RUN_ID} (Playwright Chromium)`);
  } finally {
    await browser.close();
  }
}

if (require.main === module) {
  main().catch(async (error) => {
    const cls = classify(error);
    console.error(error && error.stack ? error.stack : String(error));
    const { report, state, screenshot } = await captureArtifacts(error);
    console.error(`browser acceptance: FAIL kind=${KIND} class=${cls} run=${RUN_ID}`);
    console.error(`browser acceptance: page state ${JSON.stringify({
      url: report.url || null,
      title: report.title || null,
      activeSection: report.activeSection || null,
      activeElement: report.activeElement || null,
      visibleModals: report.visibleModals || null,
      toasts: report.toasts || null,
      consoleErrors: report.consoleErrors,
      requestFailures: report.requestFailures,
    })}`);
    console.error(`browser acceptance: artifacts state=${state} screenshot=${screenshot}`);
    process.exitCode = cls === 'infra' ? EXIT_INFRA : EXIT_ASSERTION;
  });
}

module.exports = { classify, EXIT_ASSERTION, EXIT_INFRA, INFRA_PATTERNS };
