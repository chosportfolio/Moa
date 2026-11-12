(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const seats = [
    {id: 'driver', label: '운전자', number: '01', color: '#3267a6', bg: '#edf3fa'},
    {id: 'passenger', label: '동승자', number: '02', color: '#59616c', bg: '#f5f6f8'},
    {id: 'rear1', label: '뒷좌석 1', number: '03', color: '#59616c', bg: '#f5f6f8'},
    {id: 'rear2', label: '뒷좌석 2', number: '04', color: '#59616c', bg: '#f5f6f8'}
  ];
  let state = {seat_channels: [], segments: [], recommendations: [], constraints: [], models: {ready: false}};
  let recording = null;
  let processing = false;
  let processingPurpose = 'meeting';
  let processingMessage = '';
  let activeJob = null;
  let recorder = null;
  let microphoneStream = null;
  let audioChunks = [];
  let editId = null;
  let transcriptSignature = '';
  let crewSignature = '';
  let transcriptKeys = new Set();
  let unreadMessages = 0;
  let stateFetching = false;
  let stateRefreshPromise = null;
  let stateRefreshQueued = false;
  let stateRequestSequence = 0;
  let stateMetadataSequence = 0;
  const stateResponseSequences = new WeakMap();
  const stateMetadataKeys = new Set(['status', 'models', 'location', 'seat_input', 'seat_channels', 'job', 'errors']);
  let currentTab = 'transcript';
  let placesSignature = '';
  let regionDraftDirty = false;
  let regionSaving = false;
  let recommendAfterRegion = false;
  let areaChoiceKey = null;
  let initialStateLoaded = false;
  let stateSnapshotVersion = 0;
  let stateTimer = null;
  let pollTimer = null;
  let nativeStartTimer = null;
  let recordingLimits = {meetingMaxSeconds: 180};
  let nativeStreaming = false;
  let nativePendingAudio = false;
  let streamQueue = [];
  let streamRunning = false;
  let streamFailure = null;
  let currentStreamSession = null;
  let ratingPlaceId = null;
  let ratingVisitId = null;
  let showPlaceDetail = false;
  let tripBusy = false;
  let manualArrivalContext = null;
  let arrivalDecisionContext = null;
  let savedSignature = '';
  let detailReasonsSignature = '';
  let nativeNavigation = {available: null, status: 'unavailable'};
  let guidanceStarting = false;
  let guidanceCancelling = false;
  let guidanceCancelTimer = null;
  let guidanceTimer = null;
  let guidanceRouteTimerStarted = false;
  let releasedNavigationTrip = null;
  let nativeGuidingTrip = null;
  let navigationEvents = Promise.resolve();
  let activeDialog = null;
  let dialogOpener = null;
  let noticeTimer = null;
  let nativeSpeaker = {available:false, state:'unavailable'};
  let voiceRecommendationRequest = null;

  try {
    if (window.CabinAndroid?.getCapabilities) {
      const capabilities = JSON.parse(window.CabinAndroid.getCapabilities());
      nativeStreaming = capabilities.streaming === true;
      if (typeof capabilities.nativeNavigation?.connected === 'boolean') nativeNavigation.available = capabilities.nativeNavigation.connected;
      if (capabilities.meetingMaxSeconds > 0) recordingLimits.meetingMaxSeconds = capabilities.meetingMaxSeconds;
    }
  } catch { /* Use conservative recording durations if the bridge is unavailable. */ }

  const escapeHtml = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[char]));
  const seatInfo = id => seats.find(seat => seat.id === id) || {id: 'unknown', label: '좌석 확인 필요', color: '#6a707a', bg: '#f5f6f8'};
  const asArray = value => Array.isArray(value) ? value : value ? [value] : [];
  const elapsed = seconds => {
    const value = Math.max(0, Math.floor(Number(seconds) || 0));
    return String(Math.floor(value / 60)).padStart(2, '0') + ':' + String(value % 60).padStart(2, '0');
  };
  const conversationTime = timestamp => {
    if (typeof timestamp !== 'string' || !/^\d{4}-\d{2}-\d{2}T.+(?:Z|[+-]\d{2}:\d{2})$/.test(timestamp)) return '';
    const date = new Date(timestamp);
    if (!Number.isFinite(date.getTime())) return '';
    const hour = date.getHours();
    return (hour < 12 ? '오전 ' : '오후 ') + (hour % 12 || 12) + ':' + String(date.getMinutes()).padStart(2, '0');
  };
  function notify(message, isError = false) {
    if (!message) return;
    clearTimeout(noticeTimer);
    $('notice-text').textContent = message;
    $('notice').hidden = false;
    $('notice').classList.toggle('error', isError);
    if (!isError) noticeTimer = setTimeout(() => { $('notice').hidden = true; }, 6000);
  }

  function canFocusDialogElement(element) {
    return Boolean(element?.isConnected && !element.disabled && !element.closest('[hidden]') && element.getClientRects().length);
  }

  function dialogFocusables(dialog) {
    return [...dialog.querySelectorAll('button, input, select, textarea, a[href], [tabindex]:not([tabindex="-1"])')]
      .filter(element => element.tabIndex >= 0 && canFocusDialogElement(element));
  }

  function setDialogOpen(id, open, initialFocus) {
    const backdrop = $(id);
    const shell = document.querySelector('.app-shell');
    if (open) {
      // Automatic arrival questions wait for the current editor/confirmation.
      // The existing state refresh will present them after that dialog closes.
      if (activeDialog && activeDialog !== id) return false;
      if (activeDialog === id && !backdrop.hidden) return true;
      dialogOpener = document.activeElement;
      activeDialog = id;
      backdrop.hidden = false;
      const target = initialFocus && $(initialFocus);
      (canFocusDialogElement(target) ? target : backdrop.querySelector('[role="dialog"]')).focus();
      shell.inert = true;
      shell.setAttribute('aria-hidden', 'true');
      return true;
    }
    backdrop.hidden = true;
    if (activeDialog !== id) return false;
    const opener = dialogOpener;
    activeDialog = null;
    dialogOpener = null;
    shell.inert = false;
    shell.removeAttribute('aria-hidden');
    requestAnimationFrame(() => {
      if (activeDialog) return;
      const editIndex = opener?.dataset?.editIndex;
      const replacement = /^\d+$/.test(editIndex || '') ? document.querySelector('[data-edit-index="' + editIndex + '"]') : null;
      const target = canFocusDialogElement(opener) ? opener : canFocusDialogElement(replacement) ? replacement : $('tab-' + currentTab);
      if (canFocusDialogElement(target)) target.focus();
    });
    return true;
  }

  function handleDialogKeydown(event) {
    if (!activeDialog || event.isComposing) return;
    const backdrop = $(activeDialog);
    if (event.key === 'Tab') {
      const options = dialogFocusables(backdrop);
      const index = options.indexOf(document.activeElement);
      if (!options.length) {
        event.preventDefault();
        backdrop.querySelector('[role="dialog"]').focus();
      } else if (index < 0 || (event.shiftKey && index === 0) || (!event.shiftKey && index === options.length - 1)) {
        event.preventDefault();
        options[event.shiftKey ? options.length - 1 : 0].focus();
      }
    } else if (event.key === 'Escape') {
      event.preventDefault();
      event.stopPropagation();
      // Escape cancels an open editor; it never decides whether to save a visit.
      if (activeDialog === 'region-dialog' && !regionSaving) setDialogOpen(activeDialog, false);
      else if (activeDialog === 'manual-arrival-dialog' && !tripBusy) closeManualArrival();
      else if (activeDialog === 'edit-dialog' && !$('edit-save').disabled && !recording && !processing) setDialogOpen(activeDialog, false);
      else if (activeDialog === 'reset-dialog' && !$('reset-confirm').disabled && !recording && !processing) setDialogOpen(activeDialog, false);
      else if (activeDialog === 'rating-dialog' && !$('rating-save').disabled) setDialogOpen(activeDialog, false);
    }
  }

  function keepDialogFocus(event) {
    if (!activeDialog || $(activeDialog).contains(event.target)) return;
    const dialog = $(activeDialog);
    (dialogFocusables(dialog)[0] || dialog.querySelector('[role="dialog"]')).focus();
  }

  async function request(path, options = {}) {
    const sequence = ++stateRequestSequence;
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), options.timeout || 30000);
    try {
      const response = await fetch(path, {...options, signal: controller.signal, cache: 'no-store'});
      const text = await response.text();
      let payload;
      try { payload = text ? JSON.parse(text) : {}; } catch { payload = {message: text}; }
      if (!response.ok) {
        const detail = payload.detail;
        const error = new Error(typeof detail === 'string' ? detail : payload.error || payload.message || '요청을 처리하지 못했어요. 잠시 후 다시 시도해 주세요.');
        error.status = response.status;
        error.areaChoices = Array.isArray(payload.area_choices) ? payload.area_choices.filter(value => typeof value === 'string') : [];
        error.areaChoiceKey = typeof payload.area_choice_key === 'string' ? payload.area_choice_key : null;
        error.needsAreaChoice = payload.needs_area_choice === true || payload.search_intent?.needs_area_choice === true;
        throw error;
      }
      if (payload && typeof payload === 'object') stateResponseSequences.set(payload, sequence);
      return payload;
    } catch (error) {
      if (error.name === 'AbortError') error = new Error('연결 시간이 오래 걸리고 있어요. 처리 상태를 다시 확인해 주세요.');
      error.requestSequence = sequence;
      throw error;
    } finally { clearTimeout(timeout); }
  }

  function applyState(next) {
    if (!next || !Number.isSafeInteger(next.state_revision) || next.state_revision < 0) return false;
    const sequence = stateResponseSequences.get(next) ?? ++stateRequestSequence;
    stateResponseSequences.set(next, sequence);
    const currentRevision = Number.isSafeInteger(state.state_revision) ? state.state_revision : -1;
    if (next.state_revision < currentRevision) return false;
    const persisted = Object.fromEntries(Object.entries(next).filter(([key]) => !stateMetadataKeys.has(key)));
    state = {...state, ...persisted};
    // Models, job progress and live position can change without a disk commit.
    // They retain their own request order instead of being frozen at a revision.
    if (sequence >= stateMetadataSequence) {
      for (const key of stateMetadataKeys) if (Object.hasOwn(next, key)) state[key] = next[key];
      stateMetadataSequence = sequence;
    }
    return true;
  }

  async function jsonRequest(path, method, body) {
    const result = await request(path, {method, headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
    const next = result?.state && typeof result.state === 'object' ? result.state : result;
    if (next !== result) stateResponseSequences.set(next, stateResponseSequences.get(result));
    applyState(next);
    return result;
  }

  async function refreshState(queueAfterCurrent = true) {
    if (stateRefreshPromise) {
      if (queueAfterCurrent) stateRefreshQueued = true;
      return stateRefreshPromise;
    }
    stateFetching = true;
    stateRefreshPromise = (async () => {
      do {
        stateRefreshQueued = false;
        await readStateSnapshot();
      } while (stateRefreshQueued);
    })().finally(() => { stateFetching = false; stateRefreshPromise = null; });
    return stateRefreshPromise;
  }

  async function readStateSnapshot() {
    try {
      const next = await request('/api/state', {timeout: 6000});
      applyState(next);
      stateSnapshotVersion++;
      const firstLoad = !initialStateLoaded;
      if (firstLoad) {
        initialStateLoaded = true;
        // A restored conversation is the initial snapshot, not new arrivals.
        transcriptSignature = '';
        if (state.trip?.place && ['selected', 'navigating', 'guiding', 'en_route', 'arrived', 'awaiting_decision'].includes(state.trip.status)) {
          showPlaceDetail = true;
          switchTab('places');
        }
      }
      if (!regionDraftDirty && document.activeElement !== $('region-input')) {
        $('region-input').value = state.region || '';
      }
      renderLocation();
      const ready = Boolean(state.models?.ready);
      $('connection-badge').className = 'connection-badge ' + (ready ? 'ready' : '');
      $('connection-badge').lastElementChild.textContent = ready ? '음성 연결됨' : '음성 준비 중';
      if (state.job && ['queued', 'running', 'processing', 'pending'].includes(state.job.status) && !activeJob && !recording && !streamRunning && !streamFailure) {
        processing = true;
        const message = state.job.message || '';
        const purpose = /장소|추천/.test(message) ? 'recommend' : 'meeting';
        watchJob(state.job.id || state.job.job_id, purpose);
      }
      render();
      if (firstLoad) requestAnimationFrame(refreshNavigationState);
    } catch (error) {
      if (error.requestSequence < stateMetadataSequence) return;
      stateMetadataSequence = error.requestSequence || stateMetadataSequence;
      $('connection-badge').className = 'connection-badge error';
      $('connection-badge').lastElementChild.textContent = '연결 확인 필요';
      state.models = {...state.models, ready: false};
      renderControls();
      if (!state.segments?.length && !recording) {
        $('dock-title').textContent = '음성 연결을 확인하고 있어요';
        $('dock-subtitle').textContent = '연결되면 대화를 시작할 수 있어요.';
      }
    }
  }

  function switchTab(tab) {
    currentTab = tab;
    document.querySelector('.app-shell').dataset.view = tab;
    $('transcript-panel').hidden = tab !== 'transcript';
    $('places-panel').hidden = tab !== 'places';
    $('saved-panel').hidden = tab !== 'saved';
    ['transcript', 'places', 'saved'].forEach(name => {
      $('tab-' + name).classList.toggle('active', name === tab);
      $('tab-' + name).setAttribute('aria-selected', String(name === tab));
    });
  }

  function render() {
    const segments = asArray(state.segments).filter(segment => segment.final !== false && !segment.provisional);
    $('segment-count').textContent = segments.length;
    $('place-count').textContent = asArray(state.recommendations).length;
    $('demo-badge').hidden = !Boolean(state.demo || state.is_demo);
    renderLocation();
    renderCrew();
    renderTranscript();
    renderPlaces();
    renderTrip();
    renderSavedPlaces();
    renderControls();
  }

  function searchMode() { return state.search_mode || 'conversation'; }
  function currentLocationMode() { return searchMode() === 'current'; }
  function automaticSearchMode() { return searchMode() !== 'manual'; }

  function renderLocation() {
    const mode = searchMode(), position = state.location || {};
    $('selected-region-label').textContent = mode === 'conversation' ? '대화 기준 추천' : mode === 'current' ? '현재 위치 주변' : '직접 선택 · ' + (state.region || '지역 선택');
    $('region-picker-button').title = mode === 'conversation' ? '대화에 나온 지역과 취향으로 추천' : mode === 'current' ? '현재 위치 주변에서 대화 조건으로 추천' : '직접 선택한 지역: ' + (state.region || '');
    $('region-input').readOnly = automaticSearchMode();
    if (automaticSearchMode()) $('region-input').value = mode === 'conversation' ? '대화 내용으로 찾기' : '현재 위치 주변';
    $('location-status-text').textContent = position.available
      ? '현재 위치 주변에서 찾을 수 있어요.'
      : position.message || '현재 위치를 확인하고 있어요. 지역을 직접 정할 수도 있어요.';
    $('location-status-text').classList.toggle('unavailable', !position.available);
    $('current-location-button').textContent = recommendAfterRegion ? '대화 기준으로 다시 찾기' : mode === 'conversation' ? '대화 기준 추천 선택됨' : '대화 기준 추천 사용';
    $('current-location-button').disabled = regionSaving || Boolean(recording) || processing;
  }

  function renderCrew() {
    const channels = Array.isArray(state.seat_channels) ? state.seat_channels : Object.entries(state.seat_channels || {}).map(([seat, value]) => ({seat, ...(typeof value === 'object' ? value : {connected: value})}));
    const connected = id => channels.some(channel => channel.seat === id && channel.connected === true);
    const count = seats.filter(seat => connected(seat.id)).length;
    $('crew-count').textContent = state.demo ? '예시 속 탑승자 4명' : count ? count + '개 좌석 연결됨' : '좌석 확인 필요';
    const signature = JSON.stringify([channels, Boolean(state.demo)]);
    if (signature === crewSignature) return;
    crewSignature = signature;
    $('crew-pills').innerHTML = seats.map(seat => `<div class="crew-pill ${!state.demo && connected(seat.id) ? 'ready' : ''}" aria-label="${seat.label} · ${state.demo ? '샘플 좌석' : connected(seat.id) ? '연결됨' : '좌석 확인 필요'}"><span class="seat-dot" aria-hidden="true"></span>${seat.label}<small>${state.demo ? '예시' : connected(seat.id) ? '연결됨' : '미확인'}</small></div>`).join('');
  }

  function conversationRows() {
    const rows = asArray(state.segments).map((segment, index) => ({...segment, editIndex: index, interim: segment.final === false || segment.provisional === true}));
    const extra = asArray(state.interim_segments || state.interim || state.live?.interim);
    const known = new Set(rows.filter(row => row.id != null).map(row => String(row.id)));
    extra.forEach((segment, index) => {
      if (!segment || !segment.text || (segment.id != null && known.has(String(segment.id)))) return;
      rows.push({...segment, interim: true, editIndex: -1, interimIndex: index});
    });
    return rows.filter(row => row.text?.trim());
  }

  function nearLatest() {
    const panel = $('transcript-scroll');
    return panel.scrollHeight - panel.clientHeight - panel.scrollTop < 84;
  }

  function updateUnread() {
    $('new-messages-button').hidden = unreadMessages === 0;
    $('new-messages-label').textContent = unreadMessages > 0 ? '새 대화 ' + unreadMessages + '개' : '새 대화';
  }

  function scrollToLatest() {
    const panel = $('transcript-scroll');
    panel.scrollTop = panel.scrollHeight;
    unreadMessages = 0;
    updateUnread();
  }

  function renderTranscript() {
    const rows = conversationRows();
    $('transcript-empty').hidden = rows.length > 0 || Boolean(recording) || processing;
    const signature = JSON.stringify(rows);
    if (signature === transcriptSignature) return;
    const initialSnapshot = transcriptSignature === '';
    const firstRender = initialSnapshot || transcriptKeys.size === 0;
    const follow = currentTab === 'transcript' && (firstRender || nearLatest());
    transcriptSignature = signature;
    const oldScrollTop = $('transcript-scroll').scrollTop;
    const keys = rows.map((row, index) => String(row.id ?? (row.interim ? 'interim-' + (row.seat || 'unknown') + '-' + row.interimIndex : 'row-' + index)));
    const added = keys.filter(key => !transcriptKeys.has(key)).length;
    transcriptKeys = new Set(keys);
    $('transcript-list').innerHTML = rows.map((segment, index) => {
      const speaker = seatInfo(segment.seat);
      const uncertain = speaker.id === 'unknown' || segment.uncertain || segment.overlap;
      const warning = segment.overlap ? '겹친 발언 · 확인 필요' : segment.uncertain && speaker.id !== 'unknown' ? '좌석 확인 필요' : '';
      const provisional = segment.interim;
      const spokenTime = conversationTime(segment.spoken_at);
      const timeMarkup = spokenTime ? `<time class="chat-time" datetime="${escapeHtml(segment.spoken_at)}" title="기록 시각">${spokenTime}</time>` : '';
      const tag = provisional ? 'div' : 'button';
      const attributes = provisional ? '' : `type="button" data-edit-index="${segment.editIndex}" aria-label="${escapeHtml(speaker.label)} 발언 수정"`;
      return `<article class="chat-message ${speaker.id === 'driver' ? 'from-driver' : ''} ${uncertain ? 'uncertain' : ''} ${provisional ? 'provisional' : ''}" data-message-key="${escapeHtml(keys[index])}"><div class="chat-content"><div class="chat-author">${escapeHtml(speaker.label)}${timeMarkup}${provisional ? '<span class="interim-label">인식 중</span>' : ''}${warning ? `<span class="uncertain-badge">${escapeHtml(warning)}</span>` : ''}</div><${tag} class="speech-card" ${attributes}><p>${escapeHtml(segment.text)}${provisional ? '<span class="typing-cursor" aria-hidden="true"></span>' : ''}</p></${tag}></div></article>`;
    }).join('');
    if (!rows.length) {
      unreadMessages = 0;
      updateUnread();
    } else if (follow) {
      requestAnimationFrame(scrollToLatest);
    } else {
      $('transcript-scroll').scrollTop = oldScrollTop;
      unreadMessages += initialSnapshot ? 0 : added;
      updateUnread();
    }
  }

  function constraintRows() {
    if (Array.isArray(state.constraints)) return state.constraints;
    if (state.constraints && typeof state.constraints === 'object') {
      return Object.entries(state.constraints).map(([seat, values]) => ({seat, ...(typeof values === 'object' ? values : {likes: values})}));
    }
    return [];
  }

  function renderPlaces() {
    renderSpeakerControls();
    const recommendations = asArray(state.recommendations);
    const constraints = constraintRows();
    const groups = [...new Set(recommendations.map(placeGroup))];
    const selectedGroup = $('place-filters').dataset?.selected || '전체';
    const activeGroup = groups.includes(selectedGroup) ? selectedGroup : '전체';
    const hasTranscript = asArray(state.segments).some(segment => segment.final !== false && !segment.provisional);
    const signature = JSON.stringify([recommendations, constraints, state.coverage, state.lookup_source, state.region, state.notice, state.recommendation_summary, state.search_intent, state.request_profile, activeGroup, hasTranscript]);
    if (signature === placesSignature) return;
    placesSignature = signature;
    renderRecommendationSummary();
    const hasSearchResult = recommendations.length > 0 || /조건에 맞는 공개 장소를 찾지 못/.test(state.notice || '');
    $('recommend-heading').hidden = !hasSearchResult;
    $('search-details').hidden = !hasSearchResult || !(state.coverage || state.lookup_source);
    $('search-region').textContent = state.region || '';
    $('search-coverage').textContent = state.coverage || '검색 지역 주변';
    $('search-source').textContent = state.lookup_source || 'OpenStreetMap';
    $('search-context').textContent = state.search_intent?.message || '';
    $('search-context').hidden = !$('search-context').textContent;
    $('places-empty').hidden = recommendations.length > 0;
    $('places-empty-title').textContent = hasSearchResult ? '조금 다른 조건으로 찾아볼까요?' : hasTranscript ? '이제 함께 갈 곳을 골라볼까요?' : '어디로 갈지, 함께 이야기해요';
    const timedSearch = Boolean(state.request_profile?.travel_max_minutes);
    $('places-empty-description').textContent = hasSearchResult ? (timedSearch ? state.notice : '대화에서 원하는 장소나 검색 지역을 바꿔보세요.') : hasTranscript ? '나눈 대화를 바탕으로 각자의 취향에 맞는 곳을 찾아요.' : '먹고 싶은 것, 가고 싶은 곳을 말하면 대화에서 찾아드릴게요.';
    $('places-empty-action').textContent = hasSearchResult ? (timedSearch ? '다시 찾아보기' : '검색 지역 바꾸기') : hasTranscript ? '장소 추천받기' : '대화 시작';
    $('places-empty-action').dataset.mode = hasSearchResult ? (timedSearch ? 'recommend' : 'region') : hasTranscript ? 'recommend' : 'record';
    $('places-empty-sample').hidden = hasTranscript;
    $('preference-summary').hidden = constraints.length === 0 || !$('conversation-summary').hidden;
    $('preference-list').innerHTML = constraints.map(constraint => {
      if (typeof constraint === 'string') return `<div class="preference-row"><span class="pref-name">함께</span><div class="pref-chips"><span class="pref-chip">${escapeHtml(constraint)}</span></div></div>`;
      const speaker = seatInfo(constraint.seat || constraint.speaker);
      const likes = asArray(constraint.likes || constraint.preferences || (constraint.type === 'like' ? constraint.text : null));
      const dislikes = asArray(constraint.dislikes || constraint.exclusions || (constraint.type === 'dislike' ? constraint.text : null));
      const requirements = asArray(constraint.requirements || constraint.needs || (constraint.type === 'requirement' ? constraint.text : null));
      const getText = value => typeof value === 'string' ? value : value.text || value.value || value.label || '';
      const chips = likes.map(value => `<span class="pref-chip">선호 · ${escapeHtml(getText(value))}</span>`).concat(dislikes.map(value => `<span class="pref-chip negative">제외 · ${escapeHtml(getText(value))}</span>`), requirements.map(value => `<span class="pref-chip required">필수 · ${escapeHtml(getText(value))}</span>`));
      if (!chips.length && constraint.text) chips.push(`<span class="pref-chip">${escapeHtml(constraint.text)}</span>`);
      return `<div class="preference-row" style="--seat-color:${speaker.color}"><span class="pref-name">${constraint.seat || constraint.speaker ? speaker.label : '함께'}</span><div class="pref-chips">${chips.join('')}</div></div>`;
    }).join('');
    $('results-toolbar').hidden = recommendations.length === 0;
    $('results-count').textContent = recommendations.length + '곳';
    $('place-filters').innerHTML = ['전체', ...groups].map(group => {
      const count = group === '전체' ? recommendations.length : recommendations.filter(place => placeGroup(place) === group).length;
      return `<button type="button" class="place-filter ${group === activeGroup ? 'active' : ''}" data-place-filter="${escapeHtml(group)}" aria-pressed="${group === activeGroup}">${escapeHtml(group)} <span>${count}</span></button>`;
    }).join('');
    $('results-guidance').textContent = state.request_profile?.needs_more_preferences ? '아직 취향을 알기에는 대화가 부족해요. 먹고 싶은 메뉴나 가고 싶은 곳, 피하고 싶은 것을 더 이야기해 주세요.' : state.request_profile?.explanation || '메뉴·장소 정보와 대화가 맞는 후보를 모았어요. 확인되지 않은 조건은 별도로 표시해요.';
    $('places-list').innerHTML = recommendations.map((place, index) => {
      if (activeGroup !== '전체' && placeGroup(place) !== activeGroup) return '';
      const cautions = [...new Set(asArray(place.cautions || place.warnings).map(item => typeof item === 'string' ? item : item.text || '').filter(Boolean))];
      const distance = placeDistance(place);
      const matches = asArray(place.matched_topics).filter(item => typeof item === 'string');
      const travel = place.travel_constraint;
      const routeSource = safeLink(travel?.source_url);
      const routeAttribution = safeLink(travel?.attribution_url);
      const mapEdit = safeLink(travel?.fix_map_url || travel?.map_edit_url || place.map_edit_url);
      const reason = place.reason_summary || asArray(place.reasons || place.matches).map(textValue).filter(Boolean).join(' ');
      return `<article class="place-card selectable-place" data-place-id="${escapeHtml(place.id)}">
        <span class="place-rank">${String(index + 1).padStart(2, '0')}</span>
        <div class="place-body"><div class="place-main"><span class="place-category">${escapeHtml(placeGroup(place))} · ${escapeHtml(place.category || '장소')}</span>
          <h3><button type="button" class="place-name-button" aria-label="${escapeHtml(place.name || place.title)} 상세 보기">${escapeHtml(place.name || place.title || '추천 장소')}</button></h3>
          <p class="place-address">${escapeHtml(place.address || '상세 주소 확인 필요')}</p>
          <p class="place-distance">${escapeHtml(distance)}</p>
          ${personalRatingMarkup(place)}
        </div><div class="place-rationale">
          <p class="place-matches">${escapeHtml(matches.join(' · '))}</p>
          <p class="place-reason-summary">${escapeHtml(reason)}</p>
          <p class="place-travel ${travel?.verified && travel.within_limit === true ? 'verified' : ''}">${escapeHtml(travel?.message || '')}</p>
          ${travel?.verified ? `<p class="route-provenance">${escapeHtml(travel.origin_label || '출발 위치')} 기준 · 실시간 교통 미반영${routeSource ? ` · <a href="${escapeHtml(routeSource)}" target="_blank" rel="noopener noreferrer">${escapeHtml(travel.source || '경로 정보 출처')}</a>` : ''}${routeAttribution ? ` · <a href="${escapeHtml(routeAttribution)}" target="_blank" rel="noopener noreferrer">${escapeHtml(travel.attribution || '© OpenStreetMap contributors')}</a>` : ''}${mapEdit ? ` · <a href="${escapeHtml(mapEdit)}" target="_blank" rel="noopener noreferrer">지도 정보 수정</a>` : ''}</p>` : ''}
        </div></div><div class="place-card-footer"><span class="place-check-count">${cautions.length ? '방문 전 확인할 정보 ' + cautions.length + '개' : '대화에서 나온 조건 반영'}</span><span class="place-select-label" aria-hidden="true">상세 보기 <span>→</span></span></div></article>`;
    }).join('');
  }

  function placeGroup(place) {
    if (place.group) return place.group;
    const category = String(place.category || '');
    if (/카페|디저트|베이커리/.test(category)) return '카페·디저트';
    if (/식당|음식|한식|일식|양식|횟집|면|고기/.test(category)) return '음식점';
    if (/전시|박물관|미술관|문화/.test(category)) return '전시·문화';
    return '여행·산책';
  }

  function recommendationSpeech() {
    const places = asArray(state.recommendations).filter(place => place?.name).slice(0, 3);
    if (!places.length) return '';
    const introduction = state.demo ? '샘플 대화의 추천이에요. ' : '';
    const choices = places.map((place, index) => (index + 1) + '번째는 ' + String(place.name).slice(0,100) + '. ' + String(place.reason_summary || '').slice(0,240)).join(' ');
    const unknown = places.some(place => asArray(place.cautions || place.warnings).length > 0);
    return introduction + choices + (unknown ? ' 가격이나 주차처럼 확인되지 않은 정보는 방문 전에 확인해 주세요.' : ' 자세한 이유는 화면에서 볼 수 있어요.');
  }

  function renderSpeakerControls() {
    const button = $('recommend-listen');
    const active = ['preparing', 'speaking'].includes(nativeSpeaker.state);
    button.hidden = !window.CabinAndroid?.speakRecommendations;
    button.disabled = !active && (!nativeSpeaker.available || Boolean(recording) || processing || nativePendingAudio || !asArray(state.recommendations).length);
    button.textContent = active ? '읽기 멈추기' : '추천 듣기';
    button.setAttribute('aria-pressed', String(active));
  }

  function speakRecommendations() {
    if (recording || processing || nativePendingAudio || !nativeSpeaker.available) return false;
    const text = recommendationSpeech();
    if (!text) return false;
    try {
      if (window.CabinAndroid?.speakRecommendations?.(text) === true) {
        nativeSpeaker.state = 'preparing';
        renderSpeakerControls();
        return true;
      }
    } catch { /* The native callback reports playback failures. */ }
    notify('음성 안내를 시작하지 못했어요. 잠시 후 다시 눌러주세요.', true);
    return false;
  }

  function speakRequestedRecommendations() {
    const request = voiceRecommendationRequest;
    voiceRecommendationRequest = null;
    if (request && request.conversationId === state.conversation_id && Date.now() < request.deadline) speakRecommendations();
  }

  window.onNativeSpeaker = event => {
    if (!event || typeof event !== 'object') return;
    nativeSpeaker = event;
    if (event.error && event.state === 'error') notify(event.error, true);
    renderSpeakerControls();
  };

  function refreshSpeakerState() {
    try {
      const value = window.CabinAndroid?.getSpeakerState?.();
      if (value) window.onNativeSpeaker(typeof value === 'string' ? JSON.parse(value) : value);
    } catch { /* Keep playback unavailable on unsupported devices. */ }
  }

  function personalRatingMarkup(place) {
    const rating = place.my_rating;
    if (!rating || !Number.isInteger(rating.rating) || rating.rating < 1 || rating.rating > 5) return '';
    return `<p class="personal-rating">내 평가 <strong>★ ${rating.rating}</strong>${rating.note ? `<span>${escapeHtml(rating.note)}</span>` : ''}</p>`;
  }

  function placeDistance(place) {
    const metres = place.distance_m;
    if (typeof metres !== 'number' || !Number.isFinite(metres) || metres < 0) return '';
    const label = place.distance_basis === 'departure' ? '출발지에서' : place.distance_basis === 'current_location' ? '검색 당시 위치에서' : place.distance_basis === 'search_center' ? '검색 중심에서' : '';
    if (!label) return '';
    const distance = metres < 1000 ? Math.round(metres / 10) * 10 + 'm' : (metres / 1000).toFixed(1) + 'km';
    return label + ' 직선 약 ' + distance;
  }

  function textValue(value) {
    return typeof value === 'string' ? value : value?.text || value?.label || '';
  }

  function summaryHeading(value) {
    const title = String(value || '').trim();
    return title === '이 대화를 바탕으로 골랐어요' ? '' : title;
  }

  function renderRecommendationSummary() {
    const summary = state.recommendation_summary;
    $('conversation-summary').hidden = !summary || !(summary.text || summary.title || asArray(summary.bullets).length);
    $('summary-title').hidden = $('conversation-summary').hidden;
    $('summary-title').textContent = '우리가 찾는 곳';
    $('summary-text').textContent = summary?.text || '';
    $('summary-detail-title').textContent = summaryHeading(summary?.title);
    $('summary-detail-title').hidden = !$('summary-detail-title').textContent;
    $('summary-full-text').textContent = summary?.text || '';
    $('summary-bullets').innerHTML = asArray(summary?.bullets).map(item => `<li>${escapeHtml(textValue(item))}</li>`).join('');
    $('summary-evidence-details').hidden = !(summary?.text || summaryHeading(summary?.title) || asArray(summary?.bullets).length || asArray(summary?.evidence).length);
    $('summary-evidence').innerHTML = asArray(summary?.evidence).map(item => `<blockquote><span>${escapeHtml(item.seat ? seatInfo(item.seat).label : '대화에서')}</span><p>“${escapeHtml(item.text || '')}”</p></blockquote>`).join('');
    const limits = asArray(summary?.limitations).map(textValue).filter(Boolean);
    $('summary-limitations').hidden = limits.length === 0;
    $('summary-limitations').textContent = limits.join(' · ');
  }

  function readableDate(value) {
    if (!value) return '기록 없음';
    const date = new Date(typeof value === 'number' && value < 1e12 ? value * 1000 : value);
    if (Number.isNaN(date.getTime())) return String(value);
    return new Intl.DateTimeFormat('ko-KR', {year: 'numeric', month: 'long', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false, timeZone: 'Asia/Seoul'}).format(date);
  }

  function tripSummary(trip) {
    return trip?.summary || trip?.recommendation_summary || {};
  }

  function reasonList(place) {
    return asArray(place?.reasons || place?.matches).map(reason => {
      if (typeof reason === 'string') return `<li><div>${escapeHtml(reason)}</div></li>`;
      return `<li><div>${escapeHtml(reason.text || reason.reason || '')}${reason.quote ? `<details><summary>관련 발언</summary><blockquote>“${escapeHtml(reason.quote)}”</blockquote></details>` : ''}</div></li>`;
    }).join('');
  }

  function sameTripContext(context) {
    return Boolean(context && state.trip?.id === context.id && state.trip?.status === context.status);
  }

  function closeManualArrival() {
    manualArrivalContext = null;
    setDialogOpen('manual-arrival-dialog', false);
  }

  function renderTrip() {
    const trip = state.trip;
    const place = trip?.place;
    if (trip?.id && releasedNavigationTrip !== trip.id
        && ['arrived', 'awaiting_decision', 'saved', 'dismissed', 'cancelled', 'discarded', 'completed'].includes(trip.status)
        && typeof window.CabinAndroid?.finishNavigationTracking === 'function') {
      try {
        window.CabinAndroid.finishNavigationTracking(trip.id);
        releasedNavigationTrip = trip.id;
      } catch (_) { /* Retry on the next render if the bridge is not ready. */ }
    }
    if (manualArrivalContext && !sameTripContext(manualArrivalContext)) {
      closeManualArrival();
      if (!tripBusy) notify('이동 정보가 바뀌었어요. 현재 장소를 확인한 뒤 도착을 확인해 주세요.');
    }
    const decisionChanged = arrivalDecisionContext && !sameTripContext(arrivalDecisionContext);
    if (decisionChanged) {
      arrivalDecisionContext = null;
      setDialogOpen('arrival-dialog', false);
      if (!tripBusy) notify('이동 정보가 바뀌어 이전 저장 질문을 닫았어요.');
    }
    if (trip?.status === 'cancelled') { guidanceStarting = false; guidanceCancelling = false; nativeGuidingTrip = null; clearTimeout(guidanceTimer); clearTimeout(guidanceCancelTimer); }
    $('places-overview').hidden = Boolean(showPlaceDetail && place);
    $('place-detail').hidden = !showPlaceDetail || !place;
    document.querySelector('.app-shell').dataset.detail = String(Boolean(showPlaceDetail && place));
    if (!place) { setDialogOpen('arrival-dialog', false); return; }
    const summary = tripSummary(trip);
    const matches = nativeNavigation.trip_id === trip.id;
    const nativeDetails = matches && nativeNavigation.native_poi_available === true;
    const nativePlace = nativeDetails ? nativeNavigation.native_place : null;
    $('detail-category').textContent = (place.category || '선택한 목적지') + (trip.demo ? ' · 샘플 대화 기반' : '');
    $('detail-name').textContent = nativePlace?.name || place.name || '선택한 장소';
    $('detail-address').textContent = nativePlace?.address || place.address || '주소 정보가 제공되지 않았어요.';
    $('detail-native-match').hidden = !nativePlace?.name;
    $('detail-native-match').textContent = nativePlace?.name ? '이름·위치가 일치하는 순정 내비 장소' : '';
    const latitude = Number(place.latitude), longitude = Number(place.longitude);
    const travel = place.travel_constraint;
    const routeSource = safeLink(travel?.source_url), routeCopyright = safeLink(travel?.attribution_url), routeEdit = safeLink(travel?.fix_map_url);
    $('detail-coordinates').innerHTML = travel?.max_minutes ? `${escapeHtml(travel.message || '')}${travel.verified ? `<br>${routeSource ? `<a href="${escapeHtml(routeSource)}" target="_blank" rel="noopener noreferrer">${escapeHtml(travel.source)}</a>` : ''}${routeCopyright ? ` · <a href="${escapeHtml(routeCopyright)}" target="_blank" rel="noopener noreferrer">${escapeHtml(travel.attribution)}</a>` : ''}${routeEdit ? ` · <a href="${escapeHtml(routeEdit)}" target="_blank" rel="noopener noreferrer">지도 정보 수정</a>` : ''}` : ''}` : '';
    $('detail-summary-title').textContent = summaryHeading(summary.title);
    $('detail-summary-title').hidden = !$('detail-summary-title').textContent;
    $('detail-summary-text').textContent = place.reason_summary || summary.text || '';
    const reasonsMarkup = reasonList(place);
    if (reasonsMarkup !== detailReasonsSignature) {
      detailReasonsSignature = reasonsMarkup;
      $('detail-reasons').innerHTML = reasonsMarkup;
    }
    const cautions = asArray(place.cautions || place.warnings).map(textValue).filter(Boolean);
    $('detail-cautions').hidden = cautions.length === 0;
    const cautionMarkup = cautions.map(item => `<li>${escapeHtml(item)}</li>`).join('');
    if ($('detail-caution-list').innerHTML !== cautionMarkup) $('detail-caution-list').innerHTML = cautionMarkup;
    const sourceLink = safeLink(place.url || place.search_url || place.link);
    const evidence = asArray(place.quality_factors).map(item => ({label: item.label, detail: item.detail})).concat(asArray(place.score_breakdown)).filter(item => item.detail);
    const sourceMarkup = `${evidence.length ? `<dl class="match-evidence">${evidence.map(item => `<div><dt>${escapeHtml(item.label || '확인 근거')}</dt><dd>${escapeHtml(item.detail)}</dd></div>`).join('')}</dl>` : ''}${place.source_note ? `<p>${escapeHtml(place.source_note)}</p>` : ''}${sourceLink ? `<a href="${escapeHtml(sourceLink)}" target="_blank" rel="noopener noreferrer">장소 정보 출처 보기</a>` : ''}`;
    $('detail-source').hidden = !sourceMarkup;
    if ($('detail-source-content').innerHTML !== sourceMarkup) $('detail-source-content').innerHTML = sourceMarkup;
    const status = matches ? nativeNavigation.status : '';
    const serverNavigating = ['navigating', 'guiding', 'en_route'].includes(trip.status);
    const interrupted = matches && ['error', 'cancelled'].includes(status);
    const guiding = !interrupted && (serverNavigating || (matches && status === 'guiding'));
    const arrived = ['arrived', 'awaiting_decision', 'saved', 'dismissed', 'cancelled', 'discarded', 'completed'].includes(trip.status);
    const bridge = Boolean(window.CabinAndroid?.startNavigation || window.CabinAndroid?.startGuidance);
    const available = bridge && nativeNavigation.available !== false;
    let title = '목적지를 확인해 주세요';
    let message = '길찾기 안내를 누르면 순정 내비에 이 목적지를 전달해요.';
    if (guidanceCancelling) { title = '순정 내비의 취소 확인을 기다리고 있어요'; message = '차량에서 실제 안내 종료가 확인되면 다른 후보를 고를 수 있어요.'; }
    else if (matches && status === 'cancelled') { title = '안내 취소를 반영하고 있어요'; message = '목적지 기록을 정리하고 있어요.'; }
    else if (arrived) { title = '도착한 장소예요'; message = trip.status === 'saved' ? '대화와 추천 이유를 저장한 장소에서 볼 수 있어요.' : '이 장소를 대화와 함께 남길 수 있어요.'; }
    else if (matches && status === 'error') { title = '길찾기 연결을 확인해 주세요'; message = nativeNavigation.message || '순정 내비의 안내 상태를 확인하지 못했어요.'; }
    else if (guiding) { title = '순정 내비에서 안내 중이에요'; message = '도착 신호를 받으면 장소 저장 여부를 물어볼게요.'; }
    else if (guidanceStarting || ['connecting', 'routing'].includes(status)) { title = status === 'routing' ? '순정 내비에서 경로를 찾고 있어요' : '순정 내비에 연결하고 있어요'; message = nativeNavigation.message || '차량에서 목적지와 경로를 확인해 주세요.'; }
    else if (!available) { title = '순정 내비 연결이 필요해요'; message = nativeNavigation.message || (bridge ? '현재 차량의 순정 내비 연결 상태를 확인하고 있어요.' : '차량 앱에서 순정 내비 연결을 사용할 수 있어요.'); }
    $('navigation-title').textContent = title;
    $('navigation-message').textContent = message;
    $('navigation-status').classList.toggle('is-guiding', guiding);
    $('navigation-status').classList.toggle('is-error', matches && status === 'error');
    $('navigation-status').hidden = available && !guiding && !arrived && !guidanceStarting && !guidanceCancelling && !['error', 'cancelled', 'connecting', 'routing'].includes(status);
    $('detail-map-message').textContent = available ? '순정 내비에서 같은 장소를 선택하면 사진 등 등록된 상세정보를 볼 수 있어요. 음식점은 제공되는 메뉴와 가격도 확인할 수 있어요.' : '차량 앱에서 순정 내비에 연결하면 장소 상세정보를 확인할 수 있어요.';
    $('native-preview-button').textContent = nativeDetails ? '장소 상세정보 보기' : '내비에서 장소 확인';
    $('guidance-button').disabled = !available || serverNavigating || guiding || arrived || guidanceStarting || guidanceCancelling || tripBusy || Boolean(recording) || processing;
    $('guidance-button').textContent = guiding ? '길찾기 안내 중' : guidanceStarting ? '내비 연결 중' : '길찾기 안내';
    $('trip-cancel-button').hidden = arrived || (!serverNavigating && !guidanceStarting && !guiding);
    $('trip-cancel-button').textContent = guidanceCancelling ? '취소 확인 중' : serverNavigating || guidanceStarting ? '이동 취소' : '선택 취소';
    $('trip-cancel-button').disabled = tripBusy || Boolean(recording) || processing || guidanceCancelling || ((serverNavigating || guidanceStarting) && !window.CabinAndroid?.stopGuidance);
    $('native-preview-button').disabled = !window.CabinAndroid?.openPlacePreview || tripBusy || Boolean(recording) || processing || guidanceStarting || guidanceCancelling;
    $('manual-arrival-button').hidden = arrived || !(serverNavigating || guiding);
    $('manual-arrival-button').disabled = tripBusy || Boolean(recording) || processing || guidanceStarting || guidanceCancelling;
    const decisionNeeded = ['arrived', 'awaiting_decision'].includes(trip.status) && trip.save_decision == null && trip.decision == null;
    // Never replace an already displayed question with a different trip under a pending click.
    // A later state refresh can present the new trip's own question.
    setDialogOpen('arrival-dialog', decisionNeeded && !decisionChanged);
    if (decisionNeeded && !decisionChanged) {
      if (!arrivalDecisionContext) arrivalDecisionContext = {id: trip.id, status: trip.status};
      $('arrival-place').textContent = place.name || '선택한 장소';
      $('arrival-summary').textContent = summary.text || place.reason_summary || '이 장소를 고를 때 나눴던 대화를 함께 남겨요.';
      $('arrival-save').disabled = tripBusy;
      $('arrival-skip').disabled = tripBusy;
    } else if (!decisionNeeded) {
      arrivalDecisionContext = null;
    }
  }

  function renderSavedPlaces() {
    const saved = asArray(state.saved_places);
    $('saved-count').textContent = saved.length;
    $('saved-total').textContent = saved.length ? saved.length + '곳' : '';
    $('saved-empty').hidden = saved.length > 0;
    const signature = JSON.stringify(saved);
    if (signature === savedSignature) return;
    savedSignature = signature;
    $('saved-list').innerHTML = saved.map(item => {
      const place = item.place || item;
      const summary = tripSummary(item);
      const title = summaryHeading(summary.title);
      const reason = item.reason_summary || place.reason_summary || '';
      return `<article class="saved-place-card"><div class="saved-place-top"><div><span class="eyebrow">${escapeHtml(place.category || '방문한 장소')}</span><h3>${escapeHtml(place.name || '저장한 장소')}</h3></div>${item.demo ? '<span class="demo-badge">샘플</span>' : ''}</div><p class="place-address">${escapeHtml(place.address || '')}</p><div class="saved-conversation"><span class="section-label">대화 요약</span>${title ? `<h4>${escapeHtml(title)}</h4>` : ''}<p>${escapeHtml(summary.text || '저장된 대화 요약이 없어요.')}</p>${asArray(summary.bullets).length ? `<ul>${asArray(summary.bullets).map(bullet => `<li>${escapeHtml(textValue(bullet))}</li>`).join('')}</ul>` : ''}</div>${reason ? `<div class="saved-reason"><span class="section-label">추천 이유</span><p>${escapeHtml(reason)}</p></div>` : ''}<dl class="saved-dates"><div><dt>도착</dt><dd>${escapeHtml(readableDate(item.arrived_at || item.arrival_at))}${item.arrival_source === 'manual_confirmation' ? ' · 직접 확인' : ''}</dd></div><div><dt>저장</dt><dd>${escapeHtml(readableDate(item.saved_at || item.created_at))}</dd></div></dl><div class="saved-rating">${personalRatingMarkup(place)}${place.id ? `<button type="button" class="text-button rating-edit" data-rate-place="${escapeHtml(place.id)}" data-rate-visit="${escapeHtml(item.id || item.trip_id)}">${place.my_rating ? '내 평가 수정' : '평가 남기기'}</button>` : ''}</div></article>`;
    }).join('');
  }

  function openRating(placeId, visitId) {
    if (recording || processing) return;
    const item = asArray(state.saved_places).find(saved => (saved.place || saved).id === placeId && (saved.id || saved.trip_id) === visitId);
    if (!item) return;
    const place = item.place || item;
    ratingPlaceId = placeId;
    ratingVisitId = visitId;
    $('rating-place-name').textContent = place.name || '저장한 장소';
    $('rating-note').value = place.my_rating?.note || '';
    document.querySelectorAll('input[name="visit-rating"]').forEach(input => { input.checked = Number(input.value) === place.my_rating?.rating; });
    $('rating-error').hidden = true;
    setDialogOpen('rating-dialog', true);
  }

  async function saveRating(event) {
    event.preventDefault();
    if (!ratingPlaceId || $('rating-save').disabled) return;
    const selected = document.querySelector('input[name="visit-rating"]:checked');
    const rating = Number(selected?.value);
    if (!Number.isInteger(rating) || rating < 1 || rating > 5) return;
    $('rating-save').disabled = true;
    $('rating-cancel').disabled = true;
    $('rating-error').hidden = true;
    try {
      const result = await jsonRequest('/api/ratings', 'POST', {place_id: ratingPlaceId, visit_id: ratingVisitId, rating, note: $('rating-note').value.trim()});
      applyState(result);
      await refreshState();
      setDialogOpen('rating-dialog', false);
      notify('내 평가를 저장했어요. 다음에 장소를 고를 때 참고할 수 있어요.');
    } catch (error) { $('rating-error').textContent = error.message; $('rating-error').hidden = false; }
    finally { $('rating-save').disabled = false; $('rating-cancel').disabled = false; }
  }

  async function applyTripResult(result) {
    applyState(result);
    await refreshState();
    renderTrip();
    renderSavedPlaces();
  }

  async function selectPlace(id) {
    if (tripBusy || recording || processing || guidanceStarting || guidanceCancelling) return;
    tripBusy = true;
    try {
      const result = await jsonRequest('/api/trip/select', 'POST', {place_id: id});
      await applyTripResult(result);
      showPlaceDetail = true;
      switchTab('places');
      $('places-panel').scrollTop = 0;
      refreshNavigationState();
    } catch (error) { notify(error.message, true); }
    finally { tripBusy = false; renderTrip(); }
  }

  function refreshNavigationState() {
    try {
      if (window.CabinAndroid?.getNavigationState) {
        const value = window.CabinAndroid.getNavigationState();
        window.onNativeNavigation(typeof value === 'string' ? JSON.parse(value) : value);
      } else renderTrip();
    } catch { renderTrip(); }
  }

  function startGuidance() {
    const trip = state.trip;
    if (!trip?.place || !(window.CabinAndroid?.startNavigation || window.CabinAndroid?.startGuidance) || tripBusy || guidanceStarting || guidanceCancelling || recording || processing) return;
    const place = trip.place;
    guidanceStarting = true;
    guidanceRouteTimerStarted = false;
    renderTrip();
    try {
      // Trip/state region describes the search origin and time limit, not the destination.
      const payload = {trip_id: trip.id, place: {...place, region: place.region || ''}};
      if (window.CabinAndroid.startNavigation) window.CabinAndroid.startNavigation(JSON.stringify(payload));
      else window.CabinAndroid.startGuidance(JSON.stringify({trip_id: trip.id, ...place}));
      clearTimeout(guidanceTimer);
      guidanceTimer = setTimeout(() => { if (guidanceStarting) { guidanceStarting = false; nativeNavigation = {...nativeNavigation, trip_id: trip.id, status: 'error', message: '순정 내비의 응답을 확인하지 못했어요. 차량 화면에서 연결 상태를 확인해 주세요.'}; renderTrip(); } }, 30000);
    } catch (error) { guidanceStarting = false; notify(error.message, true); renderTrip(); }
  }

  window.onNativeNavigation = event => {
    if (!event || typeof event !== 'object') return;
    event = {...event, trip_id: event.trip_id || event.tripId, message: event.message || event.error || ''};
    const activeTrip = state.trip;
    const terminal = ['saved', 'dismissed', 'cancelled', 'discarded', 'completed'].includes(activeTrip?.status);
    if (typeof event.connected === 'boolean' && typeof event.available !== 'boolean') event.available = event.connected;
    if (event.trip_id && (event.trip_id !== activeTrip?.id || terminal)) {
      // The SDK connection belongs to the app, even when its snapshot still
      // describes an earlier destination. Keep that destination's state isolated.
      if (typeof event.connected === 'boolean') nativeNavigation.connected = event.connected;
      if (typeof event.available === 'boolean') nativeNavigation.available = event.available;
      renderTrip();
      return;
    }
    if (event.action === 'start' && event.success === true) event.status = 'guiding';
    if (event.action === 'arrived' && event.success === true) event.status = 'arrived';
    if (event.action === 'cancelled' && event.success === true) event.status = 'cancelled';
    if (['ready', 'guiding', 'routing', 'preview_requested', 'arrived'].includes(event.status) && event.available == null) event.available = true;
    nativeNavigation = {...nativeNavigation, ...event};
    if (guidanceStarting && !guidanceRouteTimerStarted && event.trip_id === activeTrip?.id
        && event.status === 'routing' && event.route_dispatched === true) {
      // Provider startup has its own deadline. Start the route-response timeout
      // once the native bridge actually sends the destination, not on each poll.
      guidanceRouteTimerStarted = true;
      clearTimeout(guidanceTimer);
      const requestedTripId = activeTrip.id;
      guidanceTimer = setTimeout(() => {
        if (!guidanceStarting || state.trip?.id !== requestedTripId) return;
        guidanceStarting = false;
        nativeNavigation = {...nativeNavigation, trip_id: requestedTripId, status: 'error', message: '순정 내비의 안내 시작을 확인하지 못했어요. 차량 화면에서 경로 상태를 확인해 주세요.'};
        renderTrip();
      }, 30000);
    }
    if (['guiding', 'arrived', 'error', 'cancelled'].includes(event.status)) { guidanceStarting = false; clearTimeout(guidanceTimer); }
    if (event.status === 'cancelled') { guidanceCancelling = true; clearTimeout(guidanceCancelTimer); }
    if (event.status === 'error') { guidanceCancelling = false; clearTimeout(guidanceCancelTimer); }
    renderTrip();
    navigationEvents = navigationEvents.then(async () => {
      const trip = state.trip;
      if (!trip || event.trip_id !== trip.id || ['saved', 'dismissed', 'cancelled', 'discarded', 'completed'].includes(trip.status)) return;
      if (event.backendSynced) {
        if (event.status === 'guiding') nativeGuidingTrip = trip.id;
        await refreshState();
        return;
      }
      if (event.status === 'cancelled') {
        nativeGuidingTrip = null;
        await applyTripResult(await jsonRequest('/api/trip/cancel', 'POST', {trip_id: trip.id, source: 'pleos_cancelled'}));
      } else if (event.status === 'guiding') {
        nativeGuidingTrip = trip.id;
        if (!['navigating', 'guiding', 'en_route', 'arrived', 'saved', 'dismissed', 'cancelled', 'discarded', 'completed'].includes(trip.status)) await applyTripResult(await jsonRequest('/api/trip/start', 'POST', {trip_id: trip.id, provider: 'pleos'}));
      } else if (event.status === 'arrived' && (nativeGuidingTrip === trip.id || ['navigating', 'guiding', 'en_route'].includes(trip.status))) {
        if (!['arrived', 'saved', 'dismissed', 'cancelled', 'discarded', 'completed'].includes(trip.status)) await applyTripResult(await jsonRequest('/api/trip/arrive', 'POST', {trip_id: trip.id, source: 'pleos_arrival'}));
      }
    }).catch(error => {
      if (event.status === 'cancelled') guidanceCancelling = false;
      notify(error.message, true);
      renderTrip();
    });
  };

  async function cancelTrip() {
    const trip = state.trip;
    if (!trip?.place || tripBusy || recording || processing || guidanceCancelling) return;
    const requiresNativeStop = guidanceStarting || ['navigating', 'guiding', 'en_route'].includes(trip.status) || (nativeNavigation.trip_id === trip.id && ['routing', 'guiding'].includes(nativeNavigation.status));
    if (requiresNativeStop) {
      if (!window.CabinAndroid?.stopGuidance) { notify('이동 취소는 순정 내비에서 안내를 종료한 뒤 확인할 수 있어요.', true); return; }
      guidanceCancelling = true;
      renderTrip();
      try {
        window.CabinAndroid.stopGuidance();
        clearTimeout(guidanceCancelTimer);
        guidanceCancelTimer = setTimeout(() => {
          if (!guidanceCancelling) return;
          guidanceCancelling = false;
          nativeNavigation = {...nativeNavigation, trip_id: trip.id, status: 'error', message: '순정 내비의 취소 응답을 확인하지 못했어요. 차량에서 안내 상태를 확인해 주세요.'};
          renderTrip();
        }, 30000);
      } catch (error) { guidanceCancelling = false; notify(error.message, true); renderTrip(); }
      return;
    }
    tripBusy = true;
    renderTrip();
    try {
      await applyTripResult(await jsonRequest('/api/trip/cancel', 'POST', {trip_id: trip.id, source: 'user_cancelled'}));
      showPlaceDetail = false;
      notify('장소 선택을 취소했어요. 다른 후보를 골라보세요.');
    } catch (error) { notify(error.message, true); }
    finally { tripBusy = false; renderTrip(); }
  }

  function previewNativePlace() {
    const trip = state.trip;
    if (!trip?.place || !window.CabinAndroid?.openPlacePreview || tripBusy || recording || processing) return;
    try { window.CabinAndroid.openPlacePreview(JSON.stringify({trip_id: trip.id, place: {...trip.place, region: trip.place.region || ''}})); }
    catch (error) { notify(error.message, true); }
  }

  function openManualArrival() {
    const trip = state.trip;
    if (!trip?.id || !trip.place || !['selected', 'navigating'].includes(trip.status)
        || tripBusy || recording || processing || guidanceStarting || guidanceCancelling) return;
    manualArrivalContext = {id: trip.id, status: trip.status};
    $('manual-arrival-place').textContent = trip.place.name || '';
    $('manual-arrival-error').hidden = true;
    setDialogOpen('manual-arrival-dialog', true, 'manual-arrival-cancel');
  }

  async function confirmManualArrival() {
    if (tripBusy || $('manual-arrival-dialog').hidden) return;
    const context = manualArrivalContext;
    if (!sameTripContext(context)) {
      closeManualArrival();
      notify('이동 정보가 바뀌었어요. 현재 장소를 확인한 뒤 도착을 확인해 주세요.');
      return;
    }
    tripBusy = true;
    $('manual-arrival-confirm').disabled = true;
    $('manual-arrival-error').hidden = true;
    try {
      await applyTripResult(await jsonRequest('/api/trip/arrive', 'POST', {trip_id: context.id, source: 'manual_confirmation'}));
      closeManualArrival();
    } catch (error) { $('manual-arrival-error').textContent = error.message; $('manual-arrival-error').hidden = false; }
    finally { tripBusy = false; $('manual-arrival-confirm').disabled = false; renderTrip(); }
  }

  async function decideSave(save) {
    if (tripBusy || $('arrival-dialog').hidden) return;
    const context = arrivalDecisionContext;
    if (!sameTripContext(context)) {
      arrivalDecisionContext = null;
      setDialogOpen('arrival-dialog', false);
      notify('저장할 이동 정보가 바뀌었어요. 현재 장소를 다시 확인해 주세요.');
      return;
    }
    tripBusy = true;
    $('arrival-error').hidden = true;
    renderTrip();
    try {
      await applyTripResult(await jsonRequest('/api/trip/decision', 'POST', {trip_id: context.id, save}));
      arrivalDecisionContext = null;
      setDialogOpen('arrival-dialog', false);
      if (save) { switchTab('saved'); notify('장소와 그때의 대화를 함께 저장했어요.'); }
      else notify('이번 장소는 저장하지 않았어요.');
    } catch (error) { $('arrival-error').textContent = error.message; $('arrival-error').hidden = false; }
    finally { tripBusy = false; renderTrip(); }
  }

  function safeLink(value) {
    if (!value) return null;
    try {
      const url = new URL(value, location.href);
      return ['https:', 'http:'].includes(url.protocol) ? url.href : null;
    } catch { return null; }
  }

  function renderControls() {
    if (streamFailure) processing = true;
    const hasTranscript = asArray(state.segments).some(segment => segment.final !== false && !segment.provisional);
    const ready = Boolean(state.models?.ready);
    const meeting = Boolean(recording);
    const microphonePaused = recording?.microphonePaused === true;
    const findingPlaces = processingPurpose === 'recommend';
    const live = Boolean((nativeStreaming && meeting) || state.live?.active || state.live?.connected || state.stream?.active);
    const jobTitle = findingPlaces ? '장소 찾는 중' : '대화 정리 중';
    document.querySelector('.recording-dock').classList.toggle('has-transcript', hasTranscript && !meeting);
    $('record-button').disabled = meeting ? Boolean(recording.pending || recording.stopping) : processing || regionSaving || !ready;
    $('record-button').classList.toggle('recording', meeting);
    $('record-icon').textContent = meeting ? '■' : '●';
    $('record-label').textContent = meeting ? (recording.pending ? '마이크 연결 중' : recording.stopping ? '대화 마치는 중' : '대화 마치기') : streamFailure ? '발언 전송 대기' : processing ? jobTitle : ready ? '대화 시작' : '준비 중';
    $('recommend-button').disabled = !hasTranscript || meeting || processing || regionSaving;
    $('recommend-refresh').disabled = !hasTranscript || meeting || processing || regionSaving;
    $('region-picker-button').disabled = meeting || processing || regionSaving;
    $('region-input').disabled = meeting || processing || regionSaving;
    $('demo-button').disabled = meeting || processing || regionSaving || guidanceStarting || guidanceCancelling;
    $('demo-button').textContent = state.demo ? '다른 샘플' : '샘플 대화';
    $('demo-exit-button').hidden = !state.demo_session;
    $('demo-exit-button').disabled = meeting || processing || regionSaving || guidanceStarting || guidanceCancelling;
    $('places-empty-action').disabled = meeting || processing || regionSaving || (!hasTranscript && !ready);
    $('places-empty-sample').disabled = $('demo-button').disabled;
    $('seat-source-note').textContent = state.demo ? '시연용으로 작성한 대화와 좌석이에요. 실제 음성 인식 결과가 아니에요.' : '좌석 정보가 없는 발언은 ‘좌석 확인 필요’로 표시돼요.';
    $('reset-button').disabled = !hasTranscript || meeting || processing || guidanceStarting || guidanceCancelling;
    $('export-button').disabled = !hasTranscript || processing;
    $('listening-card').hidden = !meeting && !live;
    $('listening-card').classList.toggle('microphone-paused', microphonePaused);
    $('listening-title').textContent = microphonePaused ? '마이크를 기다리고 있어요' : '듣고 있어요';
    $('listening-description').textContent = microphonePaused ? '다른 음성 기능이 마이크를 사용 중이에요. 그동안의 말은 기록되지 않아요.' : recording?.speechPartial || (live ? '인식된 내용이 대화창에 이어져요.' : '대화를 마치면 녹음한 내용을 정리해요.');
    $('job-card').hidden = !processing || meeting || Boolean(streamFailure);
    $('places-job').hidden = !processing || !findingPlaces;
    $('places-empty').hidden = asArray(state.recommendations).length > 0 || (processing && findingPlaces);
    $('dock-dot').classList.toggle('recording', (meeting || live) && !microphonePaused);
    if (microphonePaused) {
      $('dock-title').textContent = '마이크를 사용할 수 있을 때 이어서 들어요';
      $('dock-subtitle').textContent = '대기를 마치려면 ‘대화 마치기’를 눌러주세요.';
    } else if (meeting || live) {
      $('dock-title').textContent = '대화를 듣고 있어요';
      $('dock-subtitle').textContent = live ? '인식된 발언이 대화창에 이어져요.' : '한 분씩 자연스럽게 말씀해 주세요.';
    } else if (streamFailure) {
      $('dock-title').textContent = '아직 전달하지 못한 발언이 있어요';
      $('dock-subtitle').textContent = '다시 전송하거나 ‘다시 보내지 않기’를 선택한 뒤 계속할 수 있어요.';
    } else if (processing) {
      $('dock-title').textContent = findingPlaces ? '대화에 맞는 장소를 찾고 있어요' : '음성에서 대화를 정리하고 있어요';
      $('dock-subtitle').textContent = processingMessage || (findingPlaces ? '선호와 제외 조건을 장소 정보와 비교하고 있어요.' : '긴 녹음은 처리에 시간이 걸릴 수 있어요.');
    } else if (state.demo_session) {
      $('dock-title').textContent = state.demo_scenario?.title || '샘플 대화를 보고 있어요';
      $('dock-subtitle').textContent = '대화 시작을 누르면 원래 대화에 이어서 녹음해요.';
    } else if (!ready) {
      $('dock-title').textContent = '음성을 준비하고 있어요';
      $('dock-subtitle').textContent = '준비가 끝나면 대화를 시작할 수 있어요.';
    } else {
      $('dock-title').textContent = hasTranscript ? '대화 내용으로 장소를 찾아보세요' : '대화를 시작할 수 있어요';
      $('dock-subtitle').textContent = '녹음 전에 탑승자에게 알려주세요.';
    }
    $('job-title').textContent = jobTitle;
    $('job-message').textContent = processingMessage || '음성을 처리하고 있어요.';
    $('places-job-message').textContent = processingMessage || '대화에서 나온 조건으로 장소를 찾고 있어요.';
    $('transcript-empty').hidden = conversationRows().length > 0 || meeting || live || processing;
    $('stream-recovery').hidden = !streamFailure;
    $('stream-recovery-message').textContent = streamFailure ? streamFailure.message : '';
    $('stream-retry-button').disabled = streamRunning || streamFailure?.retrying === true;
    $('stream-discard-button').disabled = streamRunning || streamFailure?.retrying === true;
    $('stream-discard-button').textContent = streamFailure?.nativeManaged ? '보관한 발언 다시 보내지 않기' : '다시 보내지 않기';
    renderTrip();
  }

  async function startRecording(purpose) {
    if (recording || processing || regionSaving || streamFailure) return;
    if (state.demo_session) {
      processing = true;
      processingPurpose = 'meeting';
      processingMessage = '원래 대화로 돌아가 마이크를 준비하고 있어요.';
      renderControls();
      try {
        await jsonRequest('/api/demo/exit', 'POST', {});
        await refreshState();
        if (state.demo_session) throw new Error('원래 대화를 확인하지 못했어요. 다시 시도해 주세요.');
      } catch (error) { notify(error.message, true); return; }
      finally { processing = false; renderControls(); }
    }
    const conversationId = state.conversation_id;
    if (typeof conversationId !== 'string' || !conversationId) {
      notify('대화 상태를 확인한 뒤 녹음을 시작할 수 있어요. 잠시 후 다시 시도해 주세요.', true);
      return;
    }
    $('notice').hidden = true;
    if (purpose === 'meeting') switchTab('transcript');
    recording = {purpose, conversationId, startedAt: Date.now(), pending: true};
    nativePendingAudio = false;
    currentStreamSession = null;
    renderControls();
    renderCrew();
    try {
      if (window.CabinAndroid?.startRecording || window.CabinAndroid?.startRecordingForConversation) {
        if (!window.CabinAndroid.startRecordingForConversation) throw new Error('녹음을 시작하려면 차량 앱 업데이트가 필요해요.');
        window.CabinAndroid.startRecordingForConversation(purpose, conversationId);
        nativeStartTimer = setTimeout(() => {
          if (recording?.pending) {
            recording = null;
            notify('마이크를 시작하지 못했어요. 앱의 마이크 권한을 확인해 주세요.', true);
            render();
          }
        }, 12000);
      } else {
        if (!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) throw new Error('이 화면에서는 마이크를 열 수 없어요. Pleos 앱에서 대화를 시작해 주세요.');
        microphoneStream = await navigator.mediaDevices.getUserMedia({audio: {channelCount: 1, echoCancellation: true, noiseSuppression: true}});
        const type = ['audio/webm;codecs=opus', 'audio/webm', 'audio/mp4'].find(item => MediaRecorder.isTypeSupported(item));
        recorder = new MediaRecorder(microphoneStream, type ? {mimeType: type} : {});
        let recordingStartedAt = null;
        audioChunks = [];
        recorder.ondataavailable = event => { if (event.data.size) audioChunks.push(event.data); };
        recorder.onstop = async () => {
          const mime = recorder?.mimeType || type || 'audio/webm';
          const blob = new Blob(audioChunks, {type: mime});
          microphoneStream?.getTracks().forEach(track => track.stop());
          microphoneStream = null;
          recorder = null;
          audioChunks = [];
          await uploadAudio(blob, purpose, 'recording', conversationId, recordingStartedAt);
        };
        recorder.onerror = () => {
          notify('녹음 중 오류가 발생했어요. 마이크 연결을 확인해 주세요.', true);
          stopRecording();
        };
        recorder.start(500);
        recording.pending = false;
        recording.startedAt = Date.now();
        recordingStartedAt = new Date(recording.startedAt).toISOString();
        renderControls();
      }
    } catch (error) {
      microphoneStream?.getTracks().forEach(track => track.stop());
      microphoneStream = null;
      recording = null;
      const permissionDenied = ['NotAllowedError', 'SecurityError'].includes(error.name);
      notify(permissionDenied ? '마이크 사용 권한이 필요해요. 권한을 허용한 후 다시 시작해 주세요.' : error.message, true);
      render();
    }
  }

  function stopRecording() {
    if (!recording) return;
    const purpose = recording.purpose;
    if (nativeStreaming && window.CabinAndroid?.stopRecording) {
      if (recording.stopping) return;
      recording.stopping = true;
      renderControls();
      window.CabinAndroid.stopRecording();
      return;
    }
    recording = null;
    clearTimeout(nativeStartTimer);
    processing = true;
    processingPurpose = purpose;
    processingMessage = '녹음 파일을 전달하고 있어요.';
    $('job-title').textContent = '대화 정리 중';
    $('job-message').textContent = '녹음 파일을 전달하고 있어요.';
    render();
    try {
      if (window.CabinAndroid?.stopRecording) window.CabinAndroid.stopRecording();
      else if (recorder && recorder.state !== 'inactive') recorder.stop();
      else throw new Error('녹음 데이터가 없어요. 다시 시작해 주세요.');
    } catch (error) {
      processing = false;
      notify(error.message, true);
      render();
    }
  }

  window.onNativeStatus = event => {
    if (!event || typeof event !== 'object') return;
    if (event.error) {
      clearTimeout(nativeStartTimer);
      recording = null;
      nativePendingAudio = event.pendingAudio === true || Number(event.pendingChunks) > 0;
      processing = streamRunning || streamQueue.length > 0 || nativePendingAudio;
      notify(event.error, true);
      render();
      return;
    }
    if (event.recording === false && nativeStreaming) {
      clearTimeout(nativeStartTimer);
      recording = null;
      nativePendingAudio = event.pendingAudio === true || Number(event.pendingChunks) > 0;
      processing = streamRunning || streamQueue.length > 0 || nativePendingAudio;
      render();
      return;
    }
    if (event.recording && recording) {
      if (recording.pending) recording.startedAt = Date.now();
      recording.pending = false;
      recording.microphonePaused = event.microphonePaused === true;
      clearTimeout(nativeStartTimer);
      renderControls();
    }
  };

  function audioBlob(event) {
    if (!event?.base64) throw new Error('녹음된 음성이 없어요. 마이크를 확인하고 다시 녹음해 주세요.');
    const bytes = atob(event.base64);
    const array = new Uint8Array(bytes.length);
    for (let index = 0; index < bytes.length; index++) array[index] = bytes.charCodeAt(index);
    return new Blob([array], {type: event.mime || 'audio/wav'});
  }

  // The official vehicle recognizer supplies text; only final results enter
  // the durable conversation. Interims never become facts or recommendations.
  window.onNativeSpeech = event => {
    if (!event || event.source !== 'pleos_on_device') return;
    if (event.backgroundManaged === true) {
      // The microphone service owns final-result delivery even with no WebView.
      // Replaying a service event must never enqueue the same text a second time.
      const sameConversation = !event.conversation_id || event.conversation_id === state.conversation_id;
      if (!sameConversation && !(event.type === 'status' && (event.pending_results || streamFailure?.nativeManaged))) return;
      if (event.type === 'status') {
        if (event.upload_error === true && event.pending_results) {
          streamFailure = {nativeManaged:true, retrying:false, message:event.error || '보관 중인 발언의 저장 완료를 확인하지 못했어요.'};
        } else if (streamFailure?.nativeManaged && event.pending_results === false) {
          streamFailure = null;
        }
        if (sameConversation && event.recording && event.session_id && event.conversation_id) {
          if (!recording) recording = {purpose:'meeting', conversationId:event.conversation_id,
            startedAt:Date.now() - Math.max(0, Number(event.end) || 0) * 1000, pending:false};
          recording.engine = 'pleos';
          recording.backgroundManaged = true;
        }
        if (recording?.engine === 'pleos' || event.recording) {
          window.onNativeStatus({recording:event.recording, microphonePaused:event.microphone_paused === true, pendingAudio:event.pending_results === true, error:event.error});
        } else if (!recording) {
          nativePendingAudio = event.pending_results === true;
          processing = Boolean(activeJob) || Boolean(streamFailure) || streamRunning || streamQueue.length > 0 || nativePendingAudio;
          if (event.error) notify(event.error, true);
          renderControls();
        }
      } else if (event.status === 'interim' && recording?.backgroundManaged) {
        recording.speechPartial = String(event.text || '').slice(0, 180);
        if (!recording.microphonePaused) $('listening-description').textContent = recording.speechPartial;
      } else if (event.status === 'committed') {
        if (recording?.backgroundManaged) recording.speechPartial = '';
        state.recommendations = [];
        state.recommendation_summary = {};
        voiceRecommendationRequest = null;
        window.CabinAndroid?.stopRecommendationSpeech?.();
        void refreshState();
      }
      return;
    }
    if (event.type === 'status') {
      if (recording && event.conversation_id === recording.conversationId && event.session_id) {
        if (event.recording) recording.engine = 'pleos';
        if (recording.engine === 'pleos') {
          window.onNativeStatus({recording:event.recording, microphonePaused:event.microphone_paused === true, pendingAudio:event.pending_results === true, error:event.error});
        }
      } else if (!recording && event.pending_results === false) {
        nativePendingAudio = false;
        processing = Boolean(streamFailure) || streamRunning || streamQueue.length > 0;
        renderControls();
      }
      return;
    }
    if (event.status === 'interim') {
      if (recording?.engine === 'pleos' && recording.conversationId === event.conversation_id) {
        recording.speechPartial = String(event.text || '').slice(0, 180);
        if (!recording.microphonePaused) $('listening-description').textContent = recording.speechPartial;
      }
      return;
    }
    if (event.status !== 'final' || !event.text || !event.session_id || !Number.isInteger(event.sequence)) return;
    if (recording?.engine === 'pleos' && recording.conversationId === event.conversation_id) recording.speechPartial = '';
    if (streamQueue.some(item => item.kind === 'speech' && item.session === event.session_id && item.sequence === event.sequence)) return;
    if (streamQueue.length >= 64) { notify('저장 중인 발언이 많아요. 잠시 후 다시 연결해 주세요.', true); return; }
    nativePendingAudio = true;
    streamQueue.push({kind:'speech', text:String(event.text), session:event.session_id, sequence:event.sequence,
      conversationId:event.conversation_id, start:event.start, end:event.end, recordingStartedAt:event.recording_started_at, recognizedAt:event.recognized_at});
    drainStreamQueue();
  };

  // The native bridge needs a synchronous acknowledgment for backpressure.
  window.onNativeAudio = event => {
    if (event?.purpose === 'stream') {
      try {
        if (!currentStreamSession) currentStreamSession = event.sessionId || (window.crypto?.randomUUID?.() || 'session-' + Date.now());
        const session = event.sessionId || currentStreamSession;
        if (streamQueue.some(chunk => chunk.session === session && chunk.sequence === event.sequence)) return true;
        if (streamQueue.length >= 8) return false;
        streamQueue.push({blob: audioBlob(event), session, conversationId: event.conversationId || null,
          sequence: event.sequence, start: event.start || 0, jobId: null, recordingStartedAt:event.recordingStartedAt});
        drainStreamQueue();
        return true;
      } catch (error) { notify(error.message, true); return false; }
    }
    clearTimeout(nativeStartTimer);
    recording = null;
    try { uploadAudio(audioBlob(event), 'meeting', 'recording.wav', event?.conversationId, event?.recordingStartedAt); }
    catch (error) { processing = false; notify(error.message, true); render(); }
    return true;
  };

  async function drainStreamQueue() {
    if (streamRunning || streamFailure) return;
    streamRunning = true;
    processing = true;
    processingPurpose = 'meeting';
    while (streamQueue.length) {
      const chunk = streamQueue[0];
      processingMessage = '인식한 발언을 대화에 추가하고 있어요.';
      renderControls();
      try {
        await commitStreamChunk(chunk);
        // The native process retains the recording until the server has
        // committed it. A WebView reload resends the same idempotency key.
        if (chunk.kind === 'speech') window.CabinAndroid?.acknowledgeSpeech?.(chunk.session, chunk.sequence);
        else window.CabinAndroid?.acknowledgeAudio?.(chunk.session, chunk.sequence);
        streamQueue.shift();
        await refreshState();
      } catch (error) {
        streamFailure = error;
        if (recording) stopRecording();
        notify(error.status === 409 ? error.message : '발언을 전송하지 못해 보관 중이에요. 연결을 확인한 뒤 다시 전송해 주세요.', true);
        break;
      } finally { activeJob = null; }
    }
    streamRunning = false;
    processing = Boolean(streamFailure) || streamQueue.length > 0 || nativePendingAudio;
    processingMessage = '';
    await refreshState();
    render();
  }

  async function commitStreamChunk(chunk) {
    if (typeof chunk.conversationId !== 'string' || !chunk.conversationId) {
      const error = new Error('이전 버전에서 녹음한 발언이에요. 현재 대화에 섞이지 않도록 전송을 보류했어요.');
      error.status = 409;
      throw error;
    }
    if (chunk.kind === 'speech') {
      const result = await request('/api/speech/final', {method:'POST', headers:{'Content-Type':'application/json'},
        body:JSON.stringify({conversation_id:chunk.conversationId, session_id:chunk.session,
          sequence:chunk.sequence, text:chunk.text, start:chunk.start, end:chunk.end, recording_started_at:chunk.recordingStartedAt, recognized_at:chunk.recognizedAt, source:'pleos_on_device'}), timeout:12000});
      if (result.committed !== true) throw new Error('발언의 저장 결과를 확인하지 못했어요.');
      return;
    }
    // If the server restarts while we are polling, submit the identical chunk
    // once more. A durable receipt answers committed:true without re-appending.
    for (let restartAttempt = 0; restartAttempt < 2; restartAttempt++) {
      if (!chunk.jobId) {
        let result;
        for (let attempt = 0; attempt < 3; attempt++) {
          try {
            result = await request('/api/transcribe?stream=1', {method: 'POST', headers: {'Content-Type': chunk.blob.type, 'X-Stream-Id': chunk.session, 'X-Sequence': String(chunk.sequence), 'X-Start-Seconds': String(chunk.start), 'X-Conversation-Id': chunk.conversationId, ...(chunk.recordingStartedAt ? {'X-Recording-Started-At':chunk.recordingStartedAt} : {})}, body: chunk.blob, timeout: 30000});
            break;
          } catch (error) {
            if (attempt === 2 || (error.status >= 400 && error.status < 500)) throw error;
            await new Promise(resolve => setTimeout(resolve, 1500));
          }
        }
        if (result.committed === true) return;
        chunk.jobId = result.job_id || result.id;
        if (!chunk.jobId) throw new Error('서버가 발언 접수 결과를 보내지 않았어요.');
      }
      activeJob = chunk.jobId;
      let failures = 0;
      try {
        while (true) {
          let job;
          try { job = await request('/api/jobs/' + encodeURIComponent(chunk.jobId), {timeout: 12000}); failures = 0; }
          catch (error) {
            if (error.status === 404 || (error.status >= 400 && error.status < 500) || ++failures >= 3) throw error;
            await new Promise(resolve => setTimeout(resolve, 1500));
            continue;
          }
          processingMessage = job.message || '발언을 텍스트로 바꾸고 있어요.';
          if (['done', 'complete', 'completed', 'success', 'succeeded'].includes(job.status)) return;
          if (['failed', 'error', 'cancelled'].includes(job.status)) {
            chunk.jobId = null;
            throw new Error(job.error || job.message || '이번 발언을 처리하지 못했어요.');
          }
          renderControls();
          await new Promise(resolve => setTimeout(resolve, 850));
        }
      } catch (error) {
        if (error.status === 404 && restartAttempt === 0) { chunk.jobId = null; continue; }
        throw error;
      }
    }
  }

  function retryStreamAudio() {
    if (!streamFailure || streamRunning) return;
    if (streamFailure.nativeManaged) {
      try {
        if (!window.CabinAndroid?.retrySpeechResults) throw new Error('앱을 다시 열어 발언 전송을 재시도해 주세요.');
        streamFailure.retrying = true;
        window.CabinAndroid.retrySpeechResults();
        renderControls();
      } catch (error) { streamFailure.retrying = false; notify(error.message, true); }
      return;
    }
    streamFailure = null;
    $('notice').hidden = true;
    return drainStreamQueue();
  }

  function discardStreamAudio() {
    if (streamFailure?.nativeManaged && !streamRunning && !streamFailure.retrying) {
      try {
        if (window.CabinAndroid?.discardSpeechResults?.() !== true) {
          notify('발언 전송이 마무리 중이에요. 잠시 후 다시 눌러주세요.');
          return;
        }
        streamFailure.retrying = true;
        renderControls();
        notify('보관 중인 발언을 다시 보내지 않도록 요청했어요. 이미 저장된 내용은 남아요.');
      } catch (error) { notify(error.message, true); }
      return;
    }
    if (!streamFailure || streamRunning || !streamQueue.length) return;
    const chunk = streamQueue[0];
    try {
      if (chunk.kind === 'speech') window.CabinAndroid?.acknowledgeSpeech?.(chunk.session, chunk.sequence);
      else window.CabinAndroid?.discardAudio?.(chunk.session, chunk.sequence);
    }
    catch (error) { notify('‘다시 보내지 않기’를 적용하지 못했어요. ' + error.message, true); return; }
    streamQueue.shift();
    streamFailure = null;
    notify('이 발언은 다시 보내지 않아요. 이미 전달된 내용은 대화에 추가될 수 있어요.');
    return drainStreamQueue();
  }

  window.onNativeSaveResult = event => {
    if (event?.success) notify(event.message || '대화록을 저장했어요.');
    else if (event?.error) notify(event.error, true);
  };

  async function uploadAudio(blob, purpose, filename, conversationId, recordingStartedAt) {
    if (streamFailure || streamRunning || streamQueue.length || nativePendingAudio) {
      notify('보관 중인 발언을 다시 전송하거나 ‘다시 보내지 않기’를 선택해 주세요.', true);
      return;
    }
    if (typeof conversationId !== 'string' || !conversationId) {
      notify('녹음을 시작한 대화를 확인하지 못했어요. 현재 대화에 섞이지 않도록 전송하지 않았어요.', true);
      return;
    }
    recording = null;
    processing = true;
    processingPurpose = purpose;
    processingMessage = '녹음 파일을 전달하고 있어요.';
    switchTab('transcript');
    $('job-title').textContent = '대화 정리 중';
    $('job-message').textContent = '녹음 파일을 전달하고 있어요.';
    render();
    try {
      if (!blob.size) throw new Error('빈 녹음 파일이에요. 음성이 들어 있는 파일을 선택해 주세요.');
      if (blob.size > 64 * 1024 * 1024) throw new Error('64MB 이하의 녹음 파일을 선택해 주세요.');
      const path = '/api/transcribe';
      const result = await request(path, {method: 'POST', headers: {'Content-Type': blob.type || 'application/octet-stream', 'X-Filename': encodeURIComponent(filename || 'audio'), 'X-Conversation-Id': conversationId, ...(recordingStartedAt ? {'X-Recording-Started-At':recordingStartedAt} : {})}, body: blob, timeout: 120000});
      const jobId = result.job_id || result.id;
      if (jobId) watchJob(jobId, purpose);
      else {
        processing = false;
        await refreshState();
        notify('대화록을 정리했어요. 좌석과 내용을 확인해 주세요.');
      }
    } catch (error) {
      processing = false;
      notify(error.message, true);
      render();
    }
  }

  function watchJob(id, purpose = 'meeting') {
    if (!id || activeJob === id) return;
    clearTimeout(pollTimer);
    activeJob = id;
    processing = true;
    processingPurpose = purpose;
    let failures = 0;
    const poll = async () => {
      try {
        const job = await request('/api/jobs/' + encodeURIComponent(id), {timeout: 12000});
        if (activeJob !== id) return;
        failures = 0;
        processingMessage = job.message || '';
        $('job-message').textContent = job.message || '음성을 분석하고 있어요.';
        $('places-job-message').textContent = job.message || '대화에서 나온 조건으로 장소를 찾고 있어요.';
        if (['done', 'complete', 'completed', 'success', 'succeeded'].includes(job.status)) {
          activeJob = null;
          processing = false;
          await refreshState();
          if (purpose === 'recommend') speakRequestedRecommendations();
          notify(purpose === 'recommend'
            ? (asArray(state.recommendations).length ? `대화에 맞춰 장소 ${state.recommendations.length}곳을 찾았어요.` : '조건에 맞는 장소를 찾지 못했어요. 검색 범위를 확인해 주세요.')
            : '대화록을 정리했어요. 발언을 누르면 내용을 수정할 수 있어요.');
          render();
          return;
        }
        if (['failed', 'error', 'cancelled'].includes(job.status)) throw new Error(job.error || job.message || '음성을 처리하지 못했어요. 다시 시도해 주세요.');
        renderControls();
        pollTimer = setTimeout(poll, 1000);
      } catch (error) {
        failures++;
        if (failures < 3 && !/failed|처리하지/.test(error.message)) {
          $('job-message').textContent = '연결 상태를 다시 확인하고 있어요.';
          pollTimer = setTimeout(poll, 2000);
          return;
        }
        activeJob = null;
        processing = false;
        notify(error.message, true);
        render();
      }
    };
    poll();
  }

  function openRegionPicker(findAfter = false, choices = [], choiceKey = null) {
    if (recording || processing || regionSaving) return;
    recommendAfterRegion = findAfter;
    areaChoiceKey = choiceKey;
    $('region-picker-input').value = automaticSearchMode() ? '' : regionDraftDirty ? $('region-input').value.trim() : state.region || '';
    $('region-picker-error').hidden = true;
    $('region-picker-save').textContent = findAfter ? '이 지역에서 찾기' : '지역 선택';
    $('area-choice-list').innerHTML = choices.map(region => `<button type="button" class="button button-outline" data-area-choice="${escapeHtml(region)}">${escapeHtml(region)}</button>`).join('');
    $('area-choice-section').hidden = choices.length === 0;
    renderLocation();
    if (setDialogOpen('region-dialog', true, choices.length ? 'area-choice-list' : automaticSearchMode() ? 'current-location-button' : 'region-picker-input') && !automaticSearchMode()) $('region-picker-input').select();
  }

  async function useConversationBasis() {
    if (regionSaving || recording || processing) return;
    regionSaving = true;
    renderLocation(); renderControls();
    let saved = false;
    try {
      await jsonRequest('/api/search-mode','POST',{mode:'conversation'});
      regionDraftDirty = false;
      setDialogOpen('region-dialog', false);
      saved = true;
      render();
    } catch (error) {
      $('region-picker-error').textContent = error.message;
      $('region-picker-error').hidden = false;
    } finally { regionSaving = false; renderLocation(); renderControls(); }
    if (saved && recommendAfterRegion) await recommend();
  }

  async function saveRegion(event) {
    event.preventDefault();
    if (regionSaving || recording || processing) return;
    const region = $('region-picker-input').value.trim();
    if (!region) { $('region-picker-input').focus(); return; }
    if (areaChoiceKey) { await selectConversationArea(region, true); return; }
    regionSaving = true;
    $('region-picker-save').disabled = true;
    $('region-picker-cancel').disabled = true;
    $('region-picker-input').disabled = true;
    $('region-picker-error').hidden = true;
    renderControls();
    let saved = false;
    try {
      const result = await jsonRequest('/api/region', 'POST', {region});
      applyState(result);
      regionDraftDirty = false;
      $('region-input').value = region;
      $('selected-region-label').textContent = '검색 지역 · ' + region;
      setDialogOpen('region-dialog', false);
      saved = true;
      render();
    } catch (error) {
      $('region-picker-error').textContent = error.message;
      $('region-picker-error').hidden = false;
    } finally {
      regionSaving = false;
      $('region-picker-save').disabled = false;
      $('region-picker-cancel').disabled = false;
      $('region-picker-input').disabled = false;
      renderControls();
    }
    if (saved && recommendAfterRegion) await recommend();
  }

  async function selectConversationArea(region, custom = false) {
    if (!areaChoiceKey || regionSaving || recording || processing) return;
    const choice = {region, key: areaChoiceKey};
    if (custom) choice.custom = true;
    setDialogOpen('region-dialog', false);
    await recommend(choice);
  }

  async function recommend(areaChoice = null) {
    if (recording || processing || regionSaving || streamFailure || !asArray(state.segments).length) return;
    const region = $('region-input').value.trim();
    if (!areaChoice && currentLocationMode() && !state.location?.available) { openRegionPicker(true); return; }
    if (!areaChoice && !automaticSearchMode() && !region) { openRegionPicker(true); return; }
    $('notice').hidden = true;
    processing = true;
    processingPurpose = 'recommend';
    showPlaceDetail = false;
    processingMessage = '대화에서 나온 조건을 모으고 있어요.';
    $('job-title').textContent = '함께 갈 장소 찾는 중';
    $('job-message').textContent = '대화에서 나온 조건을 모으고 있어요.';
    renderControls();
    try {
      const options = areaChoice ? {mode:'conversation', area_choice:areaChoice} : automaticSearchMode() ? {mode:searchMode()} : {mode:'manual', region};
      const result = await jsonRequest('/api/recommend', 'POST', options);
      regionDraftDirty = false;
      if (result.job_id) {
        if (result.state) render();
        watchJob(result.job_id, 'recommend');
        switchTab('places');
      } else {
        processing = false;
        if (result.recommendations) applyState(result);
        await refreshState();
        speakRequestedRecommendations();
        switchTab('places');
        if (!asArray(state.recommendations).length) notify(result.message || '조건에 맞는 장소를 찾지 못했어요. 지역이나 대화 내용을 확인해 주세요.');
      }
    } catch (error) {
      processing = false;
      if (error.needsAreaChoice || error.areaChoices?.length || /최신 위치|현재 위치/.test(error.message)) {
        openRegionPicker(true, error.areaChoices || [], error.areaChoiceKey);
        $('region-picker-error').textContent = error.message;
        $('region-picker-error').hidden = false;
      } else notify(error.message, true);
    }
    renderControls();
  }

  function openEditor(index) {
    if (recording || processing) return;
    const segment = asArray(state.segments)[index];
    if (!segment) return;
    editId = segment.id ?? index;
    $('edit-seat').value = seats.some(seat => seat.id === segment.seat) ? segment.seat : 'unknown';
    $('edit-text').value = segment.text || '';
    $('edit-error').hidden = true;
    setDialogOpen('edit-dialog', true, 'edit-text');
  }

  async function saveEditor(event) {
    event.preventDefault();
    const text = $('edit-text').value.trim();
    if (!text) return;
    $('edit-save').disabled = true;
    $('edit-error').hidden = true;
    try {
      await jsonRequest('/api/segments/' + encodeURIComponent(editId), 'PATCH', {seat: $('edit-seat').value, text});
      await refreshState();
      setDialogOpen('edit-dialog', false);
      notify('발언을 수정했어요. 장소 추천을 누르면 새 내용을 반영해요.');
    } catch (error) { $('edit-error').textContent = error.message; $('edit-error').hidden = false; }
    finally { $('edit-save').disabled = false; }
  }

  async function exportTranscript() {
    try {
      const response = await fetch('/api/export', {cache: 'no-store'});
      if (!response.ok) throw new Error('대화록을 저장하지 못했어요.');
      const markdown = await response.text();
      if (window.CabinAndroid?.saveTranscript) {
        window.CabinAndroid.saveTranscript(markdown);
        return;
      }
      const url = URL.createObjectURL(new Blob([markdown], {type: 'text/markdown;charset=utf-8'}));
      const anchor = document.createElement('a');
      anchor.href = url;
      anchor.download = '모아_대화록_' + new Date().toISOString().slice(0, 10) + '.md';
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1500);
    } catch (error) { notify(error.message, true); }
  }

  $('tab-transcript').addEventListener('click', () => switchTab('transcript'));
  $('tab-places').addEventListener('click', () => switchTab('places'));
  $('tab-saved').addEventListener('click', () => switchTab('saved'));
  $('places-list').addEventListener('click', event => { if (event.target.closest('a, details')) return; const card = event.target.closest('[data-place-id]'); if (card) selectPlace(card.dataset.placeId); });
  $('place-filters').addEventListener('click', event => { const button = event.target.closest('[data-place-filter]'); if (!button) return; $('place-filters').dataset.selected = button.dataset.placeFilter; renderPlaces(); });
  $('trip-cancel-button').addEventListener('click', cancelTrip);
  $('saved-list').addEventListener('click', event => { const button = event.target.closest('[data-rate-place]'); if (button) openRating(button.dataset.ratePlace, button.dataset.rateVisit); });
  $('rating-form').addEventListener('submit', saveRating);
  $('rating-cancel').addEventListener('click', () => { if (!$('rating-save').disabled) setDialogOpen('rating-dialog', false); });
  $('detail-back').addEventListener('click', () => { showPlaceDetail = false; renderTrip(); });
  $('guidance-button').addEventListener('click', startGuidance);
  $('native-preview-button').addEventListener('click', previewNativePlace);
  $('manual-arrival-button').addEventListener('click', openManualArrival);
  $('manual-arrival-cancel').addEventListener('click', closeManualArrival);
  $('manual-arrival-confirm').addEventListener('click', confirmManualArrival);
  $('arrival-save').addEventListener('click', () => decideSave(true));
  $('arrival-skip').addEventListener('click', () => decideSave(false));
  $('notice-close').addEventListener('click', () => { $('notice').hidden = true; });
  $('record-button').addEventListener('click', () => { if (recording) stopRecording(); else startRecording('meeting'); });
  $('stream-retry-button').addEventListener('click', retryStreamAudio);
  $('stream-discard-button').addEventListener('click', discardStreamAudio);
  $('recommend-button').addEventListener('click', () => recommend());
  $('region-picker-button').addEventListener('click', () => openRegionPicker());
  $('current-location-button').addEventListener('click', useConversationBasis);
  $('area-choice-list').addEventListener('click', event => { const button = event.target.closest('[data-area-choice]'); if (!button) return; selectConversationArea(button.dataset.areaChoice); });
  $('region-input').addEventListener('click', () => { if (automaticSearchMode()) openRegionPicker(); });
  $('region-picker-form').addEventListener('submit', saveRegion);
  $('region-picker-cancel').addEventListener('click', () => { if (!regionSaving) setDialogOpen('region-dialog', false); });
  $('region-input').addEventListener('input', () => { regionDraftDirty = true; });
  $('region-form').addEventListener('submit', event => { event.preventDefault(); recommend(); });
  $('transcript-list').addEventListener('click', event => { const target = event.target.closest('[data-edit-index]'); if (target) openEditor(Number(target.dataset.editIndex)); });
  $('new-messages-button').addEventListener('click', scrollToLatest);
  $('transcript-scroll').addEventListener('scroll', () => { if (nearLatest()) { unreadMessages = 0; updateUnread(); } }, {passive: true});
  $('edit-form').addEventListener('submit', saveEditor);
  ['edit-close', 'edit-cancel'].forEach(id => $(id).addEventListener('click', () => { if (!$('edit-save').disabled) setDialogOpen('edit-dialog', false); }));
  $('export-button').addEventListener('click', exportTranscript);
  $('reset-button').addEventListener('click', () => { if (!recording && !processing && !streamFailure) { $('reset-error').hidden = true; $('reset-description').textContent = state.demo_session ? '샘플과 보관 중인 원래 대화·추천을 지워요. 저장한 장소는 유지돼요. 남겨둘 대화는 먼저 저장해 주세요.' : '현재 대화와 추천 결과를 지워요. 저장한 장소는 유지돼요. 남겨둘 대화는 먼저 저장해 주세요.'; setDialogOpen('reset-dialog', true, 'reset-cancel'); } });
  $('reset-cancel').addEventListener('click', () => { if (!$('reset-confirm').disabled) setDialogOpen('reset-dialog', false); });
  $('reset-confirm').addEventListener('click', async () => {
    if (recording || processing || streamFailure || streamQueue.length || nativePendingAudio) return;
    $('reset-confirm').disabled = true;
    $('reset-error').hidden = true;
    try {
      await jsonRequest('/api/reset', 'POST', {});
      setDialogOpen('reset-dialog', false);
      $('notice').hidden = true;
      await refreshState();
      switchTab('transcript');
    } catch (error) { $('reset-error').textContent = error.message; $('reset-error').hidden = false; }
    finally { $('reset-confirm').disabled = false; }
  });
  async function changeDemo(exit = false) {
    if (recording || processing || regionSaving || streamFailure || streamQueue.length || nativePendingAudio || guidanceStarting || guidanceCancelling) return;
    if (['navigating', 'arrived'].includes(state.trip?.status)) {
      notify(state.trip.status === 'navigating' ? '길안내가 진행 중이에요. 안내를 마친 뒤 샘플을 바꿔 주세요.' : '도착한 장소의 저장 여부를 먼저 선택해 주세요.'); return;
    }
    processing = true;
    processingPurpose = 'meeting';
    processingMessage = exit ? '기존 대화로 돌아가고 있어요.' : '샘플 대화를 불러오고 있어요.';
    renderControls();
    try {
      await jsonRequest(exit ? '/api/demo/exit' : '/api/demo', 'POST', {});
      await refreshState();
      showPlaceDetail = false;
      switchTab('transcript');
      requestAnimationFrame(() => {
        $('transcript-scroll').scrollTop = 0;
        unreadMessages = 0;
        updateUnread();
      });
      notify(exit ? '기존 대화로 돌아왔어요.' : '새 샘플을 불러왔어요. 장소 추천을 눌러보세요.');
    } catch (error) { notify(error.message, true); }
    finally { processing = false; renderControls(); }
  }
  $('demo-button').addEventListener('click', () => changeDemo());
  $('demo-exit-button').addEventListener('click', () => changeDemo(true));
  $('places-empty-sample').addEventListener('click', () => changeDemo());
  $('places-empty-action').addEventListener('click', () => {
    const mode = $('places-empty-action').dataset.mode;
    if (mode === 'region') openRegionPicker(true);
    else if (mode === 'recommend') recommend();
    else { switchTab('transcript'); startRecording('meeting'); }
  });
  document.addEventListener('keydown', handleDialogKeydown);
  document.addEventListener('focusin', keepDialogFocus);

  const gleoHandledActions = new Set();
  window.onGleoAction = async event => {
    if (!event || typeof event.id !== 'string' || !event.id || gleoHandledActions.has(event.id)) return;
    gleoHandledActions.add(event.id);
    if (gleoHandledActions.size > 64) gleoHandledActions.delete(gleoHandledActions.values().next().value);
    const reply = (status, message, data = {}) => {
      try {
        return window.CabinAndroid?.completeGleoAction?.(event.id, status, message, JSON.stringify([data])) === true;
      } catch { return false; }
    };
    let recommendationFresh = event.action !== 'recommend_places';
    if (!initialStateLoaded || !recommendationFresh) {
      const previousVersion = stateSnapshotVersion;
      await Promise.race([refreshState(), new Promise(resolve => setTimeout(resolve, 3000))]);
      recommendationFresh = stateSnapshotVersion > previousVersion;
    }
    if (event.expiresAt && Date.now() >= event.expiresAt) return;
    if (!initialStateLoaded) {
      reply('asking', 'The app is still loading. Please try again shortly.', {state:'loading'});
      return;
    }
    if (event.action === 'recommend_places' && !recommendationFresh) {
      reply('asking', 'The latest conversation could not be checked. Please try again shortly.', {state:'refresh_required'});
      return;
    }
    if (!['start_conversation', 'recommend_places', 'show_saved'].includes(event.action)) {
      reply('fail', 'This action is not supported.');
      return;
    }
    if (activeDialog || processing || regionSaving || streamFailure || guidanceStarting || guidanceCancelling) {
      reply('asking', 'Please finish the current action in the app first.', {state:'busy'});
      return;
    }
    if (event.action === 'show_saved') {
      if (reply('success', 'Opening the saved places screen.', {screen:'saved', count:asArray(state.saved_places).length})) switchTab('saved');
      return;
    }
    if (recording || streamRunning || streamQueue.length || nativePendingAudio) {
      reply('asking', 'A conversation is being recorded or saved. Please finish it first.', {state:'recording'});
      return;
    }
    if (event.action === 'start_conversation') {
      if (!state.models?.ready || !state.conversation_id || (state.demo_session && ['navigating', 'arrived'].includes(state.trip?.status))) {
        reply('asking', 'The conversation is not ready to start. Please check the app screen.', {state:'not_ready'});
        return;
      }
      // Acknowledge silently before opening our microphone. Starting is not a
      // claim that Gleo has released its microphone or that recording succeeded.
      if (reply('success', 'The app will now request the microphone to start the conversation.', {state:'requested', screen:'transcript'})) {
        switchTab('transcript');
        void startRecording('meeting');
      }
      return;
    }
    if (!asArray(state.segments).length) {
      if (reply('asking', 'There is no conversation yet. Please start a conversation first.', {state:'empty_conversation'})) {
        switchTab('transcript');
        notify('먼저 대화를 나눠 주세요. 그 대화로 장소를 추천할게요.');
      }
      return;
    }
    const places = asArray(state.recommendations).filter(place => place && place.name).slice(0, 3);
    if (places.length) {
      // The server invalidates recommendations whenever the conversation changes.
      // Only current results are supplied; unverified conditions remain explicit.
      const candidates = places.map(place => ({name:String(place.name).slice(0,100),
        reason:String(place.reason_summary || '').slice(0,350),
        unverified:asArray(place.cautions || place.warnings).map(item => typeof item === 'string' ? item : item.text || '').filter(Boolean).slice(0,3)}));
      if (reply('success', 'Read these recommendation names and reasons in Korean. Do not claim that unverified conditions are confirmed.',
          {state:'ready', count:asArray(state.recommendations).length, places:candidates})) switchTab('places');
      return;
    }
    // Search and area selection continue in the existing flow. Gleo's response
    // reports acceptance only, never fabricated or not-yet-ready search results.
    if (reply('success', 'The app is handling the recommendation request. Results are not ready yet.', {state:'requested', screen:'places'})) {
      voiceRecommendationRequest = {conversationId:state.conversation_id, deadline:Date.now() + 90000};
      switchTab('places');
      void recommend();
    }
  };

  setInterval(() => {
    $('clock').textContent = new Intl.DateTimeFormat('ko-KR', {hour: '2-digit', minute: '2-digit', hour12: false, timeZone: 'Asia/Seoul'}).format(new Date());
    if (!recording) return;
    const seconds = (Date.now() - recording.startedAt) / 1000;
    $('listening-time').textContent = elapsed(seconds);
    if (recording.pending) return;
    if (seconds >= recordingLimits.meetingMaxSeconds) stopRecording();
  }, 250);
  // Periodic polling reuses an in-flight request; explicit post-mutation refreshes
  // request one following snapshot instead of silently dropping that refresh.
  stateTimer = setInterval(() => refreshState(false), 1200);
  window.addEventListener('pagehide', () => {
    clearInterval(stateTimer);
    clearTimeout(pollTimer);
    microphoneStream?.getTracks().forEach(track => track.stop());
  });
  render();
  refreshState();
  refreshNavigationState();
  refreshSpeakerState();
  $('recommend-listen').addEventListener('click', () => {
    if (['preparing', 'speaking'].includes(nativeSpeaker.state)) window.CabinAndroid?.stopRecommendationSpeech?.();
    else speakRecommendations();
  });
  window.addEventListener('focus', refreshNavigationState);
  window.addEventListener('focus', refreshSpeakerState);
})();
