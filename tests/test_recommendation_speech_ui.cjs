// Test production UI handlers in isolation; no microphone, network or saved state.
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const source=fs.readFileSync(path.join(__dirname,'../web/app.js'),'utf8');
function section(start,end){const a=source.indexOf(start),b=source.indexOf(end,a);assert(a>=0&&b>a,start);return source.slice(a,b);}
function fixture(overrides={}){
  const spoken=[],replies=[],actions=[],notices=[],nodes=new Map(),timers=[];
  let now=100000;
  const context={state:{conversation_id:'current',segments:[{text:'파스타 먹자'}],recommendations:[],models:{ready:true}},
    initialStateLoaded:true,stateSnapshotVersion:0,nativeSpeaker:{available:true,state:'ready'},voiceRecommendationRequest:null,
    recording:null,processing:false,processingPurpose:'',processingMessage:'',nativePendingAudio:false,
    streamFailure:null,streamRunning:false,streamQueue:[],nativeStreaming:true,nativeStartTimer:null,activeJob:null,
    activeDialog:null,regionSaving:false,guidanceStarting:false,guidanceCancelling:false,
    asArray:v=>Array.isArray(v)?v:[],Date:{now:()=>now},
    setTimeout(fn,delay){timers.push({fn,delay});return timers.length;},clearTimeout(){},
    $(id){if(!nodes.has(id))nodes.set(id,{hidden:false,disabled:false,textContent:'',attrs:{},setAttribute(k,v){this.attrs[k]=v;}});return nodes.get(id);},
    render(){context.renderSpeakerControls();},renderControls(){context.renderSpeakerControls();},
    notify(message){notices.push(message);},switchTab(tab){actions.push(['tab',tab]);},
    async refreshState(){context.stateSnapshotVersion++;},
    startRecording(){actions.push(['record']);},recommend(){actions.push(['recommend']);},
    drainStreamQueue(){actions.push(['web-upload']);},
    window:{CabinAndroid:{
      speakRecommendations(text){spoken.push(text);return context.speakAccepted!==false;},
      stopRecommendationSpeech(){actions.push(['stop-speaker']);},
      completeGleoAction(id,status,message,data){replies.push({id,status,message,data:JSON.parse(data)[0]});return true;},
      retrySpeechResults(){actions.push(['native-retry']);},
      discardSpeechResults(){actions.push(['native-discard']);return context.discardAccepted!==false;}
    }},...overrides};
  vm.createContext(context);
  vm.runInContext(section('  function recommendationSpeech()','  function personalRatingMarkup(')
    +section('  window.onNativeStatus =','  function audioBlob(')
    +section('  window.onNativeSpeech =','  // The native bridge needs')
    +section('  function retryStreamAudio()','  window.onNativeSaveResult')
    +section('  const gleoHandledActions = new Set();','  setInterval(() => {'),context);
  return {context,spoken,replies,actions,notices,nodes,
    run:(id='cmd',expiresAt=now+5000)=>context.window.onGleoAction({id,action:'recommend_places',expiresAt}),
    advance(ms){now+=ms;},fireTimeout(){const t=timers.shift();assert(t);now+=t.delay;t.fn();}};
}
const place=(name,index=0)=>({name,reason_summary:'대화에서 파스타를 원했어요. '+index,cautions:['메뉴 확인','가격 확인','주차 확인','영업 확인']});
const status=(extra={})=>({source:'pleos_on_device',backgroundManaged:true,type:'status',conversation_id:'current',session_id:'session',recording:false,pending_results:false,...extra});
const tests=[];
function test(name,check){tests.push({name,check});}
function deferred(){let resolve;const promise=new Promise(done=>resolve=done);return {promise,resolve};}

