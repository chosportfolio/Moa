// Execute the real dialog functions with a small DOM double. No browser, server,
// microphone or user's saved state is involved.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '..', 'web', 'app.js'), 'utf8');
function section(from, until) {
  const start = source.indexOf(from);
  const end = source.indexOf(until, start);
  assert(start >= 0 && end > start, `Missing source function boundary: ${from}`);
  return source.slice(start, end);
}
function fixture() {
  const nodes = new Map();
  const node = id => {
    if (!nodes.has(id)) nodes.set(id, {
      hidden: true, disabled: false, textContent: '', innerHTML: '', dataset: {},
      classList: {toggle() {}},
    });
    return nodes.get(id);
  };
  const requests = [], notices = [], clearedTimers = [], scheduledTimers = [], finishedTracking = [];
  const sandbox = {
    finishedTracking,
    $, document: {querySelector: () => node('app-shell')}, window: {}, clearTimeout(timer) { clearedTimers.push(timer); },
    setTimeout(callback, delay) { const timer = {callback, delay}; scheduledTimers.push(timer); return timer; },
    URL, location: {href:'http://127.0.0.1:8765/'}, escapeHtml: value => String(value || '').replace(/[&<>"']/g, ''),
    notify: message => notices.push(message), asArray: value => Array.isArray(value) ? value : [],
    setDialogOpen(id, open) { node(id).hidden = !open; return true; },
    textValue: value => String(value), summaryHeading: value => value || '',
    tripSummary: trip => trip.summary || {}, reasonList: () => '', switchTab() {},
    jsonRequest: async (url, method, body) => {
      requests.push({url, method, body});
      const trip = sandbox.ui.current();
      return {trip: {...trip, status: url.endsWith('/arrive') ? 'arrived' : body.save ? 'saved' : 'dismissed'}};
    },
  };
  function $(id) { return node(id); }
  vm.createContext(sandbox);
  vm.runInContext(`
    let state = {trip:null};
    let recording = null, processing = false, tripBusy = false;
    let manualArrivalContext = null, arrivalDecisionContext = null;
    let showPlaceDetail = true, detailReasonsSignature = '', nativeGuidingTrip = null;
    let nativeNavigation = {}, guidanceStarting = false, guidanceCancelling = false;
    let guidanceTimer = null, guidanceCancelTimer = null, navigationEvents = Promise.resolve();
    let guidanceRouteTimerStarted = false;
    let releasedNavigationTrip = null;
    ${section('  function safeLink(', '  function renderControls(')}
    ${section('  function sameTripContext(', '  function renderSavedPlaces(')}
    ${section('  function openManualArrival(', '  function safeLink(')}
    ${section('  window.onNativeNavigation =', '  async function cancelTrip(')}
    async function applyTripResult(result) { state.trip = result.trip; renderTrip(); }
    this.ui = {renderTrip, openManualArrival, closeManualArrival, confirmManualArrival, decideSave,
      setNavigation(value) { nativeNavigation = value; },
      navigation() { return nativeNavigation; },
      nativeBridge() { window.CabinAndroid = {startNavigation() {}}; },
      trackingBridge() { window.CabinAndroid = {startNavigation() {}, finishNavigationTracking(id) { finishedTracking.push(id); }}; },
      event: window.onNativeNavigation, settled() { return navigationEvents; },
      startPending() { guidanceStarting = true; guidanceRouteTimerStarted = false; guidanceTimer = 'pending-route'; },
      pending() { return guidanceStarting; },
      setTrip(trip) { state.trip = trip; }, current() { return state.trip; }};
  `, sandbox);
  return {ui: sandbox.ui, node, requests, notices, clearedTimers, scheduledTimers, finishedTracking};
}
function trip(id, status = 'selected') {
  return {id, status, place: {name: `테스트 장소 ${id}`, latitude: 37, longitude: 127}, summary: {text: `${id} 요약`}};
}
async function run() {
  {
    const f = fixture();
    f.ui.trackingBridge();
    f.ui.setTrip(trip('A', 'navigating')); f.ui.renderTrip();
    assert.deepEqual(f.finishedTracking, [], 'An ongoing route keeps background tracking');
    f.ui.setTrip(trip('A', 'arrived')); f.ui.renderTrip();
    f.ui.setTrip(trip('A', 'saved')); f.ui.renderTrip();
    assert.deepEqual(f.finishedTracking, ['A'], 'Manual arrival ends observation once without cancelling navigation');
    f.ui.setTrip(trip('B', 'selected')); f.ui.renderTrip();
    assert.deepEqual(f.finishedTracking, ['A']);
    f.ui.setTrip(trip('B', 'cancelled')); f.ui.renderTrip();
    assert.deepEqual(f.finishedTracking, ['A', 'B'], 'Each completed trip releases only its own tracking owner');
  }
  {
    const f = fixture();
    f.ui.setTrip(trip('B')); f.ui.startPending();
    f.ui.event({trip_id:'B', status:'connecting', connected:true});
    f.ui.event({trip_id:'B', status:'routing', connected:true});
    f.ui.event({trip_id:'A', status:'routing', route_dispatched:true, connected:true});
    assert.equal(f.scheduledTimers.length, 0, 'Readiness, terms, and another trip cannot extend the request timeout');
    f.ui.event({trip_id:'B', status:'routing', route_dispatched:true, connected:true});
    assert.equal(f.scheduledTimers.length, 1, 'Actual dispatch gets a separate route-response timeout');
    assert.equal(f.scheduledTimers[0].delay, 30000);
    assert.deepEqual(f.clearedTimers, ['pending-route']);
    f.ui.event({trip_id:'B', status:'routing', route_dispatched:true, connected:true});
    assert.equal(f.scheduledTimers.length, 1, 'Snapshot refreshes cannot indefinitely extend the route timeout');
    f.scheduledTimers[0].callback();
    assert.equal(f.ui.pending(), false);
    assert.equal(f.ui.navigation().status, 'error');
    await f.ui.settled();
    assert.equal(f.requests.length, 0, 'Readiness and dispatch do not confirm that guidance has begun');
  }
  {
    const f = fixture();
    f.ui.setTrip(trip('B')); f.ui.startPending();
    f.ui.event({trip_id:'B', status:'routing', route_dispatched:true, connected:true});
    f.ui.setTrip(trip('C'));
    f.ui.setNavigation({trip_id:'C', status:'ready', connected:true});
    f.scheduledTimers[0].callback();
    assert.equal(f.ui.navigation().status, 'ready', 'A delayed timer from B cannot replace C status');
    assert.equal(f.ui.pending(), true);
  }
  {
    const f = fixture();
    f.ui.nativeBridge(); f.ui.setTrip(trip('B'));
    f.ui.setNavigation({trip_id:'B', available:false, connected:false, status:'ready',
      native_poi_available:true, native_place:{name:'B 내비 장소',address:'B 주소'}});
    f.ui.renderTrip();
    assert.equal(f.node('guidance-button').disabled, true);
    f.ui.event({trip_id:'A',status:'ready',connected:true,available:true});
    assert.equal(f.node('guidance-button').disabled, false, 'Reconnect from an earlier preview must enable the current destination');
    assert.equal(f.ui.navigation().trip_id, 'B');
    assert.equal(f.node('detail-name').textContent, 'B 내비 장소');
    f.ui.startPending();
    f.ui.event({trip_id:'A',action:'arrived',success:true,status:'arrived',connected:false,
      native_poi_available:true,native_place:{name:'A 내비 장소',address:'A 주소'}});
    await f.ui.settled();
    assert.equal(f.ui.navigation().available, false, 'A connection loss applies across destination identities');
    assert.equal(f.ui.navigation().status, 'ready', 'A different trip cannot replace current route status');
    assert.equal(f.node('detail-name').textContent, 'B 내비 장소');
    assert.equal(f.ui.pending(), true, 'An earlier trip event cannot cancel the current route timer');
    assert.equal(f.clearedTimers.length, 0);
    assert.equal(f.requests.length, 0, 'An earlier trip cannot record arrival for the current trip');
  }
  {
    const f = fixture();
    f.ui.nativeBridge(); f.ui.setTrip(trip('A', 'saved'));
    f.ui.setNavigation({trip_id:'A',available:false,status:'arrived'});
    f.ui.event({trip_id:'A',connected:true,status:'guiding',action:'start',success:true});
    await f.ui.settled();
    assert.equal(f.ui.navigation().available, true, 'Completed trips still receive app connection health');
    assert.equal(f.ui.navigation().status, 'arrived', 'Completed trips cannot resume from a delayed event');
    assert.equal(f.node('guidance-button').disabled, true);
    assert.equal(f.requests.length, 0);
  }
  {
    const f = fixture();
    f.ui.setTrip(trip('B'));
    f.ui.setNavigation({trip_id:'B',available:false,status:'ready'});
    f.ui.event({trip_id:'A',status:'guiding',action:'start',success:true});
    assert.equal(f.ui.navigation().available, false, 'An unrelated route status cannot imply global connection health');
    assert.equal(f.ui.navigation().status, 'ready');
  }
  {
    const f = fixture();
    f.ui.setTrip(trip('A'));
    f.ui.setNavigation({trip_id:'A',native_poi_available:true,native_place:{name:'순정 내비 장소 A',address:'확인된 주소'}});
    f.ui.renderTrip();
    assert.equal(f.node('detail-name').textContent,'순정 내비 장소 A');
    assert.equal(f.node('detail-native-match').hidden,false);
    f.ui.setTrip(trip('B')); f.ui.renderTrip();
    assert.equal(f.node('detail-name').textContent,'테스트 장소 B','Another destination cannot inherit an earlier native match');
    assert.equal(f.node('detail-native-match').hidden,true);
    f.ui.setNavigation({trip_id:'B',native_poi_available:false,native_place:{name:'검증되지 않은 이름'}});
    f.ui.renderTrip();
    assert.equal(f.node('detail-name').textContent,'테스트 장소 B','Unverified native names cannot replace the recommendation');
  }
  {
    const f = fixture();
    f.ui.setTrip(trip('A')); f.ui.renderTrip(); f.ui.openManualArrival();
    assert.equal(f.node('manual-arrival-place').textContent, '테스트 장소 A');
    // Another tab changes state before this window has redrawn the old question.
    f.ui.setTrip(trip('B'));
    await f.ui.confirmManualArrival();
    assert.equal(f.requests.length, 0, 'An A confirmation must never mark B arrived');
    assert.equal(f.node('manual-arrival-dialog').hidden, true);
    assert.equal(f.ui.current().status, 'selected');
  }
  {
    const f = fixture();
    f.ui.setTrip(trip('A')); f.ui.renderTrip(); f.ui.openManualArrival();
    f.ui.setTrip(trip('A', 'navigating')); f.ui.renderTrip();
    assert.equal(f.node('manual-arrival-dialog').hidden, true, 'A changed status invalidates displayed confirmation');
    await f.ui.confirmManualArrival();
    assert.equal(f.requests.length, 0);
    f.ui.openManualArrival(); await f.ui.confirmManualArrival();
    assert.equal(f.requests.length, 1, 'The user can explicitly confirm the updated trip');
    assert.equal(f.requests[0].body.trip_id, 'A');
    assert.equal(f.requests[0].body.source, 'manual_confirmation');
  }
  {
    const f = fixture();
    f.ui.setTrip(trip('A')); f.ui.renderTrip(); f.ui.openManualArrival();
    f.ui.closeManualArrival(); await f.ui.confirmManualArrival();
    assert.equal(f.requests.length, 0, 'A cancelled dialog cannot submit a delayed click');
  }
  for (const save of [true, false]) {
    const f = fixture();
    f.ui.setTrip(trip('A', 'arrived')); f.ui.renderTrip();
    assert.equal(f.node('arrival-place').textContent, '테스트 장소 A');
    f.ui.setTrip(trip('B', 'arrived'));
    await f.ui.decideSave(save);
    assert.equal(f.requests.length, 0, 'An A save/decline click must not decide B');
    assert.equal(f.node('arrival-dialog').hidden, true);
  }
  {
    const f = fixture();
    f.ui.setTrip(trip('A', 'arrived')); f.ui.renderTrip();
    f.ui.setTrip(trip('B', 'arrived')); f.ui.renderTrip();
    assert.equal(f.node('arrival-dialog').hidden, true, 'Do not replace a visible question under a pending click');
    await f.ui.decideSave(true);
    assert.equal(f.requests.length, 0);
    f.ui.renderTrip();
    assert.equal(f.node('arrival-place').textContent, '테스트 장소 B');
    await f.ui.decideSave(true);
    assert.equal(f.requests.length, 1);
    assert.equal(f.requests[0].body.trip_id, 'B');
    assert.equal(f.requests[0].body.save, true);
  }
  {
    const f = fixture();
    f.ui.setTrip(trip('A', 'arrived')); f.ui.renderTrip();
    f.ui.setTrip(trip('A', 'saved')); f.ui.renderTrip();
    await f.ui.decideSave(false);
    assert.equal(f.requests.length, 0, 'A completed decision in another tab cannot be reversed');
  }
  console.log('Trip UI: 14 tracking lifecycle, readiness/route timers, connection and identity regressions passed.');
}
run().catch(error => { console.error(error); process.exitCode = 1; });
