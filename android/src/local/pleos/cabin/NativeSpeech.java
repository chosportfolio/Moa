package local.pleos.cabin;

import android.content.Context;
import android.content.pm.PackageManager;
import android.os.Handler;
import android.os.Looper;
import android.os.SystemClock;
import ai.pleos.playground.stt.SpeechToText;
import ai.pleos.playground.stt.constant.ErrorType;
import ai.pleos.playground.stt.constant.Mode;
import ai.pleos.playground.stt.listener.OnInitializeListener;
import ai.pleos.playground.stt.listener.ResultListener;
import java.util.Locale;
import java.util.UUID;
import java.util.LinkedHashMap;
import java.util.ArrayList;
import java.time.Instant;
import org.json.JSONException;
import org.json.JSONObject;

/** Foreground-service PCM capture into the public Pleos SDK; no audio files or uploads. */
final class NativeSpeech {
    interface Listener { void onSpeechEvent(String json); }
    private static final long SESSION_LIMIT_MS = 180000;
    private static final long FINAL_RESULT_GRACE_MS = 2000;
    private static final long STOP_DRAIN_LIMIT_MS = 8000;
    private static final int MAX_PENDING_RESULTS = 64;
    // Process-only retention survives Activity recreation. No cabin audio or
    // transcript is written to a second store before the server acknowledges it.
    private static final LinkedHashMap<String, String> pendingResults = new LinkedHashMap<>();
    private final Context context;
    private final Listener listener;
    private final Handler main = new Handler(Looper.getMainLooper());
    private final SpeechResultLedger ledger = new SpeechResultLedger();
    // The PCM API does not emit readiness/end callbacks for a silent stream.
    // Keep that stream open until actual SDK end or the bounded user session ends.
    private final SpeechPcmStream pcmStream = new SpeechPcmStream(Integer.MAX_VALUE);
    private final SpeechPcmQueue pcmQueue = new SpeechPcmQueue(16000 * 2 * 4);
    private CabinPcmCapture capture;
    private boolean microphonePaused;
    private boolean speechObserved;
    private SpeechToText helper;
    private ResultListener resultListener;
    private volatile boolean ready;
    private volatile boolean recording;
    private volatile boolean stopping;
    private volatile boolean closed;
    private String conversationId = "";
    private String sessionId = "";
    private String recordingStartedAt = "";
    private String utteranceRecognizedAt = "";
    private long sessionStarted;
    private long utteranceStarted;
    private long initGeneration;
    private long lastRmsAt;
    private volatile String snapshot = "{\"type\":\"status\",\"status\":\"connecting\",\"source\":\"pleos_on_device\",\"available\":false,\"recording\":false}";
    private Runnable initializeDeadline;
    private Runnable sessionDeadline;
    private Runnable stopDeadline;
    private Runnable nextRequest;
    private Runnable finalResultDeadline;

    NativeSpeech(Context context, Listener listener) {
        this.context = context.getApplicationContext();
        this.listener = listener;
        initialize();
    }

    boolean available() { return ready && !closed; }
    boolean active() { return recording || stopping; }
    String state() {
        try {
            JSONObject current = new JSONObject(snapshot);
            current.put("pending_results", hasPendingResults());
            return current.toString();
        } catch (JSONException impossible) { return snapshot; }
    }
    boolean hasPendingResults() { synchronized (pendingResults) { return !pendingResults.isEmpty(); } }

    void acknowledge(String session, int sequence) {
        if (session == null || sequence <= 0) return;
        synchronized (pendingResults) { pendingResults.remove(session + ":" + sequence); }
    }

    void publishState() {
        if (closed) return;
        listener.onSpeechEvent(state());
        ArrayList<String> pending;
        synchronized (pendingResults) { pending = new ArrayList<>(pendingResults.values()); }
        for (String result : pending) listener.onSpeechEvent(result);
    }

