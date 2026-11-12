package local.pleos.cabin;

import android.content.Context;
import android.content.pm.PackageManager;
import android.media.AudioAttributes;
import android.os.Handler;
import android.os.Looper;
import ai.pleos.playground.tts.TextToSpeech;
import ai.pleos.playground.tts.constant.ErrorType;
import ai.pleos.playground.tts.constant.Mode;
import ai.pleos.playground.tts.listener.EventListener;
import ai.pleos.playground.tts.listener.OnInitializeListener;
import java.util.Locale;
import org.json.JSONObject;

/** Explicit recommendation playback through the public, local Pleos TTS SDK. */
final class NativeSpeaker {
    interface Listener { void onSpeakerEvent(String json); }
    private final Handler main = new Handler(Looper.getMainLooper());
    private final Listener listener;
    private final RequestGate requests = new RequestGate();
    private TextToSpeech helper;
    private EventListener events;
    private volatile boolean ready, closed;
    private boolean initializing;
    private volatile String snapshot = "{\"available\":false,\"state\":\"connecting\"}";
    private Runnable initializeDeadline;
    private Runnable playbackDeadline;

    NativeSpeaker(Context context, Listener listener) {
        this.listener = listener;
        if (context.checkSelfPermission("pleos.car.permission.TTS_SERVICE") != PackageManager.PERMISSION_GRANTED) {
            publish("unavailable", "차량 음성 안내 권한을 확인할 수 없어요.");
            return;
        }
        try {
            final TextToSpeech candidate = new TextToSpeech(context.getApplicationContext(), Mode.ON_DEVICE);
            helper = candidate;
            initializing = true;
            initializeDeadline = () -> {
                if (initializing && !closed && helper == candidate)
                    fail("차량 음성 안내에 연결하지 못했어요.");
            };
            main.postDelayed(initializeDeadline, 10000);
            candidate.initialize(new OnInitializeListener() {
                @Override public void onSuccess() { main.post(() -> {
                    if (closed || !initializing || helper != candidate) return;
                    initializing = false;
                    cancelInitializeDeadline();
                    try {
                        candidate.setLocale(Locale.KOREA);
                        candidate.setAudioAttributes(new AudioAttributes.Builder()
                            .setUsage(AudioAttributes.USAGE_ASSISTANT)
                            .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH).build());
                        ready = true;
                        publish("ready", null);
                    } catch (Exception | LinkageError error) { fail("차량 음성 안내를 준비하지 못했어요."); }
                }); }
                @Override public void onFailure(ErrorType error) { main.post(() -> {
                    if (!closed && initializing && helper == candidate)
                        fail("차량 음성 안내에 연결하지 못했어요。 (" + error.name() + ")");
                }); }
            });
        } catch (Exception | LinkageError error) { fail("이 차량에서 음성 안내를 사용할 수 없어요."); }
    }

    String state() {
        if (closed) return "{\"available\":false,\"state\":\"unavailable\"}";
        return snapshot;
    }
    boolean active() { return requests.active(); }

    /** Serialize reservation plus Handler enqueue with stop/close across bridge threads. */
    synchronized boolean speak(String text) {
        if (!ready || closed || text == null || text.trim().isEmpty() || text.length() > 1800) return false;
        final long request = requests.begin();
        if (request == 0) return false;
        final String utterance = text.trim();
        main.post(() -> {
            if (closed || !requests.current(request) || helper == null) return;
            detachEvents();
            events = new EventListener() {
                private void event(String status, String error, boolean done) { main.post(() -> {
                    if (closed || !requests.current(request)) return;
                    if (done) {
                        if (!requests.finish(request)) return;
                        cancelPlaybackDeadline();
                        detachEvents();
                        if (error != null) stopHelper();
                    } else if ("preparing".equals(status)) {
                        if (!requests.preparing(request)) return;
                    } else if ("speaking".equals(status) && !requests.started(request)) return;
                    publish(status, error);
                }); }
                @Override public void onReady() { event("preparing", null, false); }
                @Override public void onStart() { event("speaking", null, false); }
                @Override public void onDone() { event("done", null, true); }
                @Override public void onStop() { event("stopped", null, true); }
                @Override public void onUpdatedRms(double rms) {}
                @Override public void onError(ErrorType error) { event("error", "음성 안내를 재생하지 못했어요. (" + error.name() + ")", true); }
            };
            try {
                helper.addEventListener(events);
                publish("preparing", null);
                playbackDeadline = () -> finishWithError(request, "음성 안내 응답이 늦어 재생을 멈췄어요.");
                main.postDelayed(playbackDeadline, 60000);
                helper.speak(utterance, true);
            } catch (Exception | LinkageError error) {
                finishWithError(request, "음성 안내를 재생하지 못했어요.");
            }
        });
        return true;
    }

    synchronized void stop() {
        if (closed || !requests.active()) return;
        requests.cancel();
        runOnMain(() -> {
            cancelPlaybackDeadline();
            detachEvents();
            stopHelper();
            if (!closed) publish("stopped", null);
        });
    }

    synchronized void close() {
        if (closed) return;
        closed = true;
        ready = false;
        requests.close();
        runOnMain(() -> {
            initializing = false;
            cancelInitializeDeadline();
            cancelPlaybackDeadline();
            detachEvents();
            stopHelper();
            closeHelper();
        });
    }

    /** Runs on the main queue, so a timeout cannot overwrite a newer session. */
    private void finishWithError(long request, String message) {
        if (closed || !requests.finish(request)) return;
        cancelPlaybackDeadline();
        detachEvents();
        stopHelper();
        publish("error", message);
    }

    private void fail(String message) {
        ready = false;
        initializing = false;
        requests.cancel();
        cancelInitializeDeadline();
        cancelPlaybackDeadline();
        detachEvents();
        stopHelper();
        closeHelper();
        publish("unavailable", message);
    }

    private void runOnMain(Runnable action) {
        if (Looper.myLooper() == main.getLooper()) action.run();
        else main.post(action);
    }

    private void stopHelper() {
        if (helper != null) try { helper.stop(); } catch (Exception | LinkageError ignored) {}
    }

    private void closeHelper() {
        TextToSpeech previous = helper;
        helper = null;
        if (previous != null) try { previous.release(); } catch (Exception | LinkageError ignored) {}
    }
    private void cancelInitializeDeadline() {
        if (initializeDeadline != null) main.removeCallbacks(initializeDeadline);
        initializeDeadline = null;
    }
    private void cancelPlaybackDeadline() {
        if (playbackDeadline != null) main.removeCallbacks(playbackDeadline);
        playbackDeadline = null;
    }
    private void detachEvents() {
        if (helper != null && events != null) try { helper.removeEventListener(events); } catch (Exception | LinkageError ignored) {}
        events = null;
    }
    private void publish(String status, String error) {
        if (closed) return;
        try {
            JSONObject json = new JSONObject().put("available", ready).put("state", status);
            if (error != null) json.put("error", error);
            snapshot = json.toString();
            listener.onSpeakerEvent(snapshot);
        } catch (Exception ignored) {}
    }

    /** Real callback gate, kept Android-free so lifecycle ordering is regression tested. */
    static final class RequestGate {
        private long generation;
        private boolean active, started, closed;
        synchronized long begin() {
            if (closed || active) return 0;
            active = true;
            started = false;
            return ++generation;
        }
        synchronized boolean current(long token) {
            return !closed && active && token == generation;
        }
        synchronized boolean active() { return active; }
        synchronized boolean preparing(long token) { return current(token) && !started; }
        synchronized boolean started(long token) {
            if (!current(token)) return false;
            started = true;
            return true;
        }
        synchronized boolean finish(long token) {
            if (!current(token)) return false;
            cancel();
            return true;
        }
        synchronized void cancel() { active = false; started = false; generation++; }
        synchronized void close() { closed = true; cancel(); }
    }
}
