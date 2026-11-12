// Transport-state regressions only. No browser, microphone, server or user data.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../web/app.js'), 'utf8');
const first = source.indexOf('  async function drainStreamQueue()');
const last = source.indexOf('  window.onNativeSaveResult', first);
assert(first > 0 && last > first, 'Find actual production stream functions');
const transport = source.slice(first, last);

function chunk(sequence = 1) {
  return {session: 'test-session', conversationId: 'conversation-before-reset', sequence, start: sequence * 2,
    blob: {type: 'audio/wav', fixture: true}, jobId: null};
}

function setup(entries, handler) {
  const events = [];
  const notice = {hidden: true};
  const context = {
    streamQueue: entries, streamRunning: false, streamFailure: null,
    processing: false, processingPurpose: '', processingMessage: '',
    activeJob: null, nativePendingAudio: false, recording: null,
    state: {conversation_id: 'conversation-after-reset'},
    request: handler, renderControls() {}, render() {},
    async refreshState() {}, notify(message) { events.push(['notice', message]); },
    stopRecording() { events.push(['stop']); context.recording = null; },
    $(id) { return notice; },
    setTimeout(callback) { queueMicrotask(callback); return 1; },
    window: {CabinAndroid: {
      acknowledgeAudio(session, sequence) { events.push(['ack', session, sequence]); },
      discardAudio(session, sequence) { events.push(['discard', session, sequence]); },
    }},
  };
  vm.createContext(context);
  vm.runInContext(transport, context, {filename: 'production-stream-functions.js'});
  return {context, events};
}

function statusError(status, message = 'fixture failure') {
  return Object.assign(new Error(message), {status});
}

async function checkCaptureGeneration() {
  const startSource = source.slice(source.indexOf('  async function startRecording(purpose)'),
    source.indexOf('  function stopRecording()'));
  const uploadSource = source.slice(source.indexOf('  async function uploadAudio('),
    source.indexOf('  function watchJob('));
  const controls = new Map();
  const requests = [];
  const uploads = [];
  const context = {
    state: {conversation_id: 'capture-start'}, recording: null, processing: false,
    regionSaving: false, streamFailure: null, nativePendingAudio: false,
    currentStreamSession: null, nativeStartTimer: null, microphoneStream: null,
    recorder: null, audioChunks: [], streamQueue: [], streamRunning: false,
    processingPurpose: '', processingMessage: '',
    Blob, renderControls() {}, renderCrew() {}, render() {}, switchTab() {}, notify() {},
    setTimeout() { return 1; }, clearTimeout() {}, async refreshState() {},
    $(id) {
      if (!controls.has(id)) controls.set(id, {addEventListener(type, callback) { this[type] = callback; }, click() {}});
      return controls.get(id);
    },
    async uploadAudio(...args) { uploads.push(args); },
    async request(url, options) { requests.push(options); return {committed: true}; },
    window: {CabinAndroid: {startRecordingForConversation(...args) { requests.push(args); }}},
  };
  vm.createContext(context);
  vm.runInContext(startSource, context);
  await context.startRecording('meeting');
  context.state.conversation_id = 'after-native-start';
  assert.deepEqual(requests.pop(), ['meeting', 'capture-start'], 'Native recording receives the generation present at start');

  let mediaRecorder;
  class FakeRecorder {
    static isTypeSupported() { return true; }
    constructor() { mediaRecorder = this; this.mimeType = 'audio/webm'; }
    start() {}
  }
  context.recording = null;
  context.state.conversation_id = 'before-microphone-permission';
  context.window = {MediaRecorder: FakeRecorder};
  context.MediaRecorder = FakeRecorder;
  context.navigator = {mediaDevices: {async getUserMedia() { return {getTracks() { return [{stop() {}}]; }}; }}};
  const starting = context.startRecording('meeting');
  context.state.conversation_id = 'after-permission-prompt';
  await starting;
  mediaRecorder.ondataavailable({data: new Blob(['synthetic audio'])});
  await mediaRecorder.onstop();
  assert.equal(uploads.pop()[3], 'before-microphone-permission', 'Browser recording retains the generation from before async microphone permission');

  context.recording = null;
  context.state = {conversation_id: 'sample-conversation', demo_session: true};
  context.window = {CabinAndroid: {startRecordingForConversation(...args) { requests.push(args); }}};
  context.jsonRequest = async url => {
    assert.equal(url, '/api/demo/exit');
    context.state = {conversation_id: 'restored-conversation', demo_session: false};
  };
  await context.startRecording('meeting');
  assert.deepEqual(requests.pop(), ['meeting', 'restored-conversation'], 'Recording a sample first restores the actual conversation generation');
  context.recording = null;
  context.state = {conversation_id: 'sample-again', demo_session: true};
  context.jsonRequest = async () => { throw new Error('active trip'); };
  await context.startRecording('meeting');
  assert.equal(context.recording, null, 'A failed sample exit never opens the microphone');
  assert.equal(context.processing, false, 'A failed sample exit unlocks controls');

  vm.runInContext(uploadSource, context);
  await context.uploadAudio(new Blob(['synthetic audio']), 'meeting', 'fixture.wav', 'original-recording-conversation');
  assert.equal(requests.pop().headers['X-Conversation-Id'], 'original-recording-conversation', 'Recorded audio sends its captured generation');
}