test('Speech contains at most three places, sample label and uncertainty',()=>{
  const f=fixture();f.context.state.demo=true;f.context.state.recommendations=[null,{},place('첫집'),place('둘집'),place('셋집'),place('넷집')];
  const text=f.context.recommendationSpeech();
  assert(text.includes('샘플 대화'));assert(text.includes('3번째는 셋집'));assert(!text.includes('넷집'));
  assert(text.includes('확인되지 않은 정보'));assert(text.includes('방문 전에 확인'));
  f.context.state.recommendations=Array.from({length:4},(_,i)=>({name:String(i)+'가'.repeat(300),reason_summary:'나'.repeat(600),cautions:['확인']}));
  assert(f.context.recommendationSpeech().length<=1800);
});
test('Empty recommendations never invoke native playback',()=>{
  const f=fixture();assert.equal(f.context.recommendationSpeech(),'');assert.equal(f.context.speakRecommendations(),false);assert.equal(f.spoken.length,0);
});
test('Recording, processing, pending audio and unavailable speaker block playback',()=>{
  for(const change of [{recording:{}},{processing:true},{nativePendingAudio:true},{nativeSpeaker:{available:false,state:'unavailable'}}]){
    const f=fixture(change);f.context.state.recommendations=[place('첫집')];assert.equal(f.context.speakRecommendations(),false);assert.equal(f.spoken.length,0);
    f.context.renderSpeakerControls();assert.equal(f.nodes.get('recommend-listen').disabled,true);
  }
});
test('Busy speaker keeps the stop control available and completion restores playback',()=>{
  const f=fixture();f.context.state.recommendations=[place('첫집')];assert.equal(f.context.speakRecommendations(),true);
  assert.equal(f.context.nativeSpeaker.state,'preparing');assert.equal(f.nodes.get('recommend-listen').textContent,'읽기 멈추기');
  f.context.window.onNativeSpeaker({available:true,state:'speaking'});assert.equal(f.nodes.get('recommend-listen').disabled,false);
  f.context.window.onNativeSpeaker({available:true,state:'done'});assert.equal(f.nodes.get('recommend-listen').textContent,'추천 듣기');
  f.context.window.onNativeSpeaker({available:true,state:'error',error:'재생 실패'});assert(f.notices.includes('재생 실패'));
});
test('Voice follow-up is one-shot and belongs to the same unexpired conversation',()=>{
  for(const request of [{conversationId:'old',deadline:200000},{conversationId:'current',deadline:99999}]){
    const f=fixture({voiceRecommendationRequest:request});f.context.state.recommendations=[place('첫집')];f.context.speakRequestedRecommendations();assert.equal(f.spoken.length,0);assert.equal(f.context.voiceRecommendationRequest,null);
  }
  const f=fixture({voiceRecommendationRequest:{conversationId:'current',deadline:200000}});f.context.state.recommendations=[place('첫집')];
  f.context.speakRequestedRecommendations();f.context.speakRequestedRecommendations();assert.equal(f.spoken.length,1);
});
test('Cached Gleo response uses a fresh snapshot and at most three bounded candidates',async()=>{
  const f=fixture();f.context.state.recommendations=[place('오래된집')];
  f.context.refreshState=async()=>{f.context.stateSnapshotVersion++;f.context.state.recommendations=[place('새집1'),place('새집2'),place('새집3'),place('새집4')];};
  await f.run();assert.equal(f.replies[0].data.state,'ready');assert.equal(f.replies[0].data.count,4);
  assert.deepEqual(f.replies[0].data.places.map(p=>p.name),['새집1','새집2','새집3']);
  assert(f.replies[0].data.places.every(p=>p.unverified.length===3));assert(!JSON.stringify(f.replies).includes('오래된집'));
});
test('A failed refresh cannot read stale cached recommendations',async()=>{
  const f=fixture();f.context.state.recommendations=[place('오래된집')];f.context.refreshState=async()=>{};
  await f.run();assert.equal(f.replies[0].status,'asking');assert.equal(f.replies[0].data.state,'refresh_required');assert.equal(f.actions.length,0);
});
test('Server-invalidated recommendations trigger a new search instead of stale speech',async()=>{
  const f=fixture();f.context.state.recommendations=[place('오래된집')];
  f.context.refreshState=async()=>{f.context.stateSnapshotVersion++;f.context.state.recommendations=[];};
  await f.run();assert.equal(f.replies[0].data.state,'requested');assert(f.actions.some(a=>a[0]==='recommend'));assert(!JSON.stringify(f.replies).includes('오래된집'));
});
test('A command expiring during refresh cannot announce results or start search',async()=>{
  const load=deferred(),f=fixture();f.context.refreshState=()=>load.promise;const task=f.run();
  f.context.stateSnapshotVersion++;f.advance(6000);load.resolve();await task;assert.equal(f.replies.length,0);assert.equal(f.actions.length,0);
});
test('Managed final text is not uploaded twice by the WebView',()=>{
  const f=fixture();f.context.window.onNativeSpeech({...status(),type:'transcript',status:'final',sequence:1,text:'파스타 먹자'});
  assert.equal(f.context.streamQueue.length,0);assert(!f.actions.some(a=>a[0]==='web-upload'));
});
test('Managed commit invalidates local recommendations before asynchronous refresh finishes',()=>{
  const f=fixture({voiceRecommendationRequest:{conversationId:'current',deadline:200000}});
  f.context.state.recommendations=[place('오래된집')];f.context.state.recommendation_summary={text:'오래된 요약'};f.context.refreshState=()=>new Promise(()=>{});
  f.context.window.onNativeSpeech({...status(),type:'transcript',status:'committed'});
  assert.equal(f.context.state.recommendations.length,0);assert.equal(Object.keys(f.context.state.recommendation_summary).length,0);
  assert.equal(f.context.voiceRecommendationRequest,null);assert(f.actions.some(a=>a[0]==='stop-speaker'));
  f.context.state.recommendations=[place('현재집')];f.context.window.onNativeSpeech({...status(),type:'transcript',status:'committed',conversation_id:'old'});
  assert.equal(f.context.state.recommendations[0].name,'현재집');
});
test('Managed recording resumes with elapsed time and stops while retaining pending upload',()=>{
  const f=fixture();f.context.window.onNativeSpeech(status({recording:true,end:12}));
  assert.equal(f.context.recording.startedAt,88000);assert.equal(f.context.recording.backgroundManaged,true);
  f.context.window.onNativeSpeech(status({recording:false,pending_results:true}));
  assert.equal(f.context.recording,null);assert.equal(f.context.nativePendingAudio,true);assert.equal(f.context.processing,true);
  f.context.window.onNativeSpeech(status());assert.equal(f.context.nativePendingAudio,false);assert.equal(f.context.processing,false);
});
test('Managed upload failure retries natively and clears only after authoritative completion',()=>{
  const f=fixture();f.context.window.onNativeSpeech(status({pending_results:true,upload_error:true,error:'연결 오류'}));
  assert.equal(f.context.streamFailure.nativeManaged,true);assert.equal(f.context.processing,true);
  f.context.retryStreamAudio();assert.equal(f.context.streamFailure.retrying,true);assert(f.actions.some(a=>a[0]==='native-retry'));
  assert(!f.actions.some(a=>a[0]==='web-upload'));f.context.window.onNativeSpeech(status());
  assert.equal(f.context.streamFailure,null);assert.equal(f.context.processing,false);
});
test('Discard rejection keeps pending service data and acceptance waits for completion',()=>{
  const f=fixture();f.context.window.onNativeSpeech(status({pending_results:true,upload_error:true,error:'연결 오류'}));
  f.context.discardAccepted=false;f.context.discardStreamAudio();assert(f.context.streamFailure);assert.equal(f.context.streamFailure.retrying,false);
  f.context.discardAccepted=true;f.context.discardStreamAudio();assert.equal(f.context.streamFailure.retrying,true);
  const count=f.actions.filter(a=>a[0]==='native-discard').length;f.context.discardStreamAudio();assert.equal(f.actions.filter(a=>a[0]==='native-discard').length,count);
  f.context.window.onNativeSpeech(status());assert.equal(f.context.streamFailure,null);assert(!f.actions.some(a=>a[0]==='web-upload'));
});
test('Missing native retry bridge leaves recoverable error instead of silently discarding data',()=>{
  const f=fixture();f.context.window.onNativeSpeech(status({pending_results:true,upload_error:true,error:'연결 오류'}));
  delete f.context.window.CabinAndroid.retrySpeechResults;f.context.retryStreamAudio();
  assert(f.context.streamFailure);assert.equal(f.context.streamFailure.retrying,false);assert(f.notices.some(s=>s.includes('다시 열어')));
});
test('Pending data from an old conversation can recover without becoming the current recording',()=>{
  const f=fixture();f.context.window.onNativeSpeech(status({conversation_id:'old',pending_results:true,upload_error:true,error:'이전 발언 보관 중'}));
  assert.equal(f.context.recording,null);assert.equal(f.context.streamFailure.nativeManaged,true);
  f.context.window.onNativeSpeech(status({conversation_id:'old'}));assert.equal(f.context.streamFailure,null);assert.equal(f.context.recording,null);
});
(async()=>{let passed=0;for(const {name,check} of tests){try{await check();passed++;}catch(error){error.message=name+': '+error.message;throw error;}}console.log('Recommendation speech UI: '+passed+' checks passed.');})().catch(error=>{console.error(error);process.exitCode=1;});
