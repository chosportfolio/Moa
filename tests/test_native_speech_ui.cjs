const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname,'../web/app.js'),'utf8');
const section = (a,b) => source.slice(source.indexOf(a),source.indexOf(b,source.indexOf(a)));
function fixture(handler) {
  const events=[], nodes=new Map();
  const ctx={recording:{purpose:'meeting',conversationId:'c1',pending:true},state:{conversation_id:'c1'},
    voiceRecommendationRequest:null,nativeStreaming:true,nativeStartTimer:null,nativePendingAudio:false,streamQueue:[],streamRunning:false,streamFailure:null,
    processing:false,processingPurpose:'',processingMessage:'',activeJob:null,
    render(){},renderControls(){},clearTimeout(){},setTimeout:fn=>{queueMicrotask(fn);},
    $(id){if(!nodes.has(id))nodes.set(id,{textContent:'',hidden:false});return nodes.get(id);},
    notify(message){events.push(['notice',message]);},async refreshState(){},request:handler,
    stopRecording(){ctx.recording=null;},window:{CabinAndroid:{acknowledgeSpeech(s,q){events.push(['ack',s,q]);},acknowledgeAudio(){throw Error('Audio ACK used for text');}}}};
  vm.createContext(ctx);
  vm.runInContext(section('  window.onNativeStatus =','  function audioBlob(')+section('  window.onNativeSpeech =','  // The native bridge needs')+section('  async function drainStreamQueue()','  window.onNativeSaveResult'),ctx);
  return {ctx,events,nodes};
}
const final={source:'pleos_on_device',type:'transcript',status:'final',session_id:'s1',conversation_id:'c1',sequence:1,text:'칼국수 먹고 싶어',start:0,end:2,recording_started_at:'2026-11-12T03:03:00Z',recognized_at:'2026-11-12T03:03:02Z'};
const tick=()=>new Promise(resolve=>setImmediate(resolve));
(async()=>{
  let release, posts=[];
  const f=fixture(async(url,options)=>{posts.push([url,JSON.parse(options.body)]);return new Promise(r=>release=r);});
  f.ctx.window.onNativeSpeech({...final,type:'status',status:'listening',recording:true});
  assert.equal(f.ctx.recording.engine,'pleos');
  f.ctx.window.onNativeSpeech({...final,status:'interim',text:'칼국수'});
  assert.equal(posts.length,0);
  assert.equal(f.nodes.get('listening-description').textContent,'칼국수');
  f.ctx.window.onNativeSpeech(final); f.ctx.window.onNativeSpeech(final);
  assert.equal(posts.length,1); assert.equal(f.events.length,0);
  assert.equal(posts[0][0],'/api/speech/final'); assert.equal(posts[0][1].text,final.text);
  assert.equal(posts[0][1].recording_started_at,final.recording_started_at);
  assert.equal(posts[0][1].recognized_at,final.recognized_at);
  release({committed:true}); await tick();
  assert.deepEqual(f.events,[['ack','s1',1]]); assert.equal(f.ctx.streamQueue.length,0);
  const failed=fixture(async()=>{throw Object.assign(Error('old conversation'),{status:409});});
  failed.ctx.window.onNativeSpeech(final); await tick();
  assert.equal(failed.ctx.streamQueue.length,1); assert(failed.ctx.streamFailure);
  assert(!failed.events.some(e=>e[0]==='ack'));
  failed.ctx.request=async()=>({committed:true}); await failed.ctx.retryStreamAudio();
  assert(failed.events.some(e=>e[0]==='ack'));
  const untouched=fixture(async()=>({committed:true}));
  untouched.ctx.window.onNativeSpeech({source:'pleos_on_device',type:'status',status:'error',recording:false,error:'service absent'});
  assert(untouched.ctx.recording,'initialization error cannot cancel the fallback microphone');
  const managed=fixture(async()=>{throw Error('Service final must never upload from WebView');});
  managed.ctx.window.onNativeSpeech({...final,backgroundManaged:true,type:'status',recording:true});
  assert.equal(managed.ctx.recording.backgroundManaged,true);
  managed.ctx.window.onNativeSpeech({...final,backgroundManaged:true,type:'status',status:'microphone_paused',microphone_paused:true,recording:true});
  assert.equal(managed.ctx.recording.microphonePaused,true);
  managed.ctx.$('listening-description').textContent='마이크 대기 중';
  managed.ctx.window.onNativeSpeech({...final,backgroundManaged:true,status:'interim',text:'늦게 도착한 앞 문장'});
  assert.equal(managed.nodes.get('listening-description').textContent,'마이크 대기 중');
  managed.ctx.window.onNativeSpeech({...final,backgroundManaged:true,type:'status',status:'listening',microphone_paused:false,recording:true});
  assert.equal(managed.ctx.recording.microphonePaused,false);
  managed.ctx.window.onNativeSpeech({...final,backgroundManaged:true});
  assert.equal(managed.ctx.streamQueue.length,0);
  managed.ctx.window.onNativeSpeech({...final,backgroundManaged:true,status:'committed'});
  assert.equal(managed.ctx.streamQueue.length,0);
  managed.ctx.window.onNativeSpeech({...final,backgroundManaged:true,type:'status',recording:false,pending_results:false});
  assert.equal(managed.ctx.recording,null);
  managed.ctx.window.onNativeSpeech({...final,backgroundManaged:true,type:'status',recording:true,conversation_id:'old'});
  assert.equal(managed.ctx.recording,null);
  console.log('Native speech UI: interim isolation, final dedup, durable ACK, retry and fallback isolation passed.');
})().catch(e=>{console.error(e);process.exitCode=1;});
