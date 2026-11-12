const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(require('node:path').join(__dirname,'../web/app.js'),'utf8');
const section = (from,to) => source.slice(source.indexOf(from),source.indexOf(to,source.indexOf(from)));
(async () => {
  const calls = [], notices = [], scroll = {};
  const ctx = {tripBusy:false, recording:null, processing:false, guidanceStarting:false, guidanceCancelling:false,
    nativeNavigation:{available:true}, showPlaceDetail:false,
    window:{CabinAndroid:{openPlacePreview(){calls.push('preview');}}},
    previewNativePlace(){calls.push('preview');}, refreshNavigationState(){calls.push('state');},
    async jsonRequest(){calls.push('select'); return {trip:{id:'test'}};},
    async applyTripResult(){calls.push('apply');}, switchTab(){calls.push('tab');},
    renderTrip(){calls.push('render');}, $(){return scroll;}, notify:message=>notices.push(message)};
  vm.createContext(ctx); vm.runInContext(section('  async function selectPlace(', '  function refreshNavigationState('),ctx);
  await ctx.selectPlace('test');
  assert.equal(ctx.showPlaceDetail,true);
  assert.equal(calls.includes('preview'),false,'Selecting a recommendation must stay in the app even when native navigation is available');
  assert.equal(scroll.scrollTop,0);
  calls.length=0;ctx.processing=true;await ctx.selectPlace('test');assert.equal(calls.length,0);
  ctx.processing=false;ctx.showPlaceDetail=false;ctx.jsonRequest=async()=>{throw Error('unavailable');};
  await ctx.selectPlace('test');assert.equal(ctx.showPlaceDetail,false);assert.equal(ctx.tripBusy,false);
  assert.equal(notices.length,1);
  console.log('Place selection UI: detail first, busy guard and failure recovery passed.');
})().catch(error=>{console.error(error);process.exitCode=1;});
