package local.pleos.cabin;

import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.os.Handler;
import android.os.Looper;
import android.os.SystemClock;
import android.util.Log;
import ai.pleos.playground.navi.constants.NaviErrorCode;
import ai.pleos.playground.navi.constants.NaviMode;
import ai.pleos.playground.navi.constants.NavigationStatus;
import ai.pleos.playground.navi.constants.RouteOption;
import ai.pleos.playground.navi.data.BookmarkInfo;
import ai.pleos.playground.navi.data.DestinationArrivedInfo;
import ai.pleos.playground.navi.data.DestinationInfo;
import ai.pleos.playground.navi.data.NavigationScreenRequest;
import ai.pleos.playground.navi.data.RecentDestinationInfo;
import ai.pleos.playground.navi.data.RouteInfo;
import ai.pleos.playground.navi.data.RouteStartInfo;
import ai.pleos.playground.navi.data.RouteStateInfo;
import ai.pleos.playground.navi.helper.NaviHelper;
import ai.pleos.playground.navi.helper.listener.NaviHelperEventListener;
import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import kotlin.Unit;
import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

/** Public NaviHelper integration. No simulated guidance or inferred arrival. */
final class NativeNavigation {
    interface Listener { void onNavigationEvent(String json); }
    private static NativeNavigation instance;
    private final Context context;
    private Listener listener;
    private final Handler main = new Handler(Looper.getMainLooper());
    private final ExecutorService network = Executors.newSingleThreadExecutor();
    private final SharedPreferences preferences;
    private NaviHelper helper;
    private volatile boolean connected;
    private volatile String snapshot = "{\"status\":\"connecting\",\"available\":false,\"connected\":false}";
    private boolean closed;
    private boolean connecting;
    private boolean routeSignal;
    private String observedRouteStartName = "";
    private long observedRouteStartAt;
    private boolean routeGuiding;
    private long routeStateAt;
    private long destinationAt;
    private boolean destinationMatched;
    private boolean confirmed;
    private boolean arrived;
    private boolean syncing;
    private Place active;
    private Place pending;
    private final NativePoiCache nativePois = new NativePoiCache();
    private final PreviewSearchGate previewSearchGate = new PreviewSearchGate();
    private Runnable previewSearch;
    private NaviMode nativeRouteMode;
    private long nativeRouteModeAt;
    private boolean pendingPreview;
    private Place recoveryCandidate;
    private boolean recoveryEligible = true;
    private boolean recoveryFetching;
    private int recoveryAttempts;
    private long ownershipGeneration;
    private boolean recoveryRouteGuiding;
    private long recoveryRouteAt;
    private boolean recoveryDestinationMatched;
    private long recoveryDestinationAt;
    private String recoveryName = "";
    private String recoveryAddress = "";
    private Runnable recoveryRetry;
    private String confirmedName = "";
    private String confirmedAddress = "";
    private Runnable routeDeadline;
    private final NavigationPreparationGate preparationGate = new NavigationPreparationGate();
    private Place preparing;
    private long preparationToken;
    private Runnable preparationDeadline;
    private final NavigationTrackingLease trackingLease = new NavigationTrackingLease();
    private boolean cancelRequested;
    private JSONArray outbox;
    private NaviHelperEventListener events;
    private long helperGeneration;
    private long knownPlacesRequestedGeneration = -1;
    private long connectedAt;
    private long connectionAttemptAt;
    private long probeOutstandingSince;
    private final NavigationResponseRecovery responseRecovery = new NavigationResponseRecovery();
    private long idleSince;
    private long idleRouteAt;
    private long emptyDestinationAt;
    private long emptyDestinationSince;
    private int idleSamples;
    private boolean idleRoute;
    private boolean destinationEmpty;
    private static final long PROBE_INTERVAL_MS = 5000;
    private static final long RESPONSE_TIMEOUT_MS = 15000;
    private static final long IDLE_CONFIRM_MS = 15000;
    private final Runnable connectionWatch = new Runnable() {
        @Override public void run() {
            if (closed) return;
            if (active != null || pending != null || listener != null) {
                long now = SystemClock.elapsedRealtime();
                if (connecting && now - connectionAttemptAt >= RESPONSE_TIMEOUT_MS) {
                    reconnect("순정 내비 연결이 지연되어 다시 연결하고 있어요.");
                } else if (connected && probeOutstandingSince != 0 && now - probeOutstandingSince >= RESPONSE_TIMEOUT_MS) {
                    NavigationResponseRecovery.Action action = responseRecovery.timedOut();
                    if (action == NavigationResponseRecovery.Action.RECONNECT) {
                        reconnect("순정 내비의 상태 응답이 없어 연결을 다시 확인하고 있어요.");
                    } else {
                        // initialize() confirms the service binding, not state callbacks.
                        // Releasing it every 15 seconds can interrupt a working native route.
                        // Keep polling this binding after one retry until callbacks recover.
                        probeOutstandingSince = 0;
                        if (action == NavigationResponseRecovery.Action.REPORT) {
                            Log.w("CabinNavigation", "State callbacks unavailable after one reconnect; keeping service binding");
                            emit("status", "error", false, active,
                                "순정 내비는 연결됐지만 안내 상태 응답이 지연되고 있어요. 내비 화면에서 안내 여부를 확인해 주세요.", "pleos", false);
                        }
                        queryDestination();
                    }
                } else if (connected) {
                    queryDestination();
                } else if (!connecting) {
                    initialize();
                }
            }
            main.postDelayed(this, PROBE_INTERVAL_MS);
        }
    };

    static synchronized NativeNavigation get(Context context) {
        if (instance == null) instance = new NativeNavigation(context.getApplicationContext());
        return instance;
    }

    private NativeNavigation(Context context) {
        this.context = context.getApplicationContext();
        preferences = this.context.getSharedPreferences("cabin-navigation", Context.MODE_PRIVATE);
        try { outbox = new JSONArray(preferences.getString("outbox", "[]")); }
        catch (JSONException error) { outbox = new JSONArray(); }
        // A persisted trip is only a candidate until fresh public SDK observations identify it.
        initialize();
        flushOutbox();
        recoverJourney();
        main.postDelayed(connectionWatch, PROBE_INTERVAL_MS);
    }

    String state() { return snapshot; }
    boolean available() { return connected; }

    // Route ownership and backend delivery live for the process. An Activity only observes them.
    void attach(Listener next) {
        if (previewSearchGate.returnedToCaller()) cancelPreviewSearch();
        listener = next;
        resumeVisibleTracking();
        if (!connected && !connecting) initialize();
        else if (connected) queryDestination();
        publishState();
    }

    void detach(Listener previous) {
        if (listener == previous) {
            // Leaving this Activity is expected when the native panel opens.
            // Only a subsequent return cancels its unfinished second stage.
            previewSearchGate.leftCaller();
            listener = null;
        }
    }