    private void initialize() {
        if (context.checkSelfPermission("pleos.car.permission.STT_SERVICE") != PackageManager.PERMISSION_GRANTED) {
            fail("PERMISSION_DENIED", "차량 음성 서비스 권한이 없어 기본 음성 인식을 사용해요.");
            return;
        }
        final long generation = ++initGeneration;
        try {
            helper = new SpeechToText(context, Mode.ON_DEVICE);
            initializeDeadline = () -> {
                if (!closed && !ready && generation == initGeneration)
                    fail("TIMEOUT", "차량 음성 서비스에 연결하지 못했어요. 기본 음성 인식을 사용할 수 있어요.");
            };
            main.postDelayed(initializeDeadline, 10000);
            helper.initialize(new OnInitializeListener() {
                @Override public void onSuccess() {
                    main.post(() -> {
                        if (closed || generation != initGeneration) return;
                        main.removeCallbacks(initializeDeadline);
                        try {
                            helper.setLocale(Locale.KOREA);
                            ready = true;
                            emit("status", "ready", null, 0, 0, 0, null, null, 0);
                        } catch (Exception | LinkageError error) {
                            fail("INITIALIZATION_ERROR", "차량 한국어 음성 인식을 준비하지 못했어요.");
                        }
                    });
                }
                @Override public void onFailure(ErrorType error) {
                    main.post(() -> {
                        if (!closed && generation == initGeneration) fail(error.name(), errorMessage(error));
                    });
                }
            });
        } catch (Exception | LinkageError error) {
            fail("INITIALIZATION_ERROR", "이 환경에서 차량 음성 서비스를 사용할 수 없어 기본 음성 인식을 사용해요.");
        }
    }

    /** Invoke on the main thread. False guarantees this call did not open a microphone. */
    boolean start(String identifier) {
        if (!available() || active() || hasPendingResults() || identifier == null || identifier.isEmpty() || identifier.length() > 100) return false;
        conversationId = identifier;
        sessionId = UUID.randomUUID().toString();
        sessionStarted = SystemClock.elapsedRealtime();
        recordingStartedAt = Instant.ofEpochMilli(System.currentTimeMillis()).toString();
        ledger.reset();
        recording = true;
        stopping = false;
        microphonePaused = false;
        pcmStream.clear();
        pcmQueue.clear();
        sessionDeadline = this::stop;
        main.postDelayed(sessionDeadline, SESSION_LIMIT_MS);
        emit("status", "listening", null, 0, 0, 0, null, null, 0);
        requestUtterance();
        if (recording && !closed) startCapture();
        return true;
    }

    private void requestUtterance() {
        if (nextRequest != null) { main.removeCallbacks(nextRequest); nextRequest = null; }
        if ((!recording && !(stopping && !pcmQueue.isEmpty())) || closed || !ready || microphonePaused) return;
        if (resultListener != null) return; // A resume must not replace a live stream.
        synchronized (pendingResults) {
            if (pendingResults.size() >= MAX_PENDING_RESULTS) {
                stop();
                return;
            }
        }
        removeResultListener();
        final long token = ledger.beginUtterance();
        pcmStream.begin(token);
        speechObserved = false;
        utteranceRecognizedAt = "";
        utteranceStarted = pcmQueue.isEmpty() ? SystemClock.elapsedRealtime() : pcmQueue.firstCapturedAt();
        final double start = (utteranceStarted - sessionStarted) / 1000.0;
        ResultListener next = new ResultListener() {
            @Override public void onReady() { }
            @Override public void onStartedRecognition() {
                main.post(() -> {
                    if (ledger.current(token) && !closed) {
                        speechObserved = true;
                        emit("status", recordingStatus(), null, 0, start, elapsed(), null, null, 0);
                    }
                });
            }
            @Override public void onUpdated(String text, boolean completed) {
                final String receivedAt = Instant.ofEpochMilli(System.currentTimeMillis()).toString();
                main.post(() -> {
                    if (!ledger.current(token) || closed || !active() || text == null) return;
                    if (!completed && ledger.hasFinal(token)) return;
                    String clean = text.trim();
                    if (clean.isEmpty() || clean.length() > 4000) return;
                    speechObserved = true;
                    if (utteranceRecognizedAt.isEmpty()) utteranceRecognizedAt = receivedAt;
                    int sequence = completed ? ledger.acceptFinal(token, clean) : ledger.acceptInterim(token, clean);
                    if (sequence == 0) return;
                    emit("transcript", completed ? "final" : "interim", clean, sequence, start,
                        Math.max(start + 0.01, elapsed()), null, null, 0);
                    if (completed && ledger.ended(token)) finishUtterance(token);
                });
            }
            @Override public void onUpdatedEpdData(long on, long off) { }
            @Override public void onUpdatedRms(double rms) {
                main.post(() -> {
                    long now = SystemClock.elapsedRealtime();
                    if (!ledger.current(token) || closed || !active() || !Double.isFinite(rms) || now - lastRmsAt < 150) return;
                    lastRmsAt = now;
                    // SDK RMS is signal magnitude, not a normalized level or dB.
                    emit("status", recordingStatus(), null, 0, start, elapsed(), null, null, rms);
                });
            }
            @Override public void onEndedRecognition() {
                main.post(() -> {
                    if (closed || !ledger.markEnded(token)) return;
                    try { completePcmStream(token); }
                    catch (Exception | LinkageError error) {
                        fail("AUDIO_SEND_ERROR", "차량 음성 인식 입력을 마치지 못했어요. 다시 시작해 주세요.");
                        return;
                    }
                    if (ledger.hasFinal(token)) { finishUtterance(token); return; }
                    finalResultDeadline = () -> {
                        if (!ledger.current(token) || closed) return;
                        if (ledger.hasUnfinishedText()) failMissingFinal();
                        else finishUtterance(token); // A quiet request has no text to save.
                    };
                    main.postDelayed(finalResultDeadline, FINAL_RESULT_GRACE_MS);
                });
            }
            @Override public void onError(ErrorType error) {
                main.post(() -> {
                    if (ledger.current(token) && !closed) fail(error.name(), errorMessage(error));
                });
            }
        };
        resultListener = next;
        try {
            helper.addListener(next);
            // sendAudio() opens the official PCM input path itself. Calling
            // request() here would open a second microphone in the CAAS process.
            drainPcm();
            if (stopping) completePcmStream(token);
        } catch (Exception | LinkageError error) {
            fail("REQUEST_ERROR", "차량 음성 인식을 시작하지 못했어요. 다시 시작하면 기본 음성 인식을 사용해요.");
        }
    }

