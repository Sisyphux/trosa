// The weekly board must open on the signed-in member, not on whoever sorts
// first. currentUser is the user object returned by /api/auth/login.
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const assert = require('assert');

const source = fs.readFileSync(path.join(__dirname, '..', '..', 'app', 'static', 'app.js'), 'utf8');
function extract(name) {
  const start = source.indexOf('function ' + name + '(');
  assert.ok(start >= 0, name + ' not found');
  let depth = 0;
  for (let i = source.indexOf('{', start); i < source.length; i++) {
    if (source[i] === '{') depth++;
    else if (source[i] === '}' && --depth === 0) return source.slice(start, i + 1);
  }
  throw new Error('unbalanced ' + name);
}
const code = extract('weeklyMemberIds') + '\n' + extract('defaultWeeklyMember');
function run(currentUser) {
  const ctx = {OV: {labels: {hamid: 'Hamid', amy: 'Amy', kelley: 'Kelley'}}, currentUser};
  vm.createContext(ctx);
  vm.runInContext(code + '\nvar result = defaultWeeklyMember();', ctx);
  return ctx.result;
}
assert.strictEqual(run({id: 'amy', name: 'Amy'}), 'amy');
assert.strictEqual(run({id: 'kelley', name: 'Kelley'}), 'kelley');
assert.strictEqual(run('kelley'), 'kelley');
assert.strictEqual(run(null), 'hamid');
assert.strictEqual(run({id: 'gone'}), 'hamid');
console.log('weekly default member check: OK');