    void publishState() { if (!closed && listener != null) listener.onNavigationEvent(snapshot); }

    private void initialize() {
        if (closed || connected || connecting) return;
        for (String permission : new String[]{"pleos.car.permission.NAVI_ROUTE",
                "pleos.car.permission.NAVI_ROUTE_SEARCH", "pleos.car.permission.NAVI_CUSTOM_MAP"}) {
            if (context.checkSelfPermission(permission) != PackageManager.PERMISSION_GRANTED) {
                emit("status", "error", false, null, "순정 내비 연동 권한이 제공되지 않는 환경이에요.", "pleos", false);
                return;
            }
        }
        connecting = true;
        final long generation = ++helperGeneration;
        resetRecoveryEvidence();
        connectionAttemptAt = SystemClock.elapsedRealtime();
        NaviHelper previous = helper;
        NaviHelperEventListener previousEvents = events;
        helper = null;
        events = null;
        if (previous != null) {
            try { if (previousEvents != null) previous.removeListener(previousEvents); previous.release(); }
            catch (Exception ignored) { }
        }
        emit("status", "connecting", false, active, "순정 내비에 연결하고 있어요.", "pleos", false);
        try {
            final NaviHelper candidate = new NaviHelper(context);
            final NaviHelperEventListener generationEvents = createEvents(generation);
            helper = candidate;
            events = generationEvents;
            candidate.initialize(() -> {
                main.post(() -> {
                    if (closed || generation != helperGeneration) return;
                    connecting = false;
                    connected = true;
                    connectedAt = SystemClock.elapsedRealtime();
                    probeOutstandingSince = 0;
                    resetIdleEvidence();
                    resetRecoveryEvidence();
                    candidate.removeListener(generationEvents);
                    candidate.addListener(generationEvents);
                    preloadKnownPlaces(candidate);
                    emit("status", "ready", true, active, "순정 내비 연결 완료", "pleos", false);
                    if (pending != null) {
                        Place next = pending;
                        boolean preview = pendingPreview;
                        pending = null;
                        execute(next, preview);
                    }
                    queryDestination();
                });
                return Unit.INSTANCE;
            }, error -> {
                main.post(() -> {
                    if (closed || generation != helperGeneration) return;
                    connecting = false;
                    connected = false;
                    Place failed = pending;
                    emit(failed == null ? "status" : pendingPreview ? "preview" : "start", "error", false,
                        failed == null ? active : failed, "순정 내비 서비스에 연결하지 못했어요. 연결 상태를 다시 확인할게요.", "pleos", false);
                    if (failed != null && !pendingPreview) abandonUnconfirmedRoute(failed);
                });
                return Unit.INSTANCE;
            });
        } catch (Exception error) {
            connecting = false;
            connected = false;
            emit("status", "error", false, null, "순정 내비 초기화 실패: " + error.getClass().getSimpleName(), "pleos", false);
            if (preparing != null) abandonUnconfirmedRoute(preparing);
        }
    }

    void request(String input, boolean preview) {
        cancelPreviewSearch();
        cancelPreparedRoute();
        pending = null;
        if (!confirmed) stopBackgroundTracking();
        try {
            Place place = new Place(new JSONObject(input));
            if (!preview && place.tripId.isEmpty()) throw new IllegalArgumentException("이동 기록 번호가 없어요. 장소를 다시 선택해 주세요.");
            invalidateRecovery();
            if (!preview) prepareRoute(place);
            if (!connected) {
                pending = place;
                pendingPreview = preview;
                initialize();
                if (!connecting) {
                    pending = null;
                    cancelPreparedRoute();
                    abandonUnconfirmedRoute(place);
                    emit(preview ? "preview" : "start", "error", false, place,
                        "순정 내비가 연결되지 않았어요. 내비 앱의 지도·권한 상태를 확인해 주세요.", "pleos", false);
                }
                return;
            }
            execute(place, preview);
        } catch (Exception error) {
            cancelPreparedRoute();
            pending = null;
            if (!confirmed) { stopBackgroundTracking(); active = null; }
            emit(preview ? "preview" : "start", "error", false, null,
                error instanceof IllegalArgumentException ? error.getMessage() : "장소의 이름과 좌표를 확인해 주세요.", "pleos", false);
        }
    }

    private void execute(Place place, boolean preview) {
        cancelPreviewSearch();
        try {
            if (preview) {
                // Opening a map/search screen must not detach an already confirmed route.
                if (!place.tripId.isEmpty() && (active == null || !confirmed || arrived)
                        && (active == null || !active.tripId.equals(place.tripId))) {
                    cancelDeadline();
                    active = place;
                    confirmed = false;
                    arrived = false;
                    destinationMatched = false;
                    routeSignal = false;
                    resetGuidanceEvidence();
                    confirmedName = "";
                    confirmedAddress = "";
                }
                // Each screen request foregrounds itself; never add Foreground.
                NativePoiCache.Ref poi = nativePois.get(place.name, place.address, place.latitude, place.longitude);
                if (poi != null) helper.requestNavigationScreen(new NavigationScreenRequest.POIDetailPopup(poi.id, poi.provider));
                else requestPlaceSearch(place);
                emit("preview", "preview_requested", true, place,
                    poi != null ? "순정 내비의 장소 상세를 요청했어요. 제공되는 사진·메뉴·가격을 확인할 수 있어요."
                    : "순정 내비에 장소 검색을 요청했어요. 지점을 선택하면 제공되는 사진·메뉴·가격을 확인할 수 있어요.", "pleos", false);
                return;
            }
            if (preparing != place || !preparationGate.pending(preparationToken)) return;
            helper.requestNavigationScreen(NavigationScreenRequest.Foreground.INSTANCE);
            queryDestination();
        } catch (Exception error) {
            if (!preview) { confirmed = false; cancelPreparedRoute(); pending = null; abandonUnconfirmedRoute(place); }
            emit(preview ? "preview" : "start", "error", false, place,
                "순정 내비 요청을 처리하지 못했어요: " + error.getClass().getSimpleName(), "pleos", false);
        }
    }

    private void prepareRoute(Place place) {
        cancelDeadline();
        resetIdleEvidence();
        active = place;
        cancelRequested = false;
        routeSignal = false;
        destinationMatched = false;
        resetGuidanceEvidence();
        confirmed = false;
        arrived = false;
        confirmedName = "";
        confirmedAddress = "";
        beginBackgroundTracking(place);
        preparing = place;
        final long token = preparationGate.begin(SystemClock.elapsedRealtime());
        preparationToken = token;
        preparationDeadline = () -> {
            if (!preparationGate.expire(token, SystemClock.elapsedRealtime())) return;
            preparing = null;
            preparationDeadline = null;
            if (pending == place) pending = null;
            abandonUnconfirmedRoute(place);
            emit("start", "error", false, place,
                "순정 내비의 준비를 확인하지 못했어요. 내비 화면이 열린 뒤 길찾기를 다시 눌러 주세요.", "pleos", false);
        };
        main.postDelayed(preparationDeadline, NavigationPreparationGate.TIMEOUT_MS);
        emit("status", "connecting", true, place, "순정 내비를 열고 준비 상태를 확인하고 있어요.", "pleos", false);
        // Opening the app is immediate; the provider may need several more seconds
        // after a cold boot. Never send a route until its fresh state response.
        openGuidanceScreen();
    }

