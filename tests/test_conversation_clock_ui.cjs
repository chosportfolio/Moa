// Chat timestamps represent a persisted instant, never an elapsed duration.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
process.env.TZ = 'Asia/Seoul';
const source = fs.readFileSync(path.join(__dirname, '../web/app.js'), 'utf8');
const begin = source.indexOf('  const conversationTime =');
const end = source.indexOf('  function notify', begin);
const context = vm.createContext({Intl, Date});
vm.runInContext(source.slice(begin, end) + '\nthis.formatClock = conversationTime;', context);
const clock = context.formatClock;
assert.equal(clock('2026-11-12T03:03:02Z'), '오후 12:03');
assert.equal(clock('2026-11-12T12:03:58+09:00'), '오후 12:03');
assert.equal(clock('2026-11-12T03:04:00Z'), '오후 12:04');
assert.equal(clock('2026-11-11T15:00:00Z'), '오전 12:00');
for (const absent of [undefined, null, '', 63, '63', '00:08', 'invalid', '2026-11-12T12:03:00']) {
  assert.equal(clock(absent), '');
}
assert(source.includes('conversationTime(segment.spoken_at)'));
assert(!source.includes('title="대화 시작 후 경과 시간"'));
console.log('Conversation clock: persisted local time, minute boundaries and unknown-time omission passed.');
