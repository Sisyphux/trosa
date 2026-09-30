// Unit tests for the Today tide's time arithmetic (app.js).
//
// The tide turns each customer's local Mon–Fri 09:00–18:00 into the minutes of
// *our* one day, and every state/countdown/preview label is derived from that.
// These tests pin the day so the arithmetic is deterministic, and cover:
//   - whether a window is open at a given moment
//   - the next window, including weekend skip and crossing our midnight
//   - reordering the table for an arbitrary previewed moment
//   - the DST edges (Intl decides; we add no rules of our own)
//
// Run standalone (needs node + jsdom):
//   node tests/support/today_tide_check.cjs
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

// Pin "our day" so the tide arithmetic does not depend on when this runs.
// 2026-09-30T00:00:00Z is a Wednesday.
const WED = Date.UTC(2026, 8, 30, 0, 0, 0);
const SAT = Date.UTC(2026, 9, 3, 0, 0, 0);
let pinned = WED;
win.tideDayStartMs = function () { return pinned; };

const MIN = 60000;
const HOUR = 60;

function ok(label) { console.log('  ok   - ' + label); }

// The tide functions live in the jsdom realm, so their arrays have a different
// prototype; compare joined ids instead of deep-comparing cross-realm arrays.
function ids(list) {
  return Array.prototype.map.call(list, function (x) { return x.r.reminder_id; }).join(',');
}

function near(actual, expected, tolerance, label) {
  assert.ok(Math.abs(actual - expected) <= tolerance,
    label + ' (got ' + actual + ', expected ' + expected + ' ±' + tolerance + ')');
}

// --- 1. Windows open at local Mon–Fri 09:00–18:00 -------------------------
// Asia/Shanghai is UTC+8 with no DST. Our day starts Wed 00:00Z = Wed 08:00
// local, so Shanghai's Wednesday window is 01:00–10:00Z = minutes 60–600.
pinned = WED;
{
  const wins = win.tideWindowsForDay('Asia/Shanghai', WED);
  assert.equal(wins.length, 1, 'Shanghai should have exactly one window touching this day');
  near(wins[0].start, 60, 0.5, 'window opens at minute 60');
  near(wins[0].end, 600, 0.5, 'window closes at minute 600');

  const open = win.tideStateAt('Asia/Shanghai', 100);
  assert.equal(open.k, 'open', 'window is open at 100');
  near(open.left, 500, 0.5, 'left counts down to close');

  const later = win.tideStateAt('Asia/Shanghai', 30);
  assert.equal(later.k, 'later', 'window has not opened at 30');
  near(later.inMin, 30, 0.5, 'inMin counts up to open');

  const none = win.tideStateAt('Asia/Shanghai', 700);
  assert.equal(none.k, 'none', 'no window open at 700');
  ok('a window is open only inside local 09:00–18:00');
}

// --- 2. The next window crosses our midnight and skips weekends -----------
// Europe/London in late September is UTC+1 (BST): 09:00–18:00 local is
// 08:00–17:00Z, i.e. minutes 480–1020 of our day.
{
  const wins = win.tideWindowsForDay('Europe/London', WED);
  assert.equal(wins.length, 1, 'London has one window touching this day');
  near(wins[0].start, 480, 0.5, 'London opens at minute 480');
  near(wins[0].end, 1020, 0.5, 'London closes at minute 1020');

  const next = win.tideNextWindow('Europe/London', 1030);
  assert.ok(next, 'there is a next window after London closes');
  assert.equal(next.s, 1, 'the next London window is tomorrow');
  near(next.startMin, 480 + 1440, 0.5, 'tomorrow opens at 480 + 1440');
  ok('the next window is really computed across our midnight');
}

// America/Los_Angeles (PDT, UTC-7): Tuesday 09:00–18:00 local is
// 16:00Z–01:00Z, so it starts before our midnight and is clamped; Wednesday is
// 16:00Z–01:00Z+1d, so it runs past our midnight and is clamped too.
{
  const wins = win.tideWindowsForDay('America/Los_Angeles', WED);
  assert.equal(wins.length, 2, 'Los Angeles touches this day twice (carry-in and carry-out)');
  near(wins[0].start, 0, 0.5, 'the carried-in window is clamped to 0');
  near(wins[0].rawStart, -480, 0.5, 'its raw start is honestly negative');
  near(wins[0].end, 60, 0.5, 'the carried-in window closes at 60');
  near(wins[1].start, 960, 0.5, 'the carried-out window opens at 960');
  near(wins[1].end, 1440, 0.5, 'the carried-out window is clamped to 1440');
  near(wins[1].rawEnd, 1500, 0.5, 'its raw end is honestly past midnight');
  ok('windows that cross our midnight are clamped but keep honest edges');
}