    static boolean providerReady(RouteStateInfo info) {
        return info != null && info.getTermsAgreement() && !info.isRouting()
            && (info.getNaviMode() == NaviMode.SAFE_GUIDING || info.getNaviMode() == NaviMode.ROUTE_GUIDING
                || info.getNaviMode() == NaviMode.SIMULATION_GUIDING);
    }

    private boolean startPreparedRoute(RouteStateInfo info) {
        Place place = preparing;
        if (place == null || closed || !connected || helper == null
                || !preparationGate.take(preparationToken, SystemClock.elapsedRealtime(), providerReady(info))) return false;
        cancelPreparedRoute();
        if (pending == place) pending = null;
        // Readiness/destination events from the previous route are not evidence
        // that this request has begun. Only subsequent route callbacks can confirm it.
        routeSignal = false;
        destinationMatched = false;
        resetGuidanceEvidence();
        confirmedName = "";
        confirmedAddress = "";
        try {
            // navi-data 3.0.4's constructor order is latitude, longitude.
            NativePoiCache.Ref nativePoi = nativePois.get(place.name, place.address, place.latitude, place.longitude);
            RouteInfo route = nativePoi == null
                ? new RouteInfo(place.latitude, place.longitude, null, null,
                    place.name, "", place.address, "", RouteOption.RECOMMENDED, "", false)
                : new RouteInfo(nativePoi.latitude, nativePoi.longitude, null, null,
                    nativePoi.nativeName, nativePoi.id, nativePoi.address, "", RouteOption.RECOMMENDED, nativePoi.provider, false);
            helper.requestRoute(route);
            Log.i("CabinNavigation", "Prepared route dispatched once after provider readiness");
            JSONObject dispatched = event("status", "routing", true, place, "순정 내비에서 경로를 찾고 있어요.", "pleos", false);
            dispatched.put("route_dispatched", true);
            publish(dispatched);
            routeDeadline = () -> {
                if (active == place && !confirmed && !arrived) {
                    abandonUnconfirmedRoute(place);
                    emit("start", "error", false, place,
                        "길안내 시작을 확인하지 못했어요. 차량 내비에서 현재 위치와 경로 상태를 확인해 주세요.", "pleos", false);
                }
            };
            main.postDelayed(routeDeadline, 30000);
        } catch (Exception error) {
            abandonUnconfirmedRoute(place);
            emit("start", "error", false, place,
                "순정 내비 요청을 처리하지 못했어요: " + error.getClass().getSimpleName(), "pleos", false);
        }
        return true;
    }

    private void cancelPreparedRoute() {
        if (preparationDeadline != null) main.removeCallbacks(preparationDeadline);
        preparationDeadline = null;
        preparing = null;
        preparationGate.cancel();
    }

    boolean backgroundTrackingRequired() { return trackingLease.required(); }
    String backgroundTrackingOwner() { return trackingLease.owner(); }

    private void beginBackgroundTracking(Place place) {
        trackingLease.begin(place.tripId);
        try { NavigationTrackingService.start(context); }
        catch (RuntimeException failure) { trackingLease.clear(); throw failure; }
    }

    private void stopBackgroundTracking() {
        trackingLease.clear();
        try { NavigationTrackingService.stop(context); }
        catch (RuntimeException failure) { Log.w("CabinNavigation", "Tracking service stop failed", failure); }
    }

    private void resumeVisibleTracking() {
        if (listener == null || active == null || !confirmed || arrived) return;
        try { beginBackgroundTracking(active); }
        catch (RuntimeException failure) { Log.w("CabinNavigation", "Visible tracking resume failed", failure); }
    }

    private void abandonUnconfirmedRoute(Place place) {
        if (active != place || confirmed) return;
        cancelPreparedRoute();
        if (pending == place) pending = null;
        active = null;
        routeSignal = false;
        destinationMatched = false;
        resetGuidanceEvidence();
        stopBackgroundTracking();
    }

    void backgroundTrackingFailed() {
        Place place = active;
        trackingLease.clear();
        if (!confirmed && place != null) abandonUnconfirmedRoute(place);
        emit("status", "error", false, place,
            "백그라운드 길안내 연결을 시작하지 못했어요. 모아 화면에서 다시 시도해 주세요.", "pleos", false);
    }

    void finishTracking(String tripId) {
        if (tripId == null || tripId.isEmpty() || tripId.length() > 160) return;
        // This may arrive before a stale recovery HTTP response creates its candidate,
        // or after Android rejected the foreground service. Neither may revive the trip.
        trackingLease.rememberFinished(tripId);
        if (trackingLease.required() && !trackingLease.owner().equals(tripId)) return;
        if (active != null && !active.tripId.equals(tripId)) return;
        boolean ownsActive = active != null && active.tripId.equals(tripId);
        boolean ownsCandidate = recoveryCandidate != null && recoveryCandidate.tripId.equals(tripId);
        boolean ownsPending = pending != null && pending.tripId.equals(tripId);
        boolean ownsLease = trackingLease.finish(tripId);
        if (!ownsActive && !ownsCandidate && !ownsPending && !ownsLease) return;
        // A completed/manual arrival record stops our observer, not the OEM's route.
        if (ownsActive || ownsCandidate || ownsPending) {
            cancelPreparedRoute();
            cancelDeadline();
            if (pending != null && pending.tripId.equals(tripId)) pending = null;
            invalidateRecovery();
            active = null;
            confirmed = false;
            routeSignal = false;
            destinationMatched = false;
            resetGuidanceEvidence();
        }
        stopBackgroundTracking();
        emit("status", "ready", true, null, "이동 기록의 안내 연결을 마쳤어요.", "pleos", false);
    }

    private void openGuidanceScreen() {
        try {
            Intent launch = context.getPackageManager().getLaunchIntentForPackage("ai.umos.maps.android.navigation.app");
            if (launch == null) {
                Log.i("CabinNavigation", "Native launcher unavailable; using public screen request");
                return;
            }
            launch.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
            context.startActivity(launch);
            Log.i("CabinNavigation", "Native guidance screen opened from user request");
        } catch (RuntimeException unavailable) {
            // Other Pleos environments may expose only the SDK screen request.
            Log.w("CabinNavigation", "Native launcher unavailable; using public screen request", unavailable);
        }
    }

