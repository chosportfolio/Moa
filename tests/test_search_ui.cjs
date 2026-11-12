// Exercise the actual search, HTTP error handling and card event handlers.
// DOM/fetch doubles keep these regressions independent of live locations and data.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '..', 'web', 'app.js'), 'utf8');
function section(from, until) {
  const start = source.indexOf(from), end = source.indexOf(until, start);
  assert(start >= 0 && end > start, `Missing source function boundary: ${from}`);
  return source.slice(start, end);
}
function fixture(initial = {}) {
  const nodes = new Map(), requests = [], replies = [], notices = [], jobs = [], selected = [];
  const node = id => {
    if (!nodes.has(id)) nodes.set(id, {
      hidden: true, disabled: false, value: '', textContent: '', innerHTML: '', dataset: {},
      classList: {toggle() {}}, focus() {}, select() {}, listeners: new Map(),
      addEventListener(type, handler) {
        this.listeners.set(type, [...(this.listeners.get(type) || []), handler]);
      },
      async dispatch(type, event = {}) {
        await Promise.all((this.listeners.get(type) || []).map(handler => handler(event)));
        // Delegated click handlers may start, rather than return, an async save.
        await new Promise(resolve => setImmediate(resolve));
      },
    });
    return nodes.get(id);
  };
  const sandbox = {
    $: node, document: {querySelector: selector => node(selector)}, AbortController,
    setTimeout: () => 1, clearTimeout() {},
    asArray: value => Array.isArray(value) ? value : value ? [value] : [],
    escapeHtml: value => String(value ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[ch])),
    notify: message => notices.push(message), switchTab() {},
    setDialogOpen(id, open) { node(id).hidden = !open; return true; },
    watchJob: (id, purpose) => jobs.push({id, purpose}),
    refreshState: async () => {}, renderTrip() {}, renderRecommendationSummary() {},
    // Playback has its own native-speaker tests; search rendering only invokes it.
    renderSpeakerControls() {},
    safeLink: value => value || '', seatInfo: () => ({label: '운전자', color: '#000'}),
    selectPlace: id => selected.push(id),
    fetch: async (url, options) => {
      requests.push({url, method: options.method, body: JSON.parse(options.body)});
      const response = replies.shift() || {status: 202, payload: {job_id: 'test-recommendation'}};
      return {ok: response.status >= 200 && response.status < 300, status: response.status,
        text: async () => JSON.stringify(response.status === 200 ? {state_revision: requests.length, ...response.payload} : response.payload)};
    },
  };
  vm.createContext(sandbox);
  vm.runInContext(`
    let state = {segments:[{text:'서울에서 조용한 카페 가자', final:true}], recommendations:[], models:{ready:true}};
    let recording = null, processing = false, regionSaving = false, streamFailure = null;
    let guidanceStarting = false, guidanceCancelling = false;
    let processingPurpose = 'meeting', processingMessage = '', showPlaceDetail = false;
    let regionDraftDirty = false, recommendAfterRegion = false, areaChoiceKey = null, placesSignature = '';
    let nativeStreaming = false, streamRunning = false, renderCount = 0;
    let stateRequestSequence = 0, stateMetadataSequence = 0;
    const stateResponseSequences = new WeakMap();
    const stateMetadataKeys = new Set(['status', 'models', 'location', 'seat_input', 'seat_channels', 'job', 'errors']);
    ${section('  async function request(', '  async function refreshState(')}
    ${section('  function searchMode(', '  function renderCrew(')}
    ${section('  function constraintRows(', '  function recommendationSpeech(')}
    ${section('  function personalRatingMarkup(', '  function summaryHeading(')}
    ${section('  function reasonList(', '  function sameTripContext(')}
    ${section('  function renderControls(', '  async function startRecording(')}
    ${section('  function openRegionPicker(', '  function openEditor(')}
    function conversationRows() { return state.segments; }
    function render() { renderCount++; renderLocation(); renderControls(); }
    ${section("  $('places-list').addEventListener(", "  $('trip-cancel-button').addEventListener(")}
    ${section("  $('recommend-button').addEventListener(", "  $('transcript-list').addEventListener(")}
    this.ui = {recommend, saveRegion, useConversationBasis, openRegionPicker, renderPlaces, reasonList,
      setState(next) { state = {...state, ...next}; render(); },
      current() { return state; }, mode: searchMode, renderCount: () => renderCount};
  `, sandbox);
  sandbox.ui.setState(initial);
  return {ui: sandbox.ui, node, requests, notices, jobs, selected,
    reply(status, payload) { replies.push({status, payload}); }};
}
// Minimal ancestry for the two selectors used by delegated place clicks.
function target(tag, parent = null, dataset = {}) {
  return {tag, parent, dataset,
    closest(selector) {
      const matches = node => selector.split(',').some(part => {
        const value = part.trim();
        if (value === '[data-place-id]') return Object.hasOwn(node.dataset, 'placeId');
        if (value === '[data-area-choice]') return Object.hasOwn(node.dataset, 'areaChoice');
        if (value === '[data-place-filter]') return Object.hasOwn(node.dataset, 'placeFilter');
        return value === node.tag;
      });
      for (let node = this; node; node = node.parent) if (matches(node)) return node;
      return null;
    },
  };
}
const event = target => ({target, preventDefault() { this.defaultPrevented = true; }});
let passed = 0;
async function test(name, body) {
  try { await body(); passed++; }
  catch (error) { error.message = `${name}: ${error.message}`; throw error; }
}
async function run() {
  await test('Conversation is the default and explicit areas work without current location', async () => {
    const f = fixture({location: {available: false}});
    assert.equal(f.ui.mode(), 'conversation');
    assert.equal(f.node('recommend-button').disabled, false);
    await f.node('recommend-button').dispatch('click');
    assert.deepEqual(f.requests, [{url:'/api/recommend', method:'POST', body:{mode:'conversation'}}]);
    assert.equal(f.node('region-dialog').hidden, true);
    assert.equal(f.jobs.length, 1);
  });
  await test('Conversation requests do not inherit a stale region or client coordinates', async () => {
    const f = fixture({search_mode:'conversation', region:'이전 검색 지역',
      location:{available:true, latitude:37.5, longitude:127.0, accuracy_m:25}});
    f.node('region-input').value = '이전에 입력한 다른 지역';
    await f.ui.recommend();
    assert.deepEqual(f.requests[0].body, {mode:'conversation'});
  });
  await test('An accepted lookup renders its local summary before the job finishes', async () => {
    const f = fixture();
    const renders = f.ui.renderCount();
    f.reply(202, {job_id:'pending-places', state:{state_revision:1,
      recommendation_summary:{title:'대화에서 나온 조건', text:'조용한 카페를 원해요.'}, recommendations:[]}});
    await f.ui.recommend();
    assert.equal(f.ui.current().recommendation_summary.text, '조용한 카페를 원해요.');
    assert(f.ui.renderCount() > renders, 'Render the accepted summary without waiting for a periodic GET');
    assert.equal(f.jobs[0].id, 'pending-places');
  });
  await test('Current-location mode requires a location and leaves coordinates to the server', async () => {
    for (const available of [false, true]) {
      const f = fixture({search_mode:'current', location:{available, latitude:37.5, longitude:127.0}});
      await f.ui.recommend();
      assert.equal(f.requests.length, available ? 1 : 0);
      assert.equal(f.node('region-dialog').hidden, available);
      if (available) assert.deepEqual(f.requests[0].body, {mode:'current'});
    }
  });
  await test('Manual mode sends the chosen region and requires nonempty input', async () => {
    const f = fixture({search_mode:'manual', location:{available:false}});
    f.node('region-input').value = '   ';
    await f.ui.recommend();
    assert.equal(f.requests.length, 0);
    assert.equal(f.node('region-dialog').hidden, false);
    f.node('region-input').value = '  부산 해운대  ';
    await f.ui.recommend();
    assert.deepEqual(f.requests[0].body, {mode:'manual', region:'부산 해운대'});
  });
  await test('A conflict choice retries within the conversation instead of fixing a manual region', async () => {
    const f = fixture({location:{available:false}});
    f.reply(409, {error:'서울과 부산 중 이번에 갈 지역을 골라 주세요.', area_choices:['서울', '부산', 42], area_choice_key:'dialogue-key'});
    await f.ui.recommend();
    assert.equal(f.node('region-dialog').hidden, false);
    assert.equal(f.node('area-choice-section').hidden, false);
    assert.match(f.node('area-choice-list').innerHTML, /data-area-choice="서울"/);
    assert.match(f.node('area-choice-list').innerHTML, /data-area-choice="부산"/);
    assert.doesNotMatch(f.node('area-choice-list').innerHTML, /42/);
    assert.equal(f.node('region-picker-error').hidden, false);
    assert.equal(f.notices.length, 0);
    f.reply(202, {job_id:'chosen-area-job'});
    const choice = target('button', null, {areaChoice:'부산'});
    await f.node('area-choice-list').dispatch('click', event(target('span', choice)));
    assert.deepEqual(f.requests.slice(1), [
      {url:'/api/recommend', method:'POST', body:{mode:'conversation', area_choice:{region:'부산',key:'dialogue-key'}}},
    ]);
    assert.equal(f.node('region-dialog').hidden, true);
    assert.equal(f.jobs.length, 1);
    assert.equal(f.ui.mode(), 'conversation');
  });
  await test('Returning to conversation basis retries without the previous manual region', async () => {
    const f = fixture({search_mode:'manual', region:'부산', location:{available:false}});
    f.ui.openRegionPicker(true);
    f.reply(200, {search_mode:'conversation', region:''});
    await f.node('current-location-button').dispatch('click');
    assert.deepEqual(f.requests, [
      {url:'/api/search-mode', method:'POST', body:{mode:'conversation'}},
      {url:'/api/recommend', method:'POST', body:{mode:'conversation'}},
    ]);
    assert.equal(f.node('region-dialog').hidden, true);
  });
  await test('An excluded-only area opens manual input even without choice buttons', async () => {
    const f = fixture({location:{available:true}});
    f.reply(409, {error:'피하고 싶은 지역 외에 가고 싶은 지역을 골라 주세요.',
      needs_area_choice:true, area_choices:[], area_choice_key:'excluded-key', search_intent:{needs_area_choice:true}});
    await f.ui.recommend();
    assert.equal(f.node('region-dialog').hidden, false);
    assert.equal(f.node('area-choice-section').hidden, true);
    assert.equal(f.node('region-picker-error').hidden, false);
    assert.equal(f.requests.length, 1);
    assert.equal(f.jobs.length, 0);
    f.node('region-picker-input').value = '  강릉  ';
    f.reply(202, {job_id:'custom-area-job'});
    await f.ui.saveRegion(event());
    assert.deepEqual(f.requests[1], {url:'/api/recommend', method:'POST',
      body:{mode:'conversation', area_choice:{region:'강릉',key:'excluded-key',custom:true}}});
    assert.equal(f.ui.mode(), 'conversation');
  });
  await test('A stale conflict decision never falls back to a persistent manual region', async () => {
    const f = fixture();
    f.ui.openRegionPicker(true, ['부산','강릉'], 'old-dialogue-key');
    f.reply(409, {error:'대화 내용이 바뀌었어요. 현재 대화로 장소 추천을 다시 눌러 주세요.'});
    await f.node('area-choice-list').dispatch('click', event(target('button', null, {areaChoice:'부산'})));
    assert.equal(f.requests.length, 1);
    assert.equal(f.requests[0].url, '/api/recommend');
    assert.equal(f.ui.mode(), 'conversation');
    assert.equal(f.jobs.length, 0);
    assert.match(f.notices[0], /대화 내용이 바뀌었어요/);
    assert.equal(f.node('region-dialog').hidden, true);
  });
  await test('A failed region save keeps the dialog and cannot start a recommendation', async () => {
    const f = fixture();
    f.ui.openRegionPicker(true, ['서울', '부산']);
    f.node('region-picker-input').value = '서울';
    f.reply(400, {error:'지역을 확인해 주세요.'});
    await f.ui.saveRegion(event());
    assert.equal(f.requests.length, 1);
    assert.equal(f.requests[0].url, '/api/region');
    assert.equal(f.node('region-dialog').hidden, false);
    assert.equal(f.node('region-picker-error').hidden, false);
    assert.equal(f.node('region-picker-save').disabled, false);
    assert.equal(f.jobs.length, 0);
  });
  await test('Empty conversations and repeated clicks do not create extra jobs', async () => {
    const empty = fixture({segments:[]});
    await empty.ui.recommend();
    assert.equal(empty.requests.length, 0);
    const f = fixture();
    await Promise.all([f.ui.recommend(), f.ui.recommend()]);
    assert.equal(f.requests.length, 1);
    assert.equal(f.node('recommend-button').disabled, true);
  });
  await test('Expanding checks or following a source link does not select the place', async () => {
    const f = fixture(), card = target('article', null, {placeId:'test-place'});
    const details = target('details', card), link = target('a', card);
    for (const clicked of [details, target('summary', details), target('li', details), link, target('span', link)]) {
      await f.node('places-list').dispatch('click', event(clicked));
    }
    assert.deepEqual(f.selected, []);
    await f.node('places-list').dispatch('click', event(target('p', card)));
    assert.deepEqual(f.selected, ['test-place']);
  });
  await test('Native name buttons activate once for Enter or Space', async () => {
    const f = fixture({recommendations:[{id:'test-place', name:'테스트 카페', cautions:['영업시간 확인']}]});
    f.ui.renderPlaces();
    assert.match(f.node('places-list').innerHTML, /<button type="button" class="place-name-button"/);
    assert.doesNotMatch(f.node('places-list').innerHTML, /<article[^>]+(?:tabindex|role="button")/);
    const card = target('article', null, {placeId:'test-place'}), button = target('button', card);
    for (const key of ['Enter', ' ']) {
      const before = f.selected.length;
      // Model the native button's single click as well as bubbling key events.
      // This catches an added JS key handler that would double-select the card.
      await f.node('places-list').dispatch('keydown', {...event(button), key});
      await f.node('places-list').dispatch('click', event(button));
      await f.node('places-list').dispatch('keyup', {...event(button), key});
      assert.equal(f.selected.length - before, 1);
    }
  });
  await test('Twelve varied results remain browsable by category without losing candidates', async () => {
    const places = Array.from({length:12}, (_,i) => ({id:'p'+i, name:'후보 '+i, group:i<6?'음식점':'카페·디저트'}));
    const f = fixture({recommendations:places});
    f.ui.renderPlaces();
    assert.equal((f.node('places-list').innerHTML.match(/data-place-id=/g)||[]).length,12);
    await f.node('place-filters').dispatch('click',event(target('button',null,{placeFilter:'음식점'})));
    assert.equal((f.node('places-list').innerHTML.match(/data-place-id=/g)||[]).length,6);
    assert.match(f.node('place-filters').innerHTML,/aria-pressed="true">음식점/);
    assert.equal(f.ui.current().recommendations.length,12);
    await f.node('place-filters').dispatch('click',event(target('button',null,{placeFilter:'전체'})));
    assert.equal((f.node('places-list').innerHTML.match(/data-place-id=/g)||[]).length,12);
  });
  await test('Evidence quotes, route limits and personal ratings are factual and escaped', async () => {
    const f=fixture({recommendations:[{id:'evidence',name:'칼국수 식당',group:'음식점',matched_topics:['칼국수'],
      reasons:[{seat:'driver',text:'메뉴 등록 정보',quote:'<img src=x> 칼국수 먹자'}],
      cautions:['주차 정보 없음'],my_rating:{rating:4,note:'<script>메모</script>'},
      travel_constraint:{verified:true,within_limit:true,message:'도로 예상 80분',origin_label:'원주 도시 기준점',source:'OSRM · OpenStreetMap',source_url:'https://routing.openstreetmap.de/about.html',attribution_url:'https://www.openstreetmap.org/copyright',fix_map_url:'https://www.openstreetmap.org/edit'}}]});
    f.ui.renderPlaces();
    const html=f.node('places-list').innerHTML;
    assert.match(html,/내 평가 <strong>★ 4/);
    // Cards use concise summaries; the selected-place detail renders full quotes.
    assert.match(html,/메뉴 등록 정보/);
    const evidenceHtml=f.ui.reasonList(f.ui.current().recommendations[0]);
    assert.match(evidenceHtml,/관련 발언/);
    assert.match(evidenceHtml,/&lt;img src=x&gt; 칼국수 먹자/);
    assert.doesNotMatch(evidenceHtml,/<script>|<img/);
    assert.match(html,/&lt;script&gt;/);
    assert.match(html,/원주 도시 기준점/);
    assert.match(html,/실시간 교통 미반영/);
    assert.match(html,/OSRM · OpenStreetMap/);
    assert.match(html,/https:\/\/www.openstreetmap.org\/copyright/);
    assert.match(html,/https:\/\/www.openstreetmap.org\/edit/);
    assert.match(html,/방문 전 확인할 정보 1개/);
    assert.doesNotMatch(html,/<script>|<img/);
    f.ui.setState({recommendations:[{id:'no-rating',name:'아직 평가 없는 곳'}]});
    f.ui.renderPlaces();
    assert.doesNotMatch(f.node('places-list').innerHTML,/내 평가|★/);
  });
  await test('Missing conversation input is distinguished from unknown venue facts',async()=>{
    const f=fixture({recommendations:[{id:'p',name:'후보'}],request_profile:{needs_more_preferences:true}});
    f.ui.renderPlaces();
    assert.match(f.node('results-guidance').textContent,/대화가 부족/);
    f.ui.setState({request_profile:{needs_more_preferences:false,explanation:'조건을 반영했어요.'}});
    f.ui.renderPlaces();
    assert.equal(f.node('results-guidance').textContent,'조건을 반영했어요.');
  });
  console.log(`Search UI: ${passed} request, region-choice and place-card contracts passed.`);
}
run().catch(error => { console.error(error); process.exitCode = 1; });