async function main() {
  await checkCaptureGeneration();
  {
    const entry = chunk();
    const attempts = [];
    const {context, events} = setup([entry], async (url, options) => {
      attempts.push([url, options]);
      throw statusError(503);
    });
    context.recording = {purpose: 'meeting'};
    await context.drainStreamQueue();
    assert.equal(attempts.length, 3, 'Bound transient automatic retries');
    assert.equal(context.streamQueue[0], entry, 'Keep failed audio for retry');
    assert(context.streamFailure, 'Show a recovery state');
    assert.equal(events.filter(event => event[0] === 'ack').length, 0, 'Never release uncommitted native audio');
    assert(events.some(event => event[0] === 'stop'), 'Stop capture when delivery fails');
    assert(attempts.every(([, options]) => options.body === entry.blob), 'Retries preserve the original audio');
    assert(attempts.every(([, options]) => options.headers['X-Stream-Id'] === entry.session
      && options.headers['X-Sequence'] === '1'), 'Retries preserve the idempotency key');
    assert(attempts.every(([, options]) => options.headers['X-Conversation-Id'] === entry.conversationId),
      'Automatic retries preserve the recorded conversation even after a reset');
    context.state.conversation_id = 'conversation-reset-again';
    context.request = async (url, options) => {
      assert.equal(options.headers['X-Conversation-Id'], entry.conversationId, 'Manual retry never relabels old audio');
      return {committed: true, duplicate: true};
    };
    await context.retryStreamAudio();
    assert.equal(context.streamQueue.length, 0, 'Retry releases committed audio');
    assert.equal(context.streamFailure, null);
    assert.equal(events.filter(event => event[0] === 'ack').length, 1);
  }
  {
    let calls = 0;
    const {context, events} = setup([chunk()], async () => { calls++; throw statusError(400, 'key mismatch'); });
    await context.drainStreamQueue();
    assert.equal(calls, 1, 'Do not automatically repeat a terminal client rejection');
    assert.equal(context.streamQueue.length, 1);
    assert.equal(events.filter(event => event[0] === 'ack').length, 0);
  }
  {
    const events = [];
    let polls = 0;
    const {context} = setup([chunk()], async url => {
      if (url.startsWith('/api/transcribe')) { events.push('accepted'); return {job_id: 'job-1'}; }
      events.push(++polls === 1 ? 'running' : 'committed');
      return polls === 1 ? {status: 'running'} : {status: 'complete', result: {committed: true}};
    });
    context.window.CabinAndroid.acknowledgeAudio = () => events.push('ack');
    await context.drainStreamQueue();
    assert.deepEqual(events, ['accepted', 'running', 'committed', 'ack'], 'Acknowledge only after commit');
  }
  {
    const entry = chunk();
    entry.jobId = 'job-lost-on-restart';
    const requests = [];
    const {context, events} = setup([entry], async url => {
      requests.push(url);
      if (url.startsWith('/api/jobs/')) throw statusError(404);
      return {committed: true, duplicate: true};
    });
    await context.drainStreamQueue();
    assert.deepEqual(requests, ['/api/jobs/job-lost-on-restart', '/api/transcribe?stream=1']);
    assert.equal(events.filter(event => event[0] === 'ack').length, 1);
    assert.equal(context.streamQueue.length, 0);
  }
  {
    let calls = 0;
    const message = '이전 대화에서 녹음한 발언이에요. 현재 대화에는 추가하지 않았어요.';
    const {context, events} = setup([chunk()], async () => { calls++; throw statusError(409, message); });
    await context.drainStreamQueue();
    assert.equal(calls, 1, 'A stale first chunk is not repeatedly submitted');
    assert.equal(context.streamQueue.length, 1, 'Keep rejected old audio until the user chooses not to resend');
    assert.equal(events.filter(event => event[0] === 'ack').length, 0);
    assert(events.some(event => event[0] === 'notice' && event[1] === message), 'Explain the generation conflict');
  }
  {
    let calls = 0;
    const entry = chunk();
    delete entry.conversationId;
    const {context, events} = setup([entry], async () => { calls++; return {committed: true}; });
    await context.drainStreamQueue();
    assert.equal(calls, 0, 'Legacy queued audio must not inherit the current conversation');
    assert.equal(context.streamQueue[0], entry);
    assert.equal(context.streamFailure.status, 409);
    assert.equal(events.filter(event => event[0] === 'ack').length, 0);
  }
  {
    for (const result of [{committed: true, segments: []}, {committed: true, segments: [{text: ''}]}]) {
      const {context, events} = setup([chunk()], async url => url.startsWith('/api/transcribe')
        ? {job_id: 'empty-text-job'} : {status: 'complete', result});
      await context.drainStreamQueue();
      assert.equal(context.streamQueue.length, 0, 'Successful silent or blank recognition is a committed chunk');
      assert.equal(events.filter(event => event[0] === 'ack').length, 1);
    }
  }
  {
    const {context, events} = setup([chunk(1), chunk(2)], async () => ({committed: true}));
    context.streamFailure = new Error('fixture failure');
    await context.discardStreamAudio();
    assert.deepEqual(events.filter(event => ['discard', 'ack'].includes(event[0])),
      [['discard', 'test-session', 1], ['ack', 'test-session', 2]], 'Explicit no-resend releases only that entry, then continues FIFO');
    assert.equal(context.streamQueue.length, 0);
  }
  console.log('Stream delivery: retention/retry, conversation isolation, silence/empty ACK, restart and explicit no-resend passed.');
}

main().catch(error => { console.error(error); process.exitCode = 1; });