    private void requestPlaceSearch(Place place) {
        long now = SystemClock.elapsedRealtime();
        if (nativeRouteMode == NaviMode.SAFE_GUIDING && now - nativeRouteModeAt < 10000) {
            helper.requestNavigationScreen(new NavigationScreenRequest.SearchPlace(place.searchKeyword()));
            return;
        }
        // Navi42's guidance-mode SearchPlace updates results without opening the
        // left pane. The public RecentDestinations screen opens that same search
        // pane (without a keyboard); SearchMain is not in public SDK 3.0.4.
        // This does not cancel or replace the current route. The SDK offers no
        // screen-ready callback, so use one short, expiring layout delay.
        final NaviHelper requestedHelper = helper;
        final long connectionGeneration = helperGeneration;
        helper.requestNavigationScreen(NavigationScreenRequest.RecentDestinations.INSTANCE);
        final long token = previewSearchGate.begin(now);
        Log.i("CabinNavigation", "preview stage=search_panel");
        previewSearch = () -> {
            previewSearch = null;
            if (!previewSearchGate.take(token, SystemClock.elapsedRealtime(),
                    !closed && connected && helper == requestedHelper && helperGeneration == connectionGeneration)) return;
            try {
                requestedHelper.requestNavigationScreen(new NavigationScreenRequest.SearchPlace(place.searchKeyword()));
                Log.i("CabinNavigation", "preview stage=place_search");
            } catch (Exception error) {
                emit("preview", "error", false, place, "순정 내비 검색을 요청하지 못했어요. 다시 눌러 주세요.", "pleos", false);
            }
        };
        main.postDelayed(previewSearch, PreviewSearchGate.LAYOUT_DELAY_MS);
    }

    private void cancelPreviewSearch() {
        if (previewSearch != null) main.removeCallbacks(previewSearch);
        previewSearch = null;
        previewSearchGate.cancel();
    }

    /** Main-thread gate: an old callback must never reopen navigation later. */
    static final class PreviewSearchGate {
        static final long LAYOUT_DELAY_MS = 600;
        static final long MAX_AGE_MS = 1500;
        private long generation;
        private long started;
        private boolean pending;
        private boolean handedOff;
        long begin(long now) {
            started = now;
            pending = true;
            handedOff = false;
            return ++generation;
        }
        void leftCaller() { if (pending) handedOff = true; }
        boolean returnedToCaller() {
            boolean returning = pending && handedOff;
            if (returning) cancel();
            return returning;
        }
        void cancel() { generation++; pending = false; handedOff = false; }
        boolean take(long token, long now, boolean sameConnection) {
            if (!pending || token != generation) return false;
            pending = false;
            return sameConnection && now >= started && now - started <= MAX_AGE_MS;
        }
    }

    void stop() {
        cancelPreviewSearch();
        boolean unsentRoute = preparing != null;
        cancelPreparedRoute();
        pending = null;
        if (unsentRoute) {
            cancelTrackedTrip("길안내를 시작하기 전 요청을 취소했어요.");
            return;
        }
        if (active == null) {
            emit("status", "error", false, recoveryCandidate,
                "현재 내비의 목적지와 이 이동이 일치하는지 확인한 뒤 취소할 수 있어요.", "pleos", false);
            queryDestination();
            return;
        }
        if (!connected || helper == null) {
            emit("status", "error", false, active, "순정 내비 연결을 확인하고 있어요. 연결 후 이동 취소를 다시 눌러 주세요.", "pleos", false);
            if (!connecting) initialize();
            return;
        }
        try { cancelRequested = true; helper.cancelRoute(); }
        catch (Exception error) { emit("status", "error", false, active, "길안내 취소 요청에 실패했어요.", "pleos", false); }
    }

    private void queryDestination() {
        if (!connected || helper == null) return;
        try {
            if (probeOutstandingSince == 0) probeOutstandingSince = SystemClock.elapsedRealtime();
            if (active != null || recoveryCandidate != null) helper.getDestinationInfo();
            helper.getRouteStateInfo();
        } catch (Exception error) {
            reconnect("순정 내비 상태 조회에 실패해 연결을 다시 확인하고 있어요.");
        }
    }

    private void preloadKnownPlaces(NaviHelper candidate) {
        // Optional public metadata lookups, once for this helper connection.
        // Unsupported history/bookmark access must not fail route ownership.
        if (knownPlacesRequestedGeneration == helperGeneration) return;
        knownPlacesRequestedGeneration = helperGeneration;
        try { candidate.getRecentDestinationInfo(); }
        catch (Exception unavailable) { Log.i("CabinNavigation", "Recent-place metadata unavailable"); }
        try { candidate.getBookmarkInfo(); }
        catch (Exception unavailable) { Log.i("CabinNavigation", "Bookmark metadata unavailable"); }
    }

    private static <T> List<T> boundedPlaces(List<T> places) {
        if (places == null) return new ArrayList<>();
        return new ArrayList<>(places.subList(0, Math.min(64, places.size())));
    }

    private void resetIdleEvidence() {
        idleSince = 0;
        idleRouteAt = 0;
        idleSamples = 0;
        idleRoute = false;
        destinationEmpty = false;
        emptyDestinationAt = 0;
        emptyDestinationSince = 0;
    }

    private void reconnect(String message) {
        cancelPreviewSearch();
        nativeRouteMode = null;
        // A service outage says nothing about arrival or cancellation. Keep route ownership.
        Log.w("CabinNavigation", "reconnecting generation=" + helperGeneration);
        connected = false;
        connecting = false;
        helperGeneration++;
        probeOutstandingSince = 0;
        resetIdleEvidence();
        resetRecoveryEvidence();
        resetGuidanceEvidence();
        NaviHelper previous = helper;
        NaviHelperEventListener previousEvents = events;
        helper = null;
        events = null;
        if (previous != null) {
            try { if (previousEvents != null) previous.removeListener(previousEvents); previous.release(); }
            catch (Exception ignored) { }
        }
        emit("status", "error", false, active, message, "pleos", false);
        initialize();
    }

    private void reconcileIdleRoute() {
        long now = SystemClock.elapsedRealtime();
        if (!idleRouteConfirmed(now)) return;
        Log.i("CabinNavigation", "reconciled explicit SAFE_GUIDING + empty destination");
        cancelTrackedTrip("순정 내비의 현재 경로에서 길안내 종료를 확인했어요.");
    }

    private boolean idleRouteConfirmed(long now) {
        // Startup can temporarily report SAFE_GUIDING before restoring a route. Require fresh,
        // repeated empty-route observations after the connection has settled; never infer arrival.
        return active != null && confirmed && !arrived && connected && idleRoute && destinationEmpty
            && idleSamples >= 2 && now - idleSince >= IDLE_CONFIRM_MS
            && now - emptyDestinationSince >= IDLE_CONFIRM_MS && now - connectedAt >= IDLE_CONFIRM_MS
            && now - idleRouteAt <= 2000 && now - emptyDestinationAt <= 2000;
    }

