package local.pleos.cabin;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Intent;
import android.content.pm.ServiceInfo;
import android.os.Binder;
import android.os.Build;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;
import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.LinkedHashMap;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import org.json.JSONException;
import org.json.JSONObject;

/** User-started foreground capture with server acknowledgments independent of WebView. */
public final class CabinSpeechService extends Service {
    private static final String START = "local.pleos.cabin.START_SPEECH";
    private static final String STOP = "local.pleos.cabin.STOP_SPEECH";
    private static final String CHANNEL = "cabin_conversation_recording";
    private static final int NOTIFICATION_ID = 41;
    private final Handler main = new Handler(Looper.getMainLooper());
    private final ExecutorService uploader = Executors.newSingleThreadExecutor();
    private final LinkedHashMap<String, JSONObject> pending = new LinkedHashMap<>();
    private final LocalBinder binder = new LocalBinder();
    private NativeSpeech speech;
    private NativeSpeech.Listener listener;
    private volatile boolean starting;
    private long startRequest;
    private volatile boolean uploading;
    private volatile String uploadError;
    private boolean foregroundStarted;
    private String notificationMessage;
    private boolean notificationRecording;
    private boolean destroyed;
    private int failedAttempts;
    private Runnable retry;

    final class LocalBinder extends Binder {
        CabinSpeechService service() { return CabinSpeechService.this; }
    }

    @Override public void onCreate() {
        super.onCreate();
        NotificationManager manager = getSystemService(NotificationManager.class);
        NotificationChannel channel = new NotificationChannel(CHANNEL, "대화 기록", NotificationManager.IMPORTANCE_LOW);
        channel.setDescription("대화 기록 상태와 중지 버튼을 표시합니다.");
        manager.createNotificationChannel(channel);
        speech = new NativeSpeech(this, this::onSpeechEvent);
        speech.publishState();
    }

    @Override public IBinder onBind(Intent intent) { return binder; }

    void attach(NativeSpeech.Listener next) { listener = next; publishState(); }
    void detach(NativeSpeech.Listener previous) { if (listener == previous) listener = null; }
    boolean available() { return !destroyed && speech != null && speech.available(); }
    boolean active() { return starting || (speech != null && speech.active()); }
    boolean hasPendingResults() { return uploading || (speech != null && speech.hasPendingResults()); }

    /** The Activity invokes this only while visible and after RECORD_AUDIO permission. */
    boolean start(String conversationId) {
        if (!available() || active() || hasPendingResults() || !CapturedAudio.validConversationId(conversationId)) return false;
        starting = true;
        long request = ++startRequest;
        uploadError = null;
        try {
            startForegroundService(new Intent(this, CabinSpeechService.class).setAction(START)
                .putExtra("conversation_id", conversationId).putExtra("start_request", request));
            return true;
        } catch (RuntimeException error) {
            starting = false;
            publishFailure("백그라운드 대화 기록을 시작하지 못했어요. 앱을 연 상태에서 다시 시도해 주세요.");
            return false;
        }
    }

    @Override public int onStartCommand(Intent intent, int flags, int startId) {
        if (intent == null) { stopSelf(startId); return START_NOT_STICKY; }
        if (STOP.equals(intent.getAction())) {
            stop();
            finishForegroundIfIdle();
            return START_NOT_STICKY;
        }
        if (!START.equals(intent.getAction())) { stopSelf(startId); return START_NOT_STICKY; }
        boolean accepted = starting && intent.getLongExtra("start_request", -1) == startRequest;
        if (speech != null && speech.active()) return START_NOT_STICKY;
        try {
            Notification notification = notification("대화를 기록하고 있어요. 최대 3분 후 자동으로 마쳐요.", true);
            if (Build.VERSION.SDK_INT >= 29) startForeground(NOTIFICATION_ID, notification, ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE);
            else startForeground(NOTIFICATION_ID, notification);
            foregroundStarted = true;
            notificationMessage = "대화를 기록하고 있어요. 최대 3분 후 자동으로 마쳐요.";
            notificationRecording = true;
            // A queued start must still enter foreground promptly, but a newer
            // explicit stop prevents it from opening the microphone.
            if (!accepted) { finishForegroundIfIdle(); return START_NOT_STICKY; }
            starting = false;
            if (speech == null || !speech.start(intent.getStringExtra("conversation_id"))) {
                publishFailure("차량 음성 인식을 시작하지 못했어요. 대화 화면에서 상태를 확인해 주세요.");
                finishForegroundIfIdle();
            }
        } catch (RuntimeException error) {
            starting = false;
            if (speech != null) speech.stop();
            publishFailure("마이크 서비스 권한을 확인하지 못했어요. 앱을 연 상태에서 다시 시작해 주세요.");
            stopSelf(startId);
        }
        return START_NOT_STICKY;
    }