// A Saturday in our day: the local Saturday/Sunday are skipped, so the next
// real window is Monday (day offset 2).
{
  pinned = SAT;
  const wins = win.tideWindowsForDay('Asia/Shanghai', SAT);
  assert.equal(wins.length, 0, 'no window on a local weekend');
  assert.equal(win.tideStateAt('Asia/Shanghai', 300).k, 'none', 'nothing opens on the weekend');
  const next = win.tideNextWindow('Asia/Shanghai', 300);
  assert.ok(next, 'the weekend still has a next window');
  assert.equal(next.s, 2, 'the next window after a weekend is Monday');
  assert.equal(win.tideDayLabel(2), '后天', 'day offset 2 is labelled 后天');
  pinned = WED;
  ok('weekends are skipped and the next Monday is computed');
}

// --- 3. Previewing an arbitrary moment reorders the table -----------------
// At minute 100 Shanghai is open and London is still later; at minute 500 both
// are open, so Shanghai (closing sooner) comes first; at 1030 both are done.
{
  pinned = WED;
  const shanghai = { reminder_id: 1, timezone: 'Asia/Shanghai', priority_score: 10, remind_date: '2026-09-30' };
  const london = { reminder_id: 2, timezone: 'Europe/London', priority_score: 20, remind_date: '2026-09-30' };
  const early = win.tideOrderedGroups([london, shanghai], 100);
  assert.equal(ids(early.open), '1', 'only Shanghai is open at 100');
  assert.equal(ids(early.later), '2', 'London is still later at 100');

  const mid = win.tideOrderedGroups([london, shanghai], 500);
  assert.equal(ids(mid.open), '1,2', 'at 500 Shanghai closes sooner, so it leads');
  assert.equal(mid.later.length, 0, 'nothing is still later at 500');

  const late = win.tideOrderedGroups([london, shanghai], 1030);
  assert.equal(late.open.length + late.later.length, 0, 'both windows are done at 1030');
  assert.equal(ids(late.none), '2,1', 'done rows fall back to priority order');
  ok('previewing a moment reorders open / later / none');
}

// A customer without a timezone never gets a window; it stays in its own group.
{
  const unknown = { reminder_id: 3, timezone: '', priority_score: 99, remind_date: '2026-09-28' };
  const groups = win.tideOrderedGroups([unknown], 100);
  assert.equal(groups.unknown.length, 1, 'a customer without a timezone is 时区未知');
  assert.equal(groups.open.length + groups.later.length + groups.none.length, 0, 'it is not guessed into any window');
  const info = win.tideStatusInfo(unknown, 100);
  assert.equal(info.main, '时区未知', 'its status says so plainly');
  ok('a customer without a timezone is never guessed into a window');
}

// --- 4. DST edges are whatever Intl says ----------------------------------
{
  const pdt = win.tideOffsetMs('America/Los_Angeles', Date.UTC(2026, 9, 25, 12));
  near(pdt, -7 * 3600000, 60000, 'late October is still PDT (UTC-7)');
  const pst = win.tideOffsetMs('America/Los_Angeles', Date.UTC(2026, 10, 8, 12));
  near(pst, -8 * 3600000, 60000, 'after the fall-back it is PST (UTC-8)');

  // Fall back: 2026-11-01 is the first Sunday of November.
  const fallBack = win.tideWallToMs('America/Los_Angeles', 2026, 11, 8, 9, 0);
  assert.equal(fallBack, Date.UTC(2026, 10, 8, 17, 0, 0), 'local 09:00 PST is 17:00Z');

  // Spring forward: 2026-03-08 is the second Sunday of March.
  const spring = win.tideWallToMs('America/Los_Angeles', 2026, 3, 8, 9, 0);
  assert.equal(spring, Date.UTC(2026, 2, 8, 16, 0, 0), 'local 09:00 PDT is 16:00Z');

  // A window that contains the fall-back still opens at local 09:00.
  const wins = win.tideWindowsForDay('America/Los_Angeles', Date.UTC(2026, 10, 1, 0, 0, 0));
  const sunday = wins.filter(function (w) { return w.rawStart > 0 && w.rawStart < 1440; });
  assert.equal(sunday.length, 0, 'Sunday itself is never a working window');
  ok('DST edges come from Intl, with no rules of our own');
}

console.log('\ntoday tide time check: OK');