    private String recordingStatus() {
        return stopping ? "stopping" : microphonePaused ? "microphone_paused" : "listening";
    }

    private void startCapture() {
        final CabinPcmCapture[] owner = new CabinPcmCapture[1];
        owner[0] = new CabinPcmCapture(main, new CabinPcmCapture.Listener() {
            @Override public void onPcm(byte[] pcm, double rms, long capturedAt) {
                if (capture != owner[0] || closed || !active()) return;
                if (!pcmQueue.add(pcm, rms, Math.max(sessionStarted, capturedAt))) {
                    fail("MICROPHONE_BACKPRESSURE", "음성 처리가 지연되어 대화 기록을 멈췄어요. 마지막 발언을 다시 말해 주세요.");
                    return;
                }
                drainPcm();
            }
            @Override public void onSilenced(boolean silenced) {
                if (capture != owner[0] || closed || !recording || stopping) return;
                microphonePaused = silenced;
                pcmStream.pause(silenced);
                if (!silenced && resultListener == null) {
                    requestUtterance();
                }
                emit("status", recordingStatus(), null, 0, 0, elapsed(), null, null, 0);
            }
            @Override public void onFailure(String code) {
                if (capture == owner[0] && !closed && active())
                    fail(code, "마이크 입력이 중단됐어요. 다른 음성 기능이 끝난 뒤 다시 시작해 주세요.");
            }
            @Override public void onStopped() {
                if (capture != owner[0] || closed) return;
                capture = null;
                if (stopping) finishStoppingInput();
                else if (recording) fail("MICROPHONE_READ_ERROR", "마이크 입력이 중단됐어요. 다시 시작해 주세요.");
            }
        });
        capture = owner[0];
        try { capture.start(); }
        catch (RuntimeException error) {
            fail("MICROPHONE_START_ERROR", "마이크를 시작하지 못했어요. 마이크 권한과 연결을 확인해 주세요.");
        }
    }

