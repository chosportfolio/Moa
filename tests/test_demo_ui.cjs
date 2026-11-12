// Actual sample controls, isolated from user data, microphones and navigation.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../web/app.js'), 'utf8');
function section(from, until) {
  const start = source.indexOf(from), end = source.indexOf(until, start);
  assert(start >= 0 && end > start, `Missing production section: ${from}`);
  return source.slice(start, end);
}
function fixture(overrides = {}) {
  const nodes = new Map(), requests = [], notices = [], tabs = [];
  const node = id => {
    if (!nodes.has(id)) nodes.set(id, {hidden: false, disabled: false, textContent: '',
      classList: {toggle() {}}, addEventListener(type, handler) { this[type] = handler; }});
    return nodes.get(id);
  };
  const context = {
    state: {segments: [{text: '기존 실제 대화'}], models: {ready: true}, ...overrides},
    recording: null, processing: false, regionSaving: false, streamFailure: null, guidanceStarting:false, guidanceCancelling:false,
    streamQueue: [], nativePendingAudio: false, nativeStreaming: false, streamRunning: false,
    processingPurpose: 'meeting', processingMessage: '', showPlaceDetail: false, unreadMessages: 6,
    requestAnimationFrame: callback => callback(), updateUnread() { node('new-messages-button').hidden = context.unreadMessages === 0; },
    $: node, document: {querySelector: node},
    asArray: value => Array.isArray(value) ? value : [],
    conversationRows: () => context.state.segments || [], renderTrip() {},
    notify: value => notices.push(value), switchTab: value => tabs.push(value),
    refreshState: async () => {},
    async jsonRequest(url, method, body) {
      requests.push({url, method, body});
      if (context.failure) throw new Error('연결 실패');
      context.state = url.endsWith('/exit')
        ? {segments: [{text: '기존 실제 대화'}], models: {ready: true}, demo: false, demo_session: false}
        : {segments: [{text: '예시 대화'}], models: {ready: true}, demo: true, demo_session: true,
           demo_scenario: {title: '비 오는 날의 실내 나들이'}};
    },
  };
  vm.createContext(context);
  vm.runInContext(section('  function renderControls()', '  async function startRecording(')
    + section('  async function changeDemo(', "  document.addEventListener('keydown'"), context);
  context.renderControls();
  return {context, node, requests, notices, tabs};
}
let passed = 0;
async function test(name, run) {
  try { await run(); passed++; }
  catch (error) { error.message = name + ': ' + error.message; throw error; }
}
(async () => {
  await test('Existing real transcript can enter a reversible sample without reset or auto search', async () => {
    const f = fixture();
    await f.node('demo-button').click();
    assert.equal(f.requests.length, 1);
    assert.equal(f.requests[0].url, '/api/demo');
    assert.equal(f.requests[0].method, 'POST');
    assert.equal(f.tabs.at(-1), 'transcript');
    assert.equal(f.node('demo-button').textContent, '다른 샘플');
    assert.equal(f.node('demo-exit-button').hidden, false);
    assert.equal(f.node('record-button').disabled, false);
    assert.equal(f.node('reset-button').disabled, false);
    assert.match(f.node('dock-subtitle').textContent, /원래 대화에 이어서/);
    assert.equal(f.node('recommend-button').disabled, false);
    assert.match(f.node('seat-source-note').textContent, /실제 음성 인식 결과가 아니/);
    assert.equal(f.node('dock-title').textContent, '비 오는 날의 실내 나들이');
    assert.equal(f.node('transcript-scroll').scrollTop, 0);
    assert.equal(f.node('new-messages-button').hidden, true);
  });
  await test('Another sample is available while a sample already has lines', async () => {
    const f = fixture({demo: true, demo_session: true});
    await f.node('demo-button').click();
    assert.equal(f.requests[0].url, '/api/demo');
  });
  await test('Exit restores normal controls and does not reset the original conversation', async () => {
    const f = fixture({demo: true, demo_session: true});
    await f.node('demo-exit-button').click();
    assert.equal(f.requests[0].url, '/api/demo/exit');
    assert.equal(f.context.state.segments[0].text, '기존 실제 대화');
    assert.equal(f.node('demo-exit-button').hidden, true);
    assert.equal(f.node('demo-button').textContent, '샘플 대화');
    assert.equal(f.node('record-button').disabled, false);
  });
  await test('In-flight audio and searches cannot be crossed by sample switches', async () => {
    for (const [field, value] of [['recording', {}], ['processing', true], ['regionSaving', true],
      ['streamFailure', {}], ['streamQueue', [{}]], ['nativePendingAudio', true]]) {
      const f = fixture();
      f.context[field] = value;
      await f.context.changeDemo();
      assert.equal(f.requests.length, 0, field);
    }
  });
  await test('Selecting a place does not lock the sample button', async () => {
    const f = fixture({demo:true,demo_session:true,trip:{status:'selected'}});
    assert.equal(f.node('demo-button').disabled,false);
    await f.node('demo-button').click();
    assert.equal(f.requests[0].url,'/api/demo');
  });
  await test('An actual journey explains why a sample cannot replace it', async () => {
    for (const status of ['navigating', 'arrived']) {
      const f = fixture({demo: true, demo_session: true, trip: {status}});
      assert.equal(f.node('demo-button').disabled, false);
      assert.equal(f.node('demo-exit-button').disabled, false);
      await f.context.changeDemo(true);
      assert.equal(f.requests.length, 0);
      assert(f.notices.length > 0);
    }
  });
  await test('Failed switch retains the visible conversation and re-enables controls', async () => {
    const f = fixture();
    f.context.failure = true;
    await f.node('demo-button').click();
    assert.equal(f.context.state.segments[0].text, '기존 실제 대화');
    assert.equal(f.context.processing, false);
    assert.equal(f.node('demo-button').disabled, false);
    assert.equal(f.notices.at(-1), '연결 실패');
  });
  await test('Sample demonstration remains available while speech models prepare', async () => {
    const f = fixture({models: {ready: false}});
    assert.equal(f.node('demo-button').disabled, false);
    await f.node('demo-button').click();
    assert.equal(f.requests.length, 1);
  });
  await test('The removed file-import product flow is absent', async () => {
    const html = fs.readFileSync(path.join(__dirname, '../web/index.html'), 'utf8');
    assert(!html.includes('id="upload-button"'));
    assert(!html.includes('id="audio-file"'));
    assert(!source.includes("$('upload-button')"));
    assert(!source.includes("$('audio-file')"));
  });
  await test('A silenced microphone keeps stop available and does not claim to hear', async () => {
    const f = fixture();
    f.context.nativeStreaming = true;
    f.context.recording = {purpose:'meeting', microphonePaused:true};
    f.context.renderControls();
    assert.equal(f.node('record-button').disabled, false);
    assert.equal(f.node('record-label').textContent, '대화 마치기');
    assert.match(f.node('listening-title').textContent, /기다리고/);
    assert.match(f.node('listening-description').textContent, /기록되지 않아요/);
    assert.equal(f.node('recommend-button').disabled, true);
    f.context.recording.microphonePaused = false;
    f.context.renderControls();
    assert.equal(f.node('listening-title').textContent, '듣고 있어요');
    assert(!f.node('listening-description').textContent.includes('기록되지'));
    f.context.recording = null;
    f.context.renderControls();
    assert.equal(f.node('listening-card').hidden, true);
    assert.equal(f.node('recommend-button').disabled, false);
  });
  console.log(`Sample conversation UI: ${passed} checks passed.`);
})().catch(error => { console.error(error); process.exitCode = 1; });