    void stop() {
        ++startRequest;
        starting = false;
        if (speech != null) speech.stop();
    }

    void acknowledge(String sessionId, int sequence) {
        // The service releases a final only after the loopback server committed it.
        // Older WebViews may still issue their redundant acknowledgment.
    }

    void retryPending() {
        if (destroyed) return;
        uploadError = null;
        if (retry != null) { main.removeCallbacks(retry); retry = null; }
        failedAttempts = 0;
        if (speech != null) speech.publishState();
        drain();
        publishState();
    }

    boolean canDiscardPending() { return !destroyed && !active() && !uploading; }

    /** Main-thread operation for the user's explicit 'do not send again' action. */
    boolean discardPending() {
        if (!canDiscardPending()) { publishState(); return false; }
        if (retry != null) { main.removeCallbacks(retry); retry = null; }
        for (JSONObject event : pending.values()) {
            if (speech != null) speech.acknowledge(event.optString("session_id"), event.optInt("sequence"));
        }
        pending.clear();
        uploadError = null;
        failedAttempts = 0;
        publishState();
        finishForegroundIfIdle();
        return true;
    }

    String state() {
        try {
            JSONObject event = new JSONObject(speech == null ? "{}" : speech.state());
            event.put("source", "pleos_on_device");
            event.put("type", "status");
            event.put("backgroundManaged", true);
            event.put("recording", active());
            event.put("pending_results", hasPendingResults());
            event.put("upload_error", uploadError != null);
            if (uploadError != null) {
                event.put("error", uploadError);
            }
            return event.toString();
        } catch (JSONException error) { return "{\"source\":\"pleos_on_device\",\"backgroundManaged\":true,\"recording\":false}"; }
    }

    void publishState() {
        if (!destroyed && listener != null) listener.onSpeechEvent(state());
    }

    private void onSpeechEvent(String json) {
        if (destroyed) return;
        try {
            JSONObject event = new JSONObject(json);
            // A recognizer stop can arrive after an upload failure. Always
            // merge the service's current delivery state into status events.
            if ("status".equals(event.optString("type")) && speech != null) event = new JSONObject(state());
            event.put("backgroundManaged", true);
            if ("transcript".equals(event.optString("type")) && "final".equals(event.optString("status"))) {
                String key = event.optString("session_id") + ":" + event.optInt("sequence");
                if (!pending.containsKey(key)) pending.put(key, event);
                drain();
            }
            if (listener != null) listener.onSpeechEvent(event.toString());
            if ("status".equals(event.optString("type"))) {
                if (event.has("error")) updateNotification(event.optString("error"), false);
                else if ("stopping".equals(event.optString("status")))
                    updateNotification("마지막 발언을 정리하고 있어요.", false);
                else if (event.optBoolean("microphone_paused"))
                    updateNotification("다른 음성 기능이 마이크를 사용 중이에요. 입력이 돌아오면 이어서 기록해요.", true);
                else if (event.optBoolean("recording"))
                    updateNotification("대화를 기록하고 있어요. 최대 3분 후 자동으로 마쳐요.", true);
                finishForegroundIfIdle();
            }
        } catch (JSONException error) { publishFailure("차량 음성 결과를 읽지 못했어요."); }
    }

    private void drain() {
        if (destroyed || uploading || retry != null || uploadError != null || pending.isEmpty()) return;
        JSONObject event = pending.values().iterator().next();
        String key = event.optString("session_id") + ":" + event.optInt("sequence");
        uploading = true;
        uploader.execute(() -> {
            String error = null;
            try { commitFinal(event); }
            catch (Exception failure) {
                error = failure instanceof IOException && failure.getMessage() != null
                    ? failure.getMessage() : "발언의 저장 완료를 확인하지 못했어요. 앱에서 다시 전송해 주세요.";
            }
            final String failure = error;
            main.post(() -> {
                if (destroyed) return;
                uploading = false;
                if (failure == null) {
                    pending.remove(key);
                    speech.acknowledge(event.optString("session_id"), event.optInt("sequence"));
                    failedAttempts = 0;
                    try {
                        JSONObject committed = new JSONObject(event.toString());
                        committed.put("status", "committed");
                        committed.put("pending_results", hasPendingResults());
                        if (listener != null) listener.onSpeechEvent(committed.toString());
                    } catch (JSONException ignored) { }
                    drain();
                    publishState();
                    finishForegroundIfIdle();
                } else if (++failedAttempts < 3) {
                    retry = () -> { retry = null; drain(); };
                    main.postDelayed(retry, 1500);
                } else {
                    uploadError = failure;
                    stop();
                    updateNotification("발언 전달을 마치지 못했어요. 앱에서 다시 전송해 주세요.", false);
                    publishState();
                    finishForegroundIfIdle();
                }
            });
        });
    }

