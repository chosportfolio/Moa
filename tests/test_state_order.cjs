// Expected-behavior regression tests for delayed GET responses crossing a POST.
// Uses actual UI functions with deferred promises; no browser, server or user data.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../web/app.js'), 'utf8');
function section(from, until) {
  const first = source.indexOf(from), last = source.indexOf(until, first);
  assert(first >= 0 && last > first, `Find production function: ${from}`);
  return source.slice(first, last);
}
const clone = value => JSON.parse(JSON.stringify(value));
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return {promise, resolve, reject};
}
function fixture() {
  const old = {state_revision: 10, conversation_id: 'old-conversation', segments: [{id: 'old-line', text: 'fixture'}],
    recommendations: [], search_mode: 'manual', region: '충주', saved_places: [],
    trip: {id: 'trip-A', status: 'selected', place: {name: 'A'}},
    models: {ready: true}, location: {available: false}, job: null};
  const stale = deferred(), nodes = new Map();
  let backend = clone(old), getCount = 0;
  function node(id) {
    if (!nodes.has(id)) nodes.set(id, {value: '', hidden: true, disabled: false, scrollTop: 0,
      lastElementChild: {}, classList: {toggle() {}},
      addEventListener(type, callback) { this[type] = callback; }});
    return nodes.get(id);
  }
  const context = {
    state: clone(old), stateFetching: false, initialStateLoaded: true, regionDraftDirty: false, transcriptSignature: '',
    stateRefreshPromise: null, stateRefreshQueued: false, stateRequestSequence: 0, stateMetadataSequence: 0, stateSnapshotVersion: 0,
    stateResponseSequences: new WeakMap(), stateMetadataKeys: new Set(['status', 'models', 'location', 'seat_input', 'seat_channels', 'job', 'errors']),
    activeJob: null, recording: null, processing: false, processingPurpose: '',
    streamRunning: false, streamFailure: null, streamQueue: [], nativePendingAudio: false,
    tripBusy: false, guidanceStarting: false, guidanceCancelling: false,
    regionSaving: false, recommendAfterRegion: false, areaChoiceKey: null, showPlaceDetail: false,
    nativeNavigation: {available: false}, currentTab: 'transcript',
    $: node, document: {activeElement: null}, window: {},
    render() {}, renderControls() {}, renderTrip() {}, renderSavedPlaces() {}, renderLocation() {},
    refreshNavigationState() {}, switchTab() {}, setDialogOpen() {}, notify() {},
    requestAnimationFrame(callback) { callback(); }, watchJob() {},
    AbortController, setTimeout, clearTimeout,
    async fetch(url, options) {
      let payload;
      if (url === '/api/state') { getCount++; payload = getCount === 1 ? await stale.promise : clone(backend); }
      else {
      if (url === '/api/trip/select') backend = {...backend, trip: {id: 'trip-B', status: 'selected', place: {name: 'B'}}};
      else if (url === '/api/reset') backend = {...backend, conversation_id: 'new-conversation', segments: [], recommendations: []};
      else if (url === '/api/region') backend = {...backend, region: JSON.parse(options.body).region, search_mode: 'manual'};
      else throw new Error('Unexpected fixture request: ' + url);
      backend.state_revision++;
      payload = clone(backend);
      }
      return {ok: true, text: async () => JSON.stringify(payload)};
    },
  };
  vm.createContext(context);
  vm.runInContext([
    section('  async function request(', '  function switchTab('),
    section('  async function applyTripResult(', '  function refreshNavigationState('),
    section('  async function saveRegion(', '  async function recommend('),
    section("  $('reset-confirm').addEventListener", "  $('demo-button').addEventListener"),
  ].join('\n'), context);
  return {context, old, stale, node, getCount: () => getCount};
}

const tick = () => new Promise(resolve => setImmediate(resolve));