    private void drainPcm() {
        long token = pcmStream.token();
        if (!ledger.current(token) || ledger.ended(token) || !pcmStream.accepts(token) || helper == null) return;
        try {
            // Pleos supports a complete PCM buffer or multiple chunks. Replay
            // only the bounded handoff backlog, then return to live 100 ms input.
            while (!pcmQueue.isEmpty() && pcmStream.accepts(token)) {
                SpeechPcmQueue.Frame frame = pcmQueue.remove();
                helper.sendAudio(frame.pcm);
                if (pcmStream.addBytes(token, frame.pcm.length)) completePcmStream(token);
                long now = SystemClock.elapsedRealtime();
                if (now - lastRmsAt >= 150) {
                    lastRmsAt = now;
                    emit("status", recordingStatus(), null, 0,
                        (utteranceStarted - sessionStarted) / 1000.0, elapsed(), null, null, frame.rms);
                }
            }
        } catch (Exception | LinkageError error) {
            fail("AUDIO_SEND_ERROR", "차량 음성 인식에 소리를 전달하지 못했어요. 다시 시작해 주세요.");
        }
    }

    private void completePcmStream(long token) {
        if (pcmStream.end(token) && helper != null) helper.completeAudioSend();
    }

    private void stopCapture() {
        CabinPcmCapture previous = capture;
        capture = null;
        if (previous != null) previous.stop();
        microphonePaused = false;
        pcmStream.pause(false);
    }

    private void finishUtterance(long token) {
        if (!ledger.current(token) || closed) return;
        ledger.endUtterance();
        if (finalResultDeadline != null) main.removeCallbacks(finalResultDeadline);
        removeResultListener();
        if (stopping) {
            // The reader posts its final captured frame before onStopped.
            if (capture != null) return;
            if (pcmQueue.isEmpty()) finish();
            else requestUtterance();
            return;
        }
        if (!recording) { finish(); return; }
        // Re-arm only after the previous request's final result has settled.
        nextRequest = this::requestUtterance;
        main.postDelayed(nextRequest, 250);
    }

    private void failMissingFinal() {
        fail("FINAL_RESULT_TIMEOUT", "차량 음성 인식이 문장을 확정하지 못했어요. 다시 시작하면 기본 음성 인식을 사용해요.");
    }

    /** Wait briefly for the recognizer's final text; never turn an interim into a final. */
    void stop() {
        if (closed || !active() || stopping) return;
        recording = false;
        stopping = true;
        microphonePaused = false;
        pcmStream.pause(false);
        if (nextRequest != null) main.removeCallbacks(nextRequest);
        emit("status", "stopping", null, 0, 0, elapsed(), null, null, 0);
        stopDeadline = () -> {
            if (closed || !stopping) return;
            if (ledger.hasUnfinishedText()) failMissingFinal();
            else if (capture != null || !pcmQueue.isEmpty() || (speechObserved && resultListener != null))
                fail("AUDIO_DRAIN_TIMEOUT", "마지막 발언의 처리를 마치지 못했어요. 대화록을 확인한 뒤 다시 말해 주세요.");
            else {
                // No sentence was recognized. A quiet PCM stream has no SDK end
                // event, even after stop(); close it without fabricating a final.
                try { if (helper != null) helper.stop(); }
                catch (Exception | LinkageError error) {
                    fail("AUDIO_SEND_ERROR", "음성 입력을 마치지 못했어요. 다시 시작해 주세요.");
                    return;
                }
                finish();
            }
        };
        main.postDelayed(stopDeadline, STOP_DRAIN_LIMIT_MS);
        if (capture != null) capture.stop();
        else finishStoppingInput();
    }

    private void finishStoppingInput() {
        if (closed || !stopping || !ready) return;
        try {
            if (resultListener == null) {
                if (pcmQueue.isEmpty()) { finish(); return; }
                requestUtterance();
            } else {
                drainPcm();
                if (closed || !stopping || !ready) return;
                if (helper != null) completePcmStream(pcmStream.token());
            }
        }
        catch (Exception | LinkageError error) {
            fail("AUDIO_SEND_ERROR", "마지막 음성 입력을 마치지 못했어요. 대화록을 확인한 뒤 다시 말해 주세요.");
        }
    }

    private void finish() {
        stopCapture();
        pcmStream.clear();
        pcmQueue.clear();
        recording = false;
        stopping = false;
        ledger.endUtterance();
        cancelTimers();
        removeResultListener();
        emit("status", "stopped", null, 0, 0, elapsed(), null, null, 0);
    }