    private static void commitFinal(JSONObject event) throws IOException, JSONException {
        JSONObject payload = new JSONObject();
        for (String key : new String[]{"conversation_id", "session_id", "sequence", "text", "start", "end", "source"}) {
            payload.put(key, event.get(key));
        }
        if (event.has("recording_started_at")) payload.put("recording_started_at", event.getString("recording_started_at"));
        if (event.has("recognized_at")) payload.put("recognized_at", event.getString("recognized_at"));
        byte[] bytes = payload.toString().getBytes(StandardCharsets.UTF_8);
        HttpURLConnection connection = (HttpURLConnection)new URL("http://127.0.0.1:8765/api/speech/final").openConnection();
        try {
            connection.setInstanceFollowRedirects(false);
            connection.setConnectTimeout(5000);
            connection.setReadTimeout(12000);
            connection.setRequestMethod("POST");
            connection.setRequestProperty("Content-Type", "application/json; charset=utf-8");
            connection.setDoOutput(true);
            connection.setFixedLengthStreamingMode(bytes.length);
            try (OutputStream output = connection.getOutputStream()) { output.write(bytes); }
            int status = connection.getResponseCode();
            if (status == 409) throw new IOException("이전 대화의 발언이 보관 중이에요. 앱에서 대화를 확인해 주세요.");
            if (status != 200) throw new IOException("발언을 저장하지 못했어요. 앱에서 다시 전송해 주세요.");
            try (InputStream input = connection.getInputStream(); ByteArrayOutputStream body = new ByteArrayOutputStream()) {
                byte[] buffer = new byte[4096];
                int count;
                while ((count = input.read(buffer)) != -1) {
                    if (body.size() + count > 1024 * 1024) throw new IOException("발언 저장 응답이 너무 커요.");
                    body.write(buffer, 0, count);
                }
                if (!new JSONObject(new String(body.toByteArray(), StandardCharsets.UTF_8)).optBoolean("committed"))
                    throw new IOException("발언의 저장 완료를 확인하지 못했어요.");
            }
        } finally { connection.disconnect(); }
    }

    private void publishFailure(String error) {
        uploadError = error;
        publishState();
    }

    private Notification notification(String message, boolean recording) {
        PendingIntent open = PendingIntent.getActivity(this, 0, new Intent(this, MainActivity.class), PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        PendingIntent stop = PendingIntent.getService(this, 1, new Intent(this, CabinSpeechService.class).setAction(STOP), PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        Notification.Builder builder = new Notification.Builder(this, CHANNEL)
            .setSmallIcon(android.R.drawable.ic_btn_speak_now).setContentTitle("모아 · 대화 기록")
            .setContentText(message).setContentIntent(open).setOnlyAlertOnce(true).setOngoing(recording);
        if (recording) builder.addAction(new Notification.Action.Builder(null, "대화 마치기", stop).build());
        return builder.build();
    }

    private void updateNotification(String message, boolean recording) {
        if (!foregroundStarted || (message.equals(notificationMessage) && recording == notificationRecording)) return;
        notificationMessage = message;
        notificationRecording = recording;
        getSystemService(NotificationManager.class).notify(NOTIFICATION_ID, notification(message, recording));
    }

    private void finishForegroundIfIdle() {
        if (active() || uploading || (hasPendingResults() && uploadError == null)) return;
        if (foregroundStarted) {
            stopForeground(uploadError == null ? STOP_FOREGROUND_REMOVE : STOP_FOREGROUND_DETACH);
            foregroundStarted = false;
            notificationMessage = null;
        } else if (uploadError == null) getSystemService(NotificationManager.class).cancel(NOTIFICATION_ID);
        stopSelf();
    }

    @Override public void onTaskRemoved(Intent rootIntent) { stop(); finishForegroundIfIdle(); super.onTaskRemoved(rootIntent); }

    @Override public void onDestroy() {
        destroyed = true;
        listener = null;
        if (retry != null) main.removeCallbacks(retry);
        if (speech != null) speech.close();
        uploader.shutdownNow();
        super.onDestroy();
    }
}