const cases = [
  ['A delayed state GET cannot replace a successfully selected trip', async () => {
    const f = fixture(), ui = f.context;
    const polling = ui.refreshState();
    const selecting = ui.selectPlace('place-B');
    await tick();
    assert.equal(ui.state.trip.id, 'trip-B', 'POST response applied first');
    f.stale.resolve(clone(f.old));
    await Promise.all([polling, selecting]);
    assert.equal(ui.state.trip.id, 'trip-B', 'Late pre-selection GET rolled the selected trip back');
    assert.equal(f.getCount(), 2, 'The mutation queues exactly one fresh snapshot');
  }],
  ['Reset immediately adopts the new conversation despite an existing poll', async () => {
    const f = fixture(), ui = f.context;
    const polling = ui.refreshState();
    const resetting = f.node('reset-confirm').click();
    await tick();
    assert.equal(ui.state.conversation_id, 'new-conversation', 'Reset immediately applies its own snapshot');
    f.stale.resolve(clone(f.old));
    await Promise.all([polling, resetting]);
    assert.equal(ui.state.conversation_id, 'new-conversation', 'Reset ignored its response and reused the old in-flight GET');
    assert.equal(ui.state.segments.length, 0);
  }],
  ['A delayed GET cannot replace a successfully changed search area', async () => {
    const f = fixture(), ui = f.context;
    const polling = ui.refreshState();
    f.node('region-picker-input').value = '부산';
    const saving = ui.saveRegion({preventDefault() {}});
    await tick();
    assert.equal(ui.state.region, '부산', 'POST response applied first');
    f.stale.resolve(clone(f.old));
    await Promise.all([polling, saving]);
    assert.equal(ui.state.region, '부산', 'Late pre-change GET reverted the new area');
  }],
  ['Metadata still updates when the saved conversation has not changed', async () => {
    const f = fixture(), ui = f.context;
    ui.state.models.ready = false;
    const polling = ui.refreshState();
    f.stale.resolve({...clone(f.old), models: {ready: true}, location: {available: true, source: 'vehicle'}});
    await polling;
    assert.equal(ui.state.models.ready, true);
    assert.equal(ui.state.location.available, true);
  }],
  ['Older request metadata cannot replace a newer response at the same revision', async () => {
    const f = fixture(), ui = f.context;
    const oldResponse = {...clone(f.old), models: {ready: false}, job: {id: 'job', status: 'running'}, location: {available: false}};
    const freshResponse = {...clone(f.old), models: {ready: true}, job: {id: 'job', status: 'complete'}, location: {available: true}};
    ui.stateResponseSequences.set(oldResponse, 1);
    ui.stateResponseSequences.set(freshResponse, 2);
    ui.applyState(freshResponse);
    ui.applyState(oldResponse);
    assert.equal(ui.state.models.ready, true);
    assert.equal(ui.state.job.status, 'complete');
    assert.equal(ui.state.location.available, true);
  }],
  ['Concurrent refreshes coalesce while timer polls do not prolong the cycle', async () => {
    const f = fixture(), ui = f.context;
    const requests = [ui.refreshState(), ui.refreshState(), ui.refreshState(), ui.refreshState(false)];
    assert.equal(f.getCount(), 1);
    f.stale.resolve(clone(f.old));
    await Promise.all(requests);
    assert.equal(f.getCount(), 2);
    assert.equal(ui.stateFetching, false);
  }],
  ['A failed older GET does not disable models after a newer successful POST', async () => {
    const f = fixture(), ui = f.context;
    const polling = ui.refreshState();
    await ui.jsonRequest('/api/region', 'POST', {region: '부산'});
    f.stale.reject(new Error('Synthetic expired request'));
    await polling;
    assert.equal(ui.state.models.ready, true);
    assert.equal(ui.state.region, '부산');
  }],
  ['Accepted recommendation snapshots immediately expose the local summary', async () => {
    const f = fixture(), ui = f.context;
    const summary = {title: '대화에서 나온 조건', text: '조용한 카페를 원해요.'};
    ui.fetch = async () => ({ok: true, text: async () => JSON.stringify({job_id: 'queued-recommendation',
      state: {...clone(f.old), state_revision: 11, recommendation_summary: summary, recommendations: []}})});
    const result = await ui.jsonRequest('/api/recommend', 'POST', {mode: 'conversation'});
    assert.equal(result.job_id, 'queued-recommendation');
    assert.equal(ui.state.recommendation_summary.text, summary.text);
    assert.equal(ui.state.state_revision, 11);
    const stale = clone(f.old);
    ui.stateResponseSequences.set(stale, 0);
    ui.applyState(stale);
    assert.equal(ui.state.recommendation_summary.text, summary.text);
  }],
];

(async () => {
  let failures = 0;
  for (const [name, run] of cases) {
    try { await run(); console.log('PASS:', name); }
    catch (error) { failures++; console.error('FAIL:', name, '\n ', error.message); }
  }
  console.log(`State response ordering: ${cases.length - failures} passed, ${failures} failed.`);
  if (failures) process.exitCode = 1;
})().catch(error => { console.error(error); process.exitCode = 1; });
