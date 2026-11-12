// Exercise the production command dispatcher without a microphone or user state.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../web/app.js'), 'utf8');
const start = source.indexOf('  const gleoHandledActions = new Set();');
const end = source.indexOf('  setInterval(() => {', start);
assert(start >= 0 && end > start);
function fixture(overrides = {}) {
  const replies = [], actions = [], timers = [];
  let currentTime = Date.now();
  const context = {
    stateSnapshotVersion: 0, initialStateLoaded: true, activeDialog: null, processing: false, regionSaving: false,
    streamFailure: null, guidanceStarting: false, guidanceCancelling: false, recording: null,
    streamRunning: false, streamQueue: [], nativePendingAudio: false,
    state: {models:{ready:true}, conversation_id:'test', segments:[{text:'칼국수 먹자'}], saved_places:[{}]},
    asArray: value => Array.isArray(value) ? value : [],
    refreshState: async () => { context.stateSnapshotVersion++; },
    setTimeout: (callback, delay) => { timers.push({callback, delay}); return timers.length; },
    Date: {now: () => currentTime},
    switchTab: name => actions.push(['tab',name]), notify: () => {},
    startRecording: () => actions.push(['record']), recommend: () => actions.push(['recommend']),
    window: {CabinAndroid:{completeGleoAction(id,status,message,data) {
      replies.push({id,status,message,data:JSON.parse(data)}); return context.ack !== false;
    }}}, ...overrides,
  };
  vm.createContext(context);
  vm.runInContext(source.slice(start,end),context);
  const run = (action,id='one',expiresAt=currentTime+5000) => context.window.onGleoAction({id,action,parameters:{},expiresAt});
  return {context,replies,actions,run,
    advance: milliseconds => { currentTime += milliseconds; },
    timeout() {
      const timer = timers.shift();
      assert(timer, 'Expected a bounded loading timeout');
      assert.equal(timer.delay, 3000);
      currentTime += timer.delay;
      timer.callback();
    },
  };
}
const tests = [];
function test(name, check) { tests.push({name, check}); }
function deferred() {
  let resolve;
  const promise = new Promise(done => { resolve = done; });
  return {promise, resolve};
}

test('Recommendation accepted once and dispatches existing search', async () => {
  const f=fixture(); await f.run('recommend_places'); await f.run('recommend_places');
  assert.equal(f.replies.length,1); assert.equal(f.replies[0].data[0].state,'requested');
  assert.deepEqual(f.actions,[['tab','places'],['recommend']]);
});

test('No transcript never triggers search', async () => {
  const f=fixture({state:{segments:[]}}); await f.run('recommend_places');
  assert.equal(f.replies[0].status,'asking'); assert.deepEqual(f.actions,[['tab','transcript']]);
});

test('Microphone request follows successful acknowledgement', async () => {
  const f=fixture(); await f.run('start_conversation');
  assert.equal(f.replies[0].data[0].state,'requested'); assert.deepEqual(f.actions,[['tab','transcript'],['record']]);
});

test('Unacknowledged request cannot start a microphone', async () => {
  const f=fixture({ack:false}); await f.run('start_conversation'); assert.equal(f.actions.length,0);
});

test('Saved tab carries a count but no private transcript', async () => {
  const f=fixture(); await f.run('show_saved'); assert.deepEqual(f.actions,[['tab','saved']]);
  assert.deepEqual(f.replies[0].data,[{screen:'saved',count:1}]);
});

test('Recording blocks a second recording and a search', async () => {
  const f=fixture({recording:{}}); await f.run('start_conversation'); await f.run('recommend_places','two');
  assert(f.replies.every(x=>x.status==='asking')); assert.equal(f.actions.length,0);
});

test('Active confirmation is respected', async () => {
  const f=fixture({activeDialog:'reset-dialog'}); await f.run('show_saved'); assert.equal(f.actions.length,0);
});