    private void confirmRoute() {
        // onRouteStarted reports route-search initiation, not confirmed guidance.
        // Require fresh route state and destination identity before recording a start.
        if (active == null || preparing != null || confirmed || arrived || !guidanceConfirmed(connected, routeSignal,
                routeGuiding, routeStateAt, destinationMatched, destinationAt, SystemClock.elapsedRealtime())) return;
        confirmed = true;
        cancelDeadline();
        JSONObject event = event("start", "guiding", true, active, "순정 내비에서 길안내가 시작됐어요.", "pleos", false);
        enqueueBackend("start", active, "pleos", event);
        publish(event);
    }

    private void resetGuidanceEvidence() {
        routeGuiding = false;
        routeStateAt = 0;
        destinationAt = 0;
        observedRouteStartName = "";
        observedRouteStartAt = 0;
    }

    private void refreshRouteStartMatch(Place place) {
        if (place == null || observedRouteStartAt == 0
                || SystemClock.elapsedRealtime() - observedRouteStartAt > 30000) return;
        NativePoiCache.Ref poi = nativePois.get(place.name, place.address, place.latitude, place.longitude);
        if (routeStartNameMatches(place.name, poi, observedRouteStartName)) routeSignal = true;
    }

    static boolean routeStartNameMatches(String externalName, NativePoiCache.Ref poi, String observedName) {
        String expected = NavigationIdentity.normalized(poi == null ? externalName : poi.nativeName);
        return !expected.isEmpty() && expected.equals(NavigationIdentity.normalized(observedName));
    }

    static boolean nativeDestinationMatches(NativePoiCache.Ref poi, double expectedLatitude, double expectedLongitude,
            String expectedName, double latitude, double longitude, String name, String address) {
        // Keep the original candidate's distance guard even after native matching.
        if (!NavigationIdentity.nearby(expectedLatitude, expectedLongitude, latitude, longitude)) return false;
        return poi == null
            ? NavigationIdentity.sameArrival(expectedName, "", name, address)
            : NavigationIdentity.sameDestination(poi.latitude, poi.longitude, poi.nativeName, poi.address,
                latitude, longitude, name, address);
    }

    private boolean nativeDestinationMatches(Place place, DestinationInfo info) {
        NativePoiCache.Ref poi = nativePois.get(place.name, place.address, place.latitude, place.longitude);
        return nativeDestinationMatches(poi, place.latitude, place.longitude, place.name,
            info.getLatitude(), info.getLongitude(), info.getName(), info.getAddress());
    }

    static boolean isRouteGuiding(RouteStateInfo info) {
        return info != null && info.getTermsAgreement() && info.getNaviMode() == NaviMode.ROUTE_GUIDING
            && !info.isRouting();
    }

    static boolean guidanceConfirmed(boolean connected, boolean routeStarted, boolean guiding, long routeAt,
            boolean destinationMatched, long destinationAt, long now) {
        return routeStarted && NavigationRecovery.freshEvidence(connected, guiding, routeAt,
            destinationMatched, destinationAt, now);
    }

    static String routeErrorMessage(NaviErrorCode code) {
        if (code == NaviErrorCode.CAUSE_LOW_BATTERY) return "차량 배터리가 부족해요. 차량 내비에서 충전 계획을 확인해 주세요.";
        if (code == NaviErrorCode.CAUSE_SEARCH_ROUTE_OVER_DISTANCE) return "목적지가 경로를 찾을 수 있는 범위보다 멀어요. 다른 장소를 선택해 주세요.";
        if (code == NaviErrorCode.CAUSE_SEARCH_ROUTE_TOO_SHORT_DISTANCE) return "현재 위치와 목적지가 너무 가까워 경로를 찾을 수 없어요.";
        if (code == NaviErrorCode.CAUSE_SEARCH_ROUTE) return "경로를 찾지 못했어요. 차량 내비의 연결 상태를 확인한 뒤 다시 시도해 주세요.";
        return "차량 내비 요청을 처리하지 못했어요. 내비 화면을 확인한 뒤 다시 시도해 주세요.";
    }

    private void resetRecoveryEvidence() {
        recoveryRouteGuiding = false;
        recoveryRouteAt = 0;
        recoveryDestinationMatched = false;
        recoveryDestinationAt = 0;
        recoveryName = "";
        recoveryAddress = "";
    }

    private void invalidateRecovery() {
        ownershipGeneration++;
        recoveryEligible = false;
        recoveryCandidate = null;
        resetRecoveryEvidence();
        if (recoveryRetry != null) main.removeCallbacks(recoveryRetry);
        recoveryRetry = null;
    }

    private boolean hasTerminalDelivery(String tripId) {
        for (int i = 0; i < outbox.length(); i++) {
            JSONObject item = outbox.optJSONObject(i);
            if (item == null) continue;
            JSONObject body = item.optJSONObject("body");
            if (body != null && tripId.equals(body.optString("trip_id"))
                    && ("arrive".equals(item.optString("endpoint")) || "cancel".equals(item.optString("endpoint")))) return true;
        }
        return false;
    }

    private void recoverJourney() {
        if (closed || !recoveryEligible || recoveryFetching || recoveryAttempts >= 3
                || active != null || pending != null || recoveryCandidate != null) return;
        recoveryFetching = true;
        recoveryAttempts++;
        final long generation = ownershipGeneration;
        network.execute(() -> {
            Place candidate = null;
            boolean failed = false;
            try {
                HttpURLConnection connection = (HttpURLConnection)new URL("http://127.0.0.1:8765/api/state").openConnection();
                try {
                    connection.setConnectTimeout(4000);
                    connection.setReadTimeout(8000);
                    if (connection.getResponseCode() != 200) throw new java.io.IOException("State unavailable");
                    ByteArrayOutputStream bytes = new ByteArrayOutputStream();
                    try (InputStream input = connection.getInputStream()) {
                        byte[] buffer = new byte[8192];
                        int count;
                        while ((count = input.read(buffer)) != -1) {
                            if (bytes.size() + count > 2 * 1024 * 1024) throw new java.io.IOException("State too large");
                            bytes.write(buffer, 0, count);
                        }
                    }
                    JSONObject trip = new JSONObject(new String(bytes.toByteArray(), StandardCharsets.UTF_8)).optJSONObject("trip");
                    if (trip != null && NavigationRecovery.recoverableTrip(trip.optString("status"), trip.optString("provider"))) {
                        trip.put("trip_id", trip.optString("id"));
                        Place stored = new Place(trip);
                        if (!stored.tripId.isEmpty()) candidate = stored;
                    }
                } finally { connection.disconnect(); }
            } catch (Exception error) { failed = true; }
            final Place result = candidate;
            final boolean retry = failed;
            main.post(() -> {
                recoveryFetching = false;
                if (closed || !NavigationRecovery.candidateStillCurrent(generation, ownershipGeneration,
                        recoveryEligible, active != null, pending != null)) return;
                if (retry) {
                    if (recoveryAttempts < 3) {
                        recoveryRetry = this::recoverJourney;
                        main.postDelayed(recoveryRetry, 15000L * recoveryAttempts);
                    }
                    return;
                }
                if (result == null || trackingLease.isFinished(result.tripId) || hasTerminalDelivery(result.tripId)) {
                    recoveryEligible = false;
                    return;
                }
                recoveryCandidate = result;
                resetRecoveryEvidence();
                emit("status", "recovering", true, result,
                    "이전 이동과 현재 순정 내비의 목적지를 확인하고 있어요.", "pleos", false);
                queryDestination();
            });
        });
    }

