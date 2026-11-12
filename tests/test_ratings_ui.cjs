// Personal ratings use the selected saved visit; no aggregate rating is invented.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../web/app.js'), 'utf8');
const start = source.indexOf('  function openRating(');
const end = source.indexOf('  async function applyTripResult(', start);
assert(start > 0 && end > start);
function fixture() {
  const nodes = new Map(), requests = [];
  const node = id => {
    if (!nodes.has(id)) nodes.set(id, {value:'',hidden:true,disabled:false,textContent:''});
    return nodes.get(id);
  };
  const choices = [1,2,3,4,5].map(value => ({value:String(value),checked:false}));
  const context = {
    state:{saved_places:[
      {id:'actual-visit',place:{id:'shared-place',name:'실제 방문',my_rating:{rating:4,note:'실제 메모'}}},
      {id:'demo-visit',demo:true,place:{id:'shared-place',name:'샘플 방문',my_rating:{rating:2,note:'샘플 메모'}}},
    ]}, ratingPlaceId:null, ratingVisitId:null, recording:null, processing:false,
    $:node, asArray:value=>Array.isArray(value)?value:[],
    document:{querySelectorAll:()=>choices,querySelector:()=>choices.find(choice=>choice.checked)},
    setDialogOpen(id,open){node(id).hidden=!open;},notify(){},applyState(){},async refreshState(){},
    async jsonRequest(url,method,body){requests.push({url,method,body});if(context.failure)throw new Error('저장 실패');return {};},
  };
  vm.createContext(context);vm.runInContext(source.slice(start,end),context);
  return {context,node,choices,requests};
}
(async()=>{
  const f=fixture();
  f.context.openRating('shared-place','demo-visit');
  assert.equal(f.node('rating-place-name').textContent,'샘플 방문');
  assert.equal(f.node('rating-note').value,'샘플 메모');
  assert.equal(f.choices.find(choice=>choice.checked).value,'2');
  f.choices.forEach(choice=>{choice.checked=choice.value==='5';});
  f.node('rating-note').value='  다음에 다시 가기  ';
  await f.context.saveRating({preventDefault(){}});
  assert.equal(JSON.stringify(f.requests[0]),JSON.stringify({url:'/api/ratings',method:'POST',body:{place_id:'shared-place',visit_id:'demo-visit',rating:5,note:'다음에 다시 가기'}}));
  assert.equal(f.node('rating-dialog').hidden,true);
  const failed=fixture();failed.context.openRating('shared-place','actual-visit');failed.context.failure=true;
  await failed.context.saveRating({preventDefault(){}});
  assert.equal(failed.node('rating-dialog').hidden,false);
  assert.equal(failed.node('rating-note').value,'실제 메모');
  assert.equal(failed.node('rating-save').disabled,false);
  assert.equal(failed.node('rating-error').textContent,'저장 실패');
  const invalid=fixture();invalid.context.openRating('shared-place','missing-visit');
  await invalid.context.saveRating({preventDefault(){}});
  assert.equal(invalid.requests.length,0);
  console.log('Personal rating UI: visit scope, restored values, persistence and retry passed.');
})().catch(error=>{console.error(error);process.exitCode=1;});