test('Unavailable initial state replies after the asynchronous refresh finishes', async () => {
  const load = deferred();
  const f=fixture({initialStateLoaded:false, refreshState:() => load.promise});
  const completed = f.run('recommend_places');
  assert.equal(f.actions.length,0); assert.equal(f.replies.length,0);
  load.resolve();
  await completed;
  assert.equal(f.replies.length,1); assert.equal(f.replies[0].status,'asking');
  assert.equal(f.replies[0].data[0].state,'loading'); assert.equal(f.actions.length,0);
});

test('Cold command runs once when the initial state becomes available', async () => {
  const load = deferred();
  const f=fixture({initialStateLoaded:false, refreshState:() => load.promise});
  const completed = f.run('show_saved');
  await f.run('show_saved');
  assert.equal(f.actions.length,0); assert.equal(f.replies.length,0);
  f.context.initialStateLoaded = true;
  load.resolve();
  await completed;
  assert.equal(f.replies.length,1); assert.deepEqual(f.actions,[['tab','saved']]);
});

test('Loading timeout cannot resume a command when a late response arrives', async () => {
  const load = deferred();
  const f=fixture({initialStateLoaded:false, refreshState:() => load.promise});
  const completed = f.run('start_conversation');
  f.timeout();
  await completed;
  assert.equal(f.replies.length,1); assert.equal(f.replies[0].data[0].state,'loading');
  f.context.initialStateLoaded = true;
  load.resolve();
  await Promise.resolve();
  assert.equal(f.actions.length,0); assert.equal(f.replies.length,1);
});

test('Request that expires during loading cannot start a microphone', async () => {
  const load = deferred();
  const f=fixture({initialStateLoaded:false, refreshState:() => load.promise});
  const completed = f.run('start_conversation');
  f.context.initialStateLoaded = true;
  f.advance(6000);
  load.resolve();
  await completed;
  assert.equal(f.actions.length,0); assert.equal(f.replies.length,0);
});

test('An action opened while loading is respected before microphone dispatch', async () => {
  const load = deferred();
  const f=fixture({initialStateLoaded:false, refreshState:() => load.promise});
  const completed = f.run('start_conversation');
  f.context.initialStateLoaded = true;
  f.context.activeDialog = 'reset-dialog';
  load.resolve();
  await completed;
  assert.equal(f.replies[0].data[0].state,'busy'); assert.equal(f.actions.length,0);
});

test('Unknown commands fail without mutation', async () => {
  const f=fixture(); await f.run('delete_all'); assert.equal(f.replies[0].status,'fail'); assert.equal(f.actions.length,0);
});

test('Missing command IDs are ignored', async () => {
  const f=fixture(); await f.run('start_conversation',''); assert.equal(f.replies.length,0); assert.equal(f.actions.length,0);
});

test('Expired requests do not start a microphone', async () => {
  const f=fixture(); await f.run('start_conversation','expired',f.context.Date.now()-1); assert.equal(f.actions.length,0);
});

test('Native rejection after loading cannot start a search', async () => {
  const load = deferred();
  const f=fixture({initialStateLoaded:false, refreshState:() => load.promise});
  const completed = f.run('recommend_places');
  f.context.initialStateLoaded = true;
  f.context.ack = false;
  load.resolve();
  await completed;
  assert.equal(f.replies.length,1); assert.equal(f.actions.length,0);
});

test('Unacknowledged empty conversation does not change screens', async () => {
  const f=fixture({ack:false, state:{segments:[]}});
  await f.run('recommend_places');
  assert.equal(f.actions.length,0);
});

test('Unavailable native reply bridge prevents microphone dispatch', async () => {
  const f=fixture({window:{}}); await f.run('start_conversation');
  assert.equal(f.actions.length,0);
});

(async () => {
  let passed = 0;
  for (const {name,check} of tests) {
    try { await check(); passed++; }
    catch(error) { error.message = name + ': ' + error.message; throw error; }
  }
  console.log(`Gleo UI: ${passed} checks passed.`);
})().catch(error => { console.error(error); process.exitCode = 1; });
