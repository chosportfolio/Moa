const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../web/app.js'), 'utf8');
function section(from, to) {
  const start = source.indexOf(from), end = source.indexOf(to, start);
  assert(start >= 0 && end > start);
  return source.slice(start, end);
}
const requests = [];
const context = {
  state: {region:'판교역 출발 · 120분 이내', trip:{id:'selected-destination',
    region:'부산역 도시 기준점 출발 · 40분 이내',
    place:{name:'이재모피자', latitude:35.102, longitude:129.03,
      address:'부산역 도시 기준점 주변 · 상세 주소 확인 필요'}}},
  tripBusy:false, guidanceStarting:false, guidanceCancelling:false, recording:null,
  processing:false, guidanceTimer:null,
  window:{CabinAndroid:{
    openPlacePreview:raw=>requests.push({type:'preview',payload:JSON.parse(raw)}),
    startNavigation:raw=>requests.push({type:'route',payload:JSON.parse(raw)})}},
  renderTrip(){}, clearTimeout(){}, setTimeout(){return 1;}, notify(){},
};
vm.createContext(context);
vm.runInContext(section('  function startGuidance(', '  window.onNativeNavigation =') +
  section('  function previewNativePlace(', '  function openManualArrival('), context);
context.previewNativePlace();
context.startGuidance();
assert.equal(requests.length,2);
for (const {payload} of requests) {
  assert.equal(payload.place.name,'이재모피자');
  assert.equal(payload.place.region,'', 'Search origin must never become a destination region');
  assert.equal(payload.place.latitude,35.102);
  assert.equal(payload.place.longitude,129.03);
  assert.equal(payload.trip_id,'selected-destination');
}
context.guidanceStarting=false;
context.state.trip.place={name:'행복칼국수',region:'강릉',latitude:37.75,longitude:128.9};
context.previewNativePlace();context.startGuidance();
for (const {payload} of requests.slice(2)) {
  assert.equal(payload.place.region,'강릉','An explicit destination region must survive an unrelated origin');
}
console.log('Navigation payload: destination preserved; origin/time labels excluded from preview and routing.');