    private void confirmRecoveredJourney() {
        if (recoveryCandidate == null || active != null || pending != null || !recoveryEligible
                || trackingLease.isFinished(recoveryCandidate.tripId)
                || hasTerminalDelivery(recoveryCandidate.tripId)
                || !NavigationRecovery.freshEvidence(connected, recoveryRouteGuiding, recoveryRouteAt,
                    recoveryDestinationMatched, recoveryDestinationAt, SystemClock.elapsedRealtime())) return;
        active = recoveryCandidate;
        recoveryCandidate = null;
        recoveryEligible = false;
        confirmed = true;
        arrived = false;
        routeSignal = true;
        destinationMatched = true;
        confirmedName = recoveryName;
        confirmedAddress = recoveryAddress;
        resetIdleEvidence();
        resetRecoveryEvidence();
        resumeVisibleTracking();
        // The server already recorded a confirmed start. Do not replay a start or infer an arrival.
        emit("status", "guiding", true, active, "현재 순정 내비와 이전 이동이 일치해 안내 연결을 복원했어요.", "pleos", true);
        Log.i("CabinNavigation", "Recovered tracked route after fresh SDK identity and route-state match");
    }

    private NaviHelperEventListener createEvents(final long generation) {
        return new NaviHelperEventListener() {
        @Override public void onRouteStarted(RouteStartInfo info) {
            main.post(() -> {
                if (closed || generation != helperGeneration) return;
                Log.d("CabinNavigation", "callback=onRouteStarted name=" + info.getName());
                if (recoveryCandidate != null) { resetRecoveryEvidence(); queryDestination(); }
                if (active == null || preparing != null || arrived) return;
                resetIdleEvidence();
                // This correlates the search with our request; route-state evidence is still required.
                observedRouteStartName = info.getName();
                observedRouteStartAt = SystemClock.elapsedRealtime();
                refreshRouteStartMatch(active);
                queryDestination();
                confirmRoute();
            });
        }
        @Override public void onRoutingCompleted() {
            main.post(() -> {
                if (closed || generation != helperGeneration) return;
                Log.d("CabinNavigation", "callback=onRoutingCompleted");
                queryDestination();
            });
        }
        @Override public void onNavigationStatus(NavigationStatus status) {
            main.post(() -> {
                if (closed || generation != helperGeneration) return;
                if (status == NavigationStatus.INACTIVE || status == NavigationStatus.INITIALIZING) resetRecoveryEvidence();
                if (status == NavigationStatus.INACTIVE || status == NavigationStatus.INITIALIZING) resetGuidanceEvidence();
                if (status == NavigationStatus.INACTIVE || status == NavigationStatus.INITIALIZING
                        || status == NavigationStatus.ROUTE_DRIVE) resetIdleEvidence();
                if (active != null && (status == NavigationStatus.ROUTE_DRIVE || status == NavigationStatus.FREE_DRIVE)) queryDestination();
            });
        }
        @Override public void onRouteStateInfo(RouteStateInfo info) {
            main.post(() -> {
                if (closed || generation != helperGeneration || info == null) return;
                probeOutstandingSince = 0;
                if (responseRecovery.receivedResponse()) {
                    emit("status", "ready", true, active, "순정 내비의 상태 연결이 복구됐어요.", "pleos", false);
                }
                nativeRouteMode = info.getTermsAgreement() && !info.isRouting() ? info.getNaviMode() : null;
                nativeRouteModeAt = SystemClock.elapsedRealtime();
                Log.d("CabinNavigation", "callback=onRouteStateInfo mode=" + info.getNaviMode() + " routing=" + info.isRouting());
                if (startPreparedRoute(info)) return;
                if (recoveryCandidate != null) {
                    recoveryRouteGuiding = isRouteGuiding(info);
                    recoveryRouteAt = SystemClock.elapsedRealtime();
                    confirmRecoveredJourney();
                }
                if (active != null && !confirmed && !arrived) {
                    routeGuiding = isRouteGuiding(info);
                    routeStateAt = SystemClock.elapsedRealtime();
                    confirmRoute();
                }
                if (active != null && confirmed && !arrived && info.getTermsAgreement()
                        && info.getNaviMode() == NaviMode.SAFE_GUIDING && !info.isRouting()) {
                    if (idleSince == 0) idleSince = SystemClock.elapsedRealtime();
                    idleRoute = true;
                    idleRouteAt = SystemClock.elapsedRealtime();
                    idleSamples++;
                    reconcileIdleRoute();
                } else {
                    resetIdleEvidence();
                }
                if (active != null && !info.getTermsAgreement()) {
                    emit("status", "routing", true, active, "순정 내비 화면에서 이용 안내를 확인해 주세요.", "pleos", false);
                }
            });
        }
        @Override public void onDestinationChanged() {
            main.post(() -> {
                if (closed || generation != helperGeneration) return;
                if (recoveryCandidate != null) resetRecoveryEvidence();
                queryDestination();
            });
        }
        @Override public void onDestinationInfo(DestinationInfo info) {
            main.post(() -> {
                if (closed || generation != helperGeneration) return;
                if (info == null) return;
                nativePois.putNative(info.getName(), info.getLatitude(), info.getLongitude(),
                    info.getPoiId(), info.getPoiProvider(), info.getAddress(), "destination");
                publishSelectedNativeMatch();
                if (recoveryCandidate != null) {
                    recoveryDestinationMatched = nativeDestinationMatches(recoveryCandidate, info);
                    recoveryDestinationAt = SystemClock.elapsedRealtime();
                    recoveryName = info.getName();
                    recoveryAddress = info.getAddress();
                    confirmRecoveredJourney();
                }
                if (active == null || arrived) return;
                if (info.getLatitude() == 0 && info.getLongitude() == 0
                        && NavigationIdentity.normalized(info.getName()).isEmpty()) {
                    if (!confirmed) { destinationMatched = false; destinationAt = 0; }
                    long now = SystemClock.elapsedRealtime();
                    if (!destinationEmpty) emptyDestinationSince = now;
                    destinationEmpty = true;
                    emptyDestinationAt = now;
                    reconcileIdleRoute();
                    return;
                }
                destinationEmpty = false;
                emptyDestinationAt = 0;
                emptyDestinationSince = 0;
                if (!NavigationIdentity.validCoordinates(info.getLatitude(), info.getLongitude())) {
                    if (!confirmed) { destinationMatched = false; destinationAt = 0; }
                    return;
                }
                if (NavigationIdentity.normalized(info.getName()).isEmpty()
                        && NavigationIdentity.normalized(info.getAddress()).isEmpty()) {
                    // Missing metadata is not evidence of a different POI and must not erase
                    // the last confirmed identity. A pending route still needs positive identity.
                    if (!confirmed) { destinationMatched = false; destinationAt = 0; }
                    return;
                }
                // Confirmed route identity remains frozen; a new cache entry must
                // never redefine ownership of a different native destination.
                boolean matches = confirmed
                    ? NavigationIdentity.sameDestination(active.latitude, active.longitude, confirmedName, confirmedAddress,
                        info.getLatitude(), info.getLongitude(), info.getName(), info.getAddress())
                    : nativeDestinationMatches(active, info);
                Log.d("CabinNavigation", "callback=onDestinationInfo coordinateMatch="
                    + NavigationIdentity.nearby(active.latitude, active.longitude, info.getLatitude(), info.getLongitude())
                    + " identityMatch=" + matches + " name=" + info.getName());
                if (matches) {
                    destinationMatched = true;
                    destinationAt = SystemClock.elapsedRealtime();
                    if (!NavigationIdentity.normalized(info.getName()).isEmpty()) confirmedName = info.getName();
                    if (!NavigationIdentity.normalized(info.getAddress()).isEmpty()) confirmedAddress = info.getAddress();
                    // A first-time native selection may report its canonical
                    // RouteStarted name before DestinationInfo proves that match.
                    refreshRouteStartMatch(active);
                    confirmRoute();
                } else if (info.getLatitude() != 0 && info.getLongitude() != 0) {
                    if (confirmed) {
                        cancelTrackedTrip("순정 내비의 목적지가 변경되어 이 이동 기록을 취소했어요.");
                    } else {
                        destinationMatched = false;
                        destinationAt = 0;
                        confirmedName = "";
                        confirmedAddress = "";
                    }
                }
            });
        }
        @Override public void onRecentDestinationInfo(List<RecentDestinationInfo> places) {
            final List<RecentDestinationInfo> bounded = boundedPlaces(places);
            main.post(() -> {
                if (closed || generation != helperGeneration) return;
                for (RecentDestinationInfo place : bounded) {
                    if (place == null) continue;
                    nativePois.putNative(place.getName(), place.getLatitude(), place.getLongitude(),
                        place.getPoiId(), place.getPoiProvider(), place.getAddress(), "recent_destination");
                }
                publishSelectedNativeMatch();
            });
        }
        @Override public void onBookmarkInfo(List<BookmarkInfo> places) {
            final List<BookmarkInfo> bounded = boundedPlaces(places);
            main.post(() -> {
                if (closed || generation != helperGeneration) return;
                for (BookmarkInfo place : bounded) {
                    if (place == null) continue;
                    nativePois.putNative(place.getName(), place.getLatitude(), place.getLongitude(),
                        place.getPoiId(), place.getPoiProvider(), place.getAddress(), "bookmark");
                }
                publishSelectedNativeMatch();
            });
        }
        @Override public void onDestinationArrived(DestinationArrivedInfo info) {
            main.post(() -> {
                if (closed || generation != helperGeneration) return;
                Log.d("CabinNavigation", "callback=onDestinationArrived name=" + info.getName());
                if (active == null || !confirmed || arrived) return;
                if (!NavigationIdentity.sameArrival(confirmedName, confirmedAddress, info.getName(), info.getAddress())) {
                    emit("status", "arrival_unmatched", false, active,
                        "내비 도착 알림의 장소가 선택한 목적지와 일치하지 않아 저장을 요청하지 않았어요.", "pleos", false);
                    return;
                }
                arrived = true;
                cancelDeadline();
                JSONObject event = event("arrived", "arrived", true, active, "목적지에 도착했어요.", "pleos_arrival", false);
                enqueueBackend("arrive", active, "pleos_arrival", event);
                publish(event);
                stopBackgroundTracking();
            });
        }
        @Override public void onRouteCancelled() {
            main.post(() -> {
                if (closed || generation != helperGeneration) return;
                Log.d("CabinNavigation", "callback=onRouteCancelled confirmed=" + confirmed);
                resetRecoveryEvidence();
                if (active == null || arrived) return;
                // Replacing a previous route can cancel it while the new request is still pending.
                if (!confirmed && !cancelRequested) return;
                cancelTrackedTrip("순정 내비 길안내가 취소됐어요.");
            });
        }
        @Override public void onError(NaviErrorCode code) {
            main.post(() -> {
                if (closed || generation != helperGeneration) return;
                if (active == null || arrived) return;
                cancelPreparedRoute();
                pending = null;
                cancelDeadline();
                // A routing error must not erase an earlier, positively confirmed route.
                if (!confirmed) {
                    destinationMatched = false;
                    routeSignal = false;
                    resetGuidanceEvidence();
                }
                resetIdleEvidence();
                emit(confirmed ? "status" : "start", "error", false, active, routeErrorMessage(code), "pleos", false);
                if (!confirmed) abandonUnconfirmedRoute(active);
            });
        }
        };
    }