    void close() {
        if (closed) return;
        closed = true;
        ready = false;
        recording = false;
        stopping = false;
        initGeneration++;
        ledger.reset();
        cancelTimers();
        if (initializeDeadline != null) main.removeCallbacks(initializeDeadline);
        releaseHelper();
    }

    private void fail(String code, String message) {
        ready = false;
        recording = false;
        stopping = false;
        initGeneration++;
        ledger.endUtterance();
        cancelTimers();
        if (initializeDeadline != null) main.removeCallbacks(initializeDeadline);
        releaseHelper();
        emit("status", "error", null, 0, 0, elapsed(), message, code, 0);
    }

    private void releaseHelper() {
        stopCapture();
        pcmStream.clear();
        pcmQueue.clear();
        removeResultListener();
        SpeechToText previous = helper;
        helper = null;
        if (previous != null) {
            try { previous.stop(); } catch (Exception | LinkageError ignored) { }
            try { previous.release(); } catch (Exception | LinkageError ignored) { }
        }
    }

    private void removeResultListener() {
        if (helper != null && resultListener != null) {
            try { helper.removeListener(resultListener); } catch (Exception | LinkageError ignored) { }
        }
        resultListener = null;
    }

    private double elapsed() {
        return sessionStarted == 0 ? 0 : Math.max(0, (SystemClock.elapsedRealtime() - sessionStarted) / 1000.0);
    }

    private void cancelTimers() {
        if (sessionDeadline != null) main.removeCallbacks(sessionDeadline);
        if (stopDeadline != null) main.removeCallbacks(stopDeadline);
        if (nextRequest != null) main.removeCallbacks(nextRequest);
        if (finalResultDeadline != null) main.removeCallbacks(finalResultDeadline);
    }

    private void emit(String type, String status, String text, int sequence, double start, double end,
                      String error, String errorCode, double rms) {
        if (closed) return;
        JSONObject data = new JSONObject();
        try {
            data.put("type", type);
            data.put("status", status);
            data.put("source", "pleos_on_device");
            data.put("available", available());
            data.put("recording", active());
            data.put("microphone_paused", microphonePaused);
            data.put("audio_input", "foreground_pcm");
            data.put("pending_results", "final".equals(status) || hasPendingResults());
            data.put("conversation_id", conversationId);
            data.put("session_id", sessionId);
            if (!recordingStartedAt.isEmpty()) data.put("recording_started_at", recordingStartedAt);
            if ("transcript".equals(type) && !utteranceRecognizedAt.isEmpty()) data.put("recognized_at", utteranceRecognizedAt);
            data.put("sequence", sequence);
            data.put("seat", "unknown");
            data.put("start", start);
            data.put("end", end);
            if (text != null) data.put("text", text);
            if (error != null) data.put("error", error);
            if (errorCode != null) data.put("error_code", errorCode);
            data.put("rms", rms);
        } catch (JSONException impossible) { return; }
        if ("final".equals(status)) {
            synchronized (pendingResults) {
                pendingResults.put(sessionId + ":" + sequence, data.toString());
            }
        }
        if ("status".equals(type)) snapshot = data.toString();
        listener.onSpeechEvent(data.toString());
    }

    private static String errorMessage(ErrorType error) {
        switch (error) {
            case ON_DEVICE_ENGINE_ERROR: return "차량의 음성 모델을 사용할 수 없어요. 다시 시작하면 기본 음성 인식을 사용해요.";
            case UNAVAILABLE_LANGUAGE: return "차량 음성 모델에서 한국어를 사용할 수 없어요. 기본 음성 인식으로 다시 시작해 주세요.";
            case MIC_ERROR: return "차량 마이크를 열지 못했어요. 다른 음성 기능이 끝난 뒤 다시 시작해 주세요.";
            case PERMISSION_DENIED: return "차량 음성 서비스 권한을 확인하지 못했어요. 기본 음성 인식을 사용할 수 있어요.";
            case NOT_SUPPORTED_API: return "이 환경이 차량 음성 인식을 지원하지 않아요. 기본 음성 인식을 사용할 수 있어요.";
            default: return "차량 음성 인식이 중단됐어요. 다시 시작하면 기본 음성 인식을 사용해요.";
        }
    }
}