    private void cancelTrackedTrip(String message) {
        if (active == null || arrived) return;
        cancelDeadline();
        resetIdleEvidence();
        confirmed = false;
        destinationMatched = false;
        routeSignal = false;
        resetGuidanceEvidence();
        JSONObject event = event("cancelled", "cancelled", true, active, message, "pleos_cancelled", false);
        enqueueBackend("cancel", active, "pleos_cancelled", event);
        publish(event);
        active = null;
        cancelRequested = false;
        stopBackgroundTracking();
    }

    private JSONObject event(String action, String status, boolean success, Place place, String message, String source, boolean synced) {
        JSONObject value = new JSONObject();
        try {
            value.put("action", action).put("status", status).put("success", success)
                .put("available", connected).put("connected", connected)
                .put("provider", "pleos").put("source", source).put("backendSynced", synced)
                .put("timestamp", System.currentTimeMillis()).put("message", message);
            value.put("trip_id", place == null ? "" : place.tripId).put("tripId", place == null ? "" : place.tripId);
            if (place != null) {
                value.put("place", place.json);
                putNativeMatch(value, place);
            }
            if (!success && !"connecting".equals(status)) value.put("error", message);
        } catch (JSONException impossible) { }
        return value;
    }

    private void putNativeMatch(JSONObject value, Place place) throws JSONException {
        NativePoiCache.Ref poi = nativePois.get(place.name, place.address, place.latitude, place.longitude);
        value.put("native_poi_available", poi != null);
        value.put("preview_mode", poi == null ? "native_search" : "native_detail");
        if (poi == null) { value.remove("native_place"); return; }
        // Export only the single matched selection, never history/bookmark lists.
        value.put("native_place", new JSONObject().put("name", poi.nativeName)
            .put("address", poi.address).put("latitude", poi.latitude).put("longitude", poi.longitude)
            .put("source", poi.source));
    }

    private void publishSelectedNativeMatch() {
        try {
            JSONObject current = new JSONObject(snapshot);
            if (current.optJSONObject("place") == null) return;
            Place selected = new Place(current);
            String before = current.optString("native_place", "");
            boolean availableBefore = current.optBoolean("native_poi_available");
            putNativeMatch(current, selected);
            if (availableBefore != current.optBoolean("native_poi_available")
                    || !before.equals(current.optString("native_place", ""))) publish(current);
        } catch (JSONException | IllegalArgumentException ignored) { }
    }

    private void emit(String action, String status, boolean success, Place place, String message, String source, boolean synced) {
        publish(event(action, status, success, place, message, source, synced));
    }

    private void publish(JSONObject value) {
        snapshot = value.toString();
        Log.d("CabinNavigation", snapshot);
        publishState();
    }

    private void enqueueBackend(String endpoint, Place place, String source, JSONObject event) {
        try {
            JSONObject body = new JSONObject().put("trip_id", place.tripId);
            body.put("start".equals(endpoint) ? "provider" : "source", source);
            outbox.put(new JSONObject().put("endpoint", endpoint).put("body", body).put("event", event));
            preferences.edit().putString("outbox", outbox.toString()).commit();
            flushOutbox();
        } catch (JSONException impossible) { }
    }

    private void flushOutbox() {
        if (closed || syncing || outbox.length() == 0) return;
        syncing = true;
        JSONObject item = outbox.optJSONObject(0);
        network.execute(() -> {
            String failure = null;
            boolean terminal = false;
            try {
                HttpURLConnection connection = (HttpURLConnection)new URL("http://127.0.0.1:8765/api/trip/" + item.getString("endpoint")).openConnection();
                try {
                    connection.setRequestMethod("POST");
                    connection.setConnectTimeout(4000);
                    connection.setReadTimeout(8000);
                    connection.setDoOutput(true);
                    connection.setRequestProperty("Content-Type", "application/json; charset=utf-8");
                    byte[] bytes = item.getJSONObject("body").toString().getBytes(StandardCharsets.UTF_8);
                    connection.setFixedLengthStreamingMode(bytes.length);
                    try (OutputStream output = connection.getOutputStream()) { output.write(bytes); }
                    int status = connection.getResponseCode();
                    if (status < 200 || status >= 300) {
                        terminal = status >= 400 && status < 500 && status != 408 && status != 425 && status != 429;
                        throw new java.io.IOException("HTTP " + status);
                    }
                } finally { connection.disconnect(); }
            } catch (Exception error) { failure = error.getMessage(); }
            final String resultError = failure;
            final boolean rejected = terminal;
            main.post(() -> {
                syncing = false;
                if (resultError == null || rejected) {
                    outbox.remove(0);
                    preferences.edit().putString("outbox", outbox.toString()).commit();
                    JSONObject event = item.optJSONObject("event");
                    try {
                        if (rejected) {
                            JSONArray failures;
                            try { failures = new JSONArray(preferences.getString("failedSync", "[]")); }
                            catch (JSONException malformed) { failures = new JSONArray(); }
                            item.put("error", resultError);
                            failures.put(item);
                            while (failures.length() > 20) failures.remove(0);
                            preferences.edit().putString("failedSync", failures.toString()).commit();
                            Log.w("CabinNavigation", "Backend rejected stored transition: " + resultError);
                            if (isCurrent(event)) {
                                JSONObject notice = new JSONObject(event.toString());
                                notice.put("action", "status").put("backendSynced", false)
                                    .put("syncError", "이전 이동 기록과 맞지 않아 서버 반영이 거절됐어요. " + resultError);
                                if (!closed) publish(notice);
                            }
                        } else if (event != null && isCurrent(event)) {
                            event.put("backendSynced", true).put("available", connected).put("connected", connected);
                            if (!closed) publish(event);
                        }
                    } catch (JSONException ignored) { }
                    flushOutbox();
                } else {
                    JSONObject event = item.optJSONObject("event");
                    if (event != null && isCurrent(event)) {
                        try {
                            JSONObject notice = new JSONObject(event.toString());
                            notice.put("backendSynced", false).put("syncError", "이동 기록 서버 연결을 다시 시도하고 있어요.");
                            if (!closed) publish(notice);
                        } catch (JSONException ignored) { }
                    }
                    if (!closed) main.postDelayed(this::flushOutbox, 5000);
                }
            });
        });
    }

    private boolean isCurrent(JSONObject event) {
        if (event == null) return false;
        try {
            JSONObject current = new JSONObject(snapshot);
            String id = event.optString("trip_id");
            if (!id.equals(current.optString("trip_id"))) return false;
            // A delayed start acknowledgement must not replace a later arrival/cancellation.
            return !("guiding".equals(event.optString("status"))
                && ("arrived".equals(current.optString("status")) || "cancelled".equals(current.optString("status"))));
        } catch (JSONException invalid) { return false; }
    }

    private void cancelDeadline() {
        if (routeDeadline != null) main.removeCallbacks(routeDeadline);
        routeDeadline = null;
    }

    void close() {
        cancelPreviewSearch();
        cancelPreparedRoute();
        pending = null;
        stopBackgroundTracking();
        closed = true;
        helperGeneration++;
        cancelDeadline();
        main.removeCallbacksAndMessages(null);
        if (helper != null) {
            try { helper.removeListener(events); helper.release(); } catch (Exception ignored) { }
        }
        network.shutdown();
    }

    private static final class Place {
        final JSONObject json;
        final String tripId;
        final String name;
        final String address;
        final String region;
        final double latitude;
        final double longitude;
        Place(JSONObject input) throws JSONException {
            json = input.optJSONObject("place") == null ? new JSONObject(input.toString()) : new JSONObject(input.getJSONObject("place").toString());
            tripId = input.optString("trip_id", input.optString("tripId", "")).trim();
            name = json.optString("name", "").trim();
            address = NativePlaceSearch.actualAddress(json.optString("address", ""));
            region = json.optString("region", input.optString("region", "")).trim();
            latitude = json.optDouble("latitude", json.optDouble("lat", Double.NaN));
            longitude = json.optDouble("longitude", json.optDouble("lon", Double.NaN));
            if (name.isEmpty() || name.length() > 180 || tripId.length() > 160
                    || !Double.isFinite(latitude) || !Double.isFinite(longitude)
                    || latitude < -90 || latitude > 90 || longitude < -180 || longitude > 180) {
                throw new IllegalArgumentException("장소 이름과 위도·경도를 확인해 주세요.");
            }
        }
        String searchKeyword() {
            return NativePlaceSearch.keyword(name, region, address);
        }
    }
}
