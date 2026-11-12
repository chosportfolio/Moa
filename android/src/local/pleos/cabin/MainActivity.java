package local.pleos.cabin;

import android.Manifest;
import android.app.Activity;
import android.content.ActivityNotFoundException;
import android.content.Intent;
import android.content.ContentValues;
import android.content.ContentResolver;
import android.content.ComponentName;
import android.content.ServiceConnection;
import android.content.pm.ApplicationInfo;
import android.content.pm.PackageManager;
import android.graphics.Color;
import android.media.AudioFormat;
import android.media.AudioRecord;
import android.media.MediaRecorder;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Environment;
import android.os.IBinder;
import android.provider.MediaStore;
import android.os.SystemClock;
import android.util.Base64;
import android.util.Log;
import android.view.Gravity;
import android.view.View;
import android.view.WindowManager;
import android.webkit.JavascriptInterface;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.TextView;
import android.widget.Toast;
import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.OutputStream;
import java.nio.charset.StandardCharsets;
import java.lang.ref.WeakReference;
import java.text.SimpleDateFormat;
import java.util.Date;
import java.util.Locale;
import java.util.UUID;
import java.time.Instant;
import java.util.concurrent.atomic.AtomicBoolean;
import org.json.JSONException;
import org.json.JSONObject;

public final class MainActivity extends Activity {
    private static final String APP_URL = "http://127.0.0.1:8765/";
    private static final int SAMPLE_RATE = 16000;
    private static final int RECORD_PERMISSION = 10;
    private static final int AUDIO_FILE = 11;
    private static final int SAVE_TRANSCRIPT = 12;
    private static final int MAX_PENDING_AUDIO = 8;
    // Shared across Activity recreation, but never persisted as microphone files.
    private static final PendingAudioQueue<CapturedAudio> pendingAudio = new PendingAudioQueue<>(MAX_PENDING_AUDIO);
    private static WeakReference<MainActivity> audioActivity = new WeakReference<>(null);
    private final AtomicBoolean recording = new AtomicBoolean(false);
    private WebView webView;
    private LinearLayout connectionError;
    private TextView connectionMessage;
    private volatile AudioRecord audioRecord;
    private volatile boolean captureFinishing;
    private volatile boolean destroyed;
    private volatile String transferFailure;
    private boolean foreground;
    private volatile boolean pageReady;
    private volatile boolean preparingCapture;
    private boolean deliveringAudio;
    private CapturedAudio awaitingAudioCommit;
    private int documentGeneration;
    private volatile String pendingPurpose;
    private String pendingConversationId;
    private String pendingMarkdown;
    private Runnable stopDeadline;
    private ValueCallback<Uri[]> fileCallback;
    private NativeNavigation navigation;
    private NativeLocation vehicleLocation;
    private volatile CabinSpeechService nativeSpeech;
    private NativeSpeaker nativeSpeaker;
    private boolean speechBound;
    private final NativeSpeech.Listener speechListener = this::emitSpeech;
    private final ServiceConnection speechConnection = new ServiceConnection() {
        @Override public void onServiceConnected(ComponentName name, IBinder binder) {
            if (destroyed) return;
            nativeSpeech = ((CabinSpeechService.LocalBinder)binder).service();
            nativeSpeech.attach(speechListener);
        }
        @Override public void onServiceDisconnected(ComponentName name) {
            nativeSpeech = null;
            emitSpeech("{\"source\":\"pleos_on_device\",\"backgroundManaged\":true,\"type\":\"status\",\"recording\":false,\"available\":false,\"error\":\"대화 기록 서비스 연결이 끊겼어요. 앱을 다시 열어 주세요.\"}");
        }
    };
    private NativeGleo gleo;
    private final NativeNavigation.Listener navigationListener = this::emitNavigation;

    @Override public void onCreate(Bundle state) {
        super.onCreate(state);
        AppBranding.updateTask(this);
        FrameLayout root = new FrameLayout(this);
        root.setBackgroundColor(Color.rgb(246, 248, 251));
        webView = new WebView(this);
        webView.setBackgroundColor(Color.rgb(246, 248, 251));
        // The desktop emulator can expose an EGL configuration that Chromium
        // rejects. Keep the vehicle's normal rendering; use a software layer
        // only on the emulator so the conversation does not become transparent.
        if (Build.HARDWARE.contains("ranchu") || Build.HARDWARE.contains("goldfish"))
            webView.setLayerType(View.LAYER_TYPE_SOFTWARE, null);
        WebSettings settings = webView.getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(true);
        settings.setAllowFileAccess(false);
        settings.setAllowContentAccess(true);
        settings.setMixedContentMode(WebSettings.MIXED_CONTENT_NEVER_ALLOW);
        settings.setMediaPlaybackRequiresUserGesture(true);
        settings.setSupportMultipleWindows(false);
        webView.addJavascriptInterface(new CabinBridge(), "CabinAndroid");
        webView.setWebViewClient(new AppWebViewClient());
        webView.setWebChromeClient(new AppChromeClient());
        root.addView(webView, new FrameLayout.LayoutParams(-1, -1));
        connectionError = new LinearLayout(this);
        connectionError.setOrientation(LinearLayout.VERTICAL);
        connectionError.setGravity(Gravity.CENTER);
        connectionError.setPadding(40, 40, 40, 40);
        connectionError.setBackgroundColor(Color.rgb(246, 248, 251));
        TextView title = new TextView(this);
        title.setText("모아");
        title.setTextColor(Color.rgb(24, 43, 65));
        title.setTextSize(30);
        connectionError.addView(title);
        connectionMessage = new TextView(this);
        connectionMessage.setTextSize(19);
        connectionMessage.setGravity(Gravity.CENTER);
        connectionMessage.setPadding(0, 28, 0, 28);
        connectionError.addView(connectionMessage);
        Button retry = new Button(this);
        retry.setText("다시 연결");
        retry.setOnClickListener(view -> loadApp());
        connectionError.addView(retry);
        connectionError.setVisibility(View.GONE);
        root.addView(connectionError, new FrameLayout.LayoutParams(-1, -1));
        setContentView(root);
        navigation = NativeNavigation.get(getApplicationContext());
        vehicleLocation = new NativeLocation(this);
        nativeSpeaker = new NativeSpeaker(this, this::emitSpeaker);
        speechBound = bindService(new Intent(this, CabinSpeechService.class), speechConnection, BIND_AUTO_CREATE);
        gleo = new NativeGleo(this, json -> {
            if (!destroyed && foreground && pageReady && trustedPage()) {
                webView.evaluateJavascript("window.onGleoAction&&window.onGleoAction(" + json + ");", null);
            }
        });
        if (state == null) gleo.accept(getIntent());
        loadApp();
        if ((getApplicationInfo().flags & ApplicationInfo.FLAG_DEBUGGABLE) != 0) {
            new Thread(() -> Log.d("CabinGleo", "uid=" + android.os.Process.myUid()
                + " context=" + readGleoSeatContext()), "CabinGleoContextCheck").start();
        }
    }

    private void loadApp() {
        pageReady = false;
        if (gleo != null) gleo.setReady(false);
        connectionError.setVisibility(View.GONE);
        webView.loadUrl(APP_URL);
    }

    private static boolean isLocal(Uri uri) {
        return uri != null && "http".equals(uri.getScheme())
            && "127.0.0.1".equals(uri.getHost()) && uri.getPort() == 8765
            && uri.getUserInfo() == null;
    }

    private boolean trustedPage() {
        return webView != null && isLocal(Uri.parse(webView.getUrl() == null ? "" : webView.getUrl()));
    }

    private void showConnectionError() {
        pageReady = false;
        if (gleo != null) gleo.setReady(false);
        requestFallbackStop();
        connectionMessage.setText("대화 서비스에 연결하지 못했어요.\n잠시 후 다시 연결해 주세요.");
        connectionError.setVisibility(View.VISIBLE);
    }

    private final class AppWebViewClient extends WebViewClient {
        @Override public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
            if (isLocal(request.getUrl())) return false;
            if (request.isForMainFrame() && "https".equals(request.getUrl().getScheme())) {
                try { startActivity(new Intent(Intent.ACTION_VIEW, request.getUrl())); }
                catch (ActivityNotFoundException error) {
                    Toast.makeText(MainActivity.this, "연결할 브라우저가 설치되어 있지 않아요.", Toast.LENGTH_LONG).show();
                }
            }
            return true;
        }

        @Override public WebResourceResponse shouldInterceptRequest(WebView view, WebResourceRequest request) {
            if (isLocal(request.getUrl())) return null;
            return new WebResourceResponse("text/plain", "UTF-8", 403, "Blocked", null,
                new ByteArrayInputStream(new byte[0]));
        }

        @Override public void onPageStarted(WebView view, String url, android.graphics.Bitmap icon) {
            pageReady = false;
            if (gleo != null) gleo.setReady(false);
            documentGeneration++;
            deliveringAudio = false;
            awaitingAudioCommit = null;
            requestFallbackStop();
        }

        @Override public void onPageFinished(WebView view, String url) {
            if (isLocal(Uri.parse(url)) && connectionError.getVisibility() != View.VISIBLE) {
                pageReady = true;
                emitStatus(false, 0, null);
                deliverPendingAudio();
                if (navigation != null) navigation.publishState();
                if (nativeSpeech != null) nativeSpeech.publishState();
                if (nativeSpeaker != null) emitSpeaker(nativeSpeaker.state());
                if (gleo != null) gleo.setReady(foreground && trustedPage());
            }
        }

        @Override public void onReceivedError(WebView view, WebResourceRequest request, WebResourceError error) {
            if (request.isForMainFrame()) showConnectionError();
        }

        @Override public void onReceivedHttpError(WebView view, WebResourceRequest request, WebResourceResponse response) {
            if (request.isForMainFrame() && response.getStatusCode() >= 400) showConnectionError();
        }
    }

    private final class AppChromeClient extends WebChromeClient {
        @Override public boolean onShowFileChooser(WebView view, ValueCallback<Uri[]> callback,
                FileChooserParams params) {
            if (!trustedPage()) return false;
            if (fileCallback != null) fileCallback.onReceiveValue(null);
            fileCallback = callback;
            Intent intent = new Intent(Intent.ACTION_OPEN_DOCUMENT);
            intent.addCategory(Intent.CATEGORY_OPENABLE);
            intent.setType("audio/*");
            intent.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION);
            try { startActivityForResult(intent, AUDIO_FILE); }
            catch (ActivityNotFoundException error) {
                fileCallback.onReceiveValue(null);
                fileCallback = null;
                emitStatus(false, 0, "녹음 파일 선택 앱이 설치되어 있지 않아요.");
            }
            return true;
        }
    }

    private final class CabinBridge {
        @JavascriptInterface public boolean completeGleoAction(String id, String status, String message, String data) {
            // WebView navigation is restricted to this app's loopback origin.
            return !destroyed && gleo != null && gleo.complete(id, status, message, data);
        }

        @JavascriptInterface public String getNavigationState() {
            return navigation == null ? "{\"status\":\"connecting\",\"available\":false,\"connected\":false}" : navigation.state();
        }

        @JavascriptInterface public void openPlacePreview(String payload) {
            runOnUiThread(() -> { if (foreground && trustedPage() && navigation != null) navigation.request(payload, true); });
        }

        @JavascriptInterface public void startNavigation(String payload) {
            runOnUiThread(() -> { if (foreground && trustedPage() && navigation != null) navigation.request(payload, false); });
        }

        @JavascriptInterface public void stopGuidance() {
            runOnUiThread(() -> { if (foreground && trustedPage() && navigation != null) navigation.stop(); });
        }

        @JavascriptInterface public void finishNavigationTracking(String tripId) {
            // A completed server response may arrive after the Activity loses focus.
            // Ending our observer does not cancel or change the native route.
            runOnUiThread(() -> { if (trustedPage() && navigation != null) navigation.finishTracking(tripId); });
        }

        @JavascriptInterface public String getGleoSeatContext() {
            return readGleoSeatContext();
        }

        @JavascriptInterface public String getSpeakerState() {
            return nativeSpeaker == null ? "{\"available\":false,\"state\":\"connecting\"}" : nativeSpeaker.state();
        }

        @JavascriptInterface public boolean speakRecommendations(String text) {
            // pageReady is true only for the local app origin. Do not read WebView
            // properties from its JavaScript bridge thread.
            return !destroyed && pageReady && !microphoneBusy()
                && nativeSpeaker != null && nativeSpeaker.speak(text);
        }

        @JavascriptInterface public void stopRecommendationSpeech() {
            if (!destroyed && pageReady && nativeSpeaker != null) nativeSpeaker.stop();
        }

        @JavascriptInterface public String getSpeechState() {
            return nativeSpeech == null ? "{\"available\":false}" : nativeSpeech.state();
        }

        @JavascriptInterface public void acknowledgeSpeech(String sessionId, int sequence) {
            runOnUiThread(() -> {
                if (trustedPage() && nativeSpeech != null) {
                    nativeSpeech.acknowledge(sessionId, sequence);
                    nativeSpeech.publishState();
                }
            });
        }

        @JavascriptInterface public void retrySpeechResults() {
            runOnUiThread(() -> { if (trustedPage() && nativeSpeech != null) nativeSpeech.retryPending(); });
        }

        @JavascriptInterface public boolean discardSpeechResults() {
            CabinSpeechService service = nativeSpeech;
            if (destroyed || !pageReady || service == null || !service.canDiscardPending()) return false;
            // The boolean acknowledges the request only. The service rechecks
            // on the main thread and publishes its actual pending-results state.
            runOnUiThread(() -> {
                if (!destroyed && pageReady && nativeSpeech == service) service.discardPending();
            });
            return true;
        }

        @JavascriptInterface public void startRecording(String purpose) {
            requestRecording(purpose, "", false);
        }

        @JavascriptInterface public void startRecordingForConversation(String purpose, String conversationId) {
            requestRecording(purpose, conversationId, true);
        }

        private void requestRecording(String purpose, String conversationId, boolean requireConversation) {
            runOnUiThread(() -> {
                if (!foreground || !trustedPage()) return;
                if (recording.get() || captureFinishing || pendingPurpose != null || (nativeSpeech != null && nativeSpeech.active())) return;
                if (nativeSpeech != null && nativeSpeech.hasPendingResults()) {
                    nativeSpeech.publishState();
                    emitStatus(false, 0, "이전 발언의 저장을 마친 뒤 다시 시작해 주세요.");
                    return;
                }
                if (!pendingAudio.isEmpty() || deliveringAudio) {
                    emitStatus(false, 0, "이전 녹음을 전달하고 있어요. 잠시 후 다시 시작해 주세요.");
                    return;
                }
                String safePurpose = purpose == null ? "meeting" : purpose;
                if (safePurpose.length() > 80) {
                    emitStatus(false, 0, "녹음 종류를 확인해 주세요.");
                    return;
                }
                if (requireConversation && !CapturedAudio.validConversationId(conversationId)) {
                    emitStatus(false, 0, "대화 상태를 확인하지 못했어요. 다시 연결한 뒤 녹음을 시작해 주세요.");
                    return;
                }
                if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
                    pendingPurpose = safePurpose;
                    pendingConversationId = conversationId;
                    requestPermissions(new String[]{Manifest.permission.RECORD_AUDIO}, RECORD_PERMISSION);
                } else {
                    beginRecording(safePurpose, conversationId);
                }
            });
        }

        @JavascriptInterface public void stopRecording() {
            runOnUiThread(() -> {
                if (trustedPage()) {
                    pendingPurpose = null;
                    pendingConversationId = null;
                    requestStop();
                }
            });
        }

        @JavascriptInterface public void acknowledgeAudio(String sessionId, int sequence) {
            runOnUiThread(() -> releaseCommittedAudio(sessionId, sequence));
        }

        @JavascriptInterface public void discardAudio(String sessionId, int sequence) {
            runOnUiThread(() -> releaseCommittedAudio(sessionId, sequence));
        }

        @JavascriptInterface public void saveTranscript(String markdown) {
            runOnUiThread(() -> {
                if (!foreground || !trustedPage()) return;
                if (pendingMarkdown != null) return;
                if (markdown == null || markdown.trim().isEmpty()) {
                    emitSaveResult(false, false, "저장할 대화록이 없어요.");
                    return;
                }
                if (markdown.length() > 1024 * 1024) {
                    emitSaveResult(false, false, "대화록이 너무 커요. 대화를 나눠 저장해 주세요.");
                    return;
                }
                pendingMarkdown = markdown;
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                    saveToDownloads(markdown);
                    return;
                }
                Intent intent = new Intent(Intent.ACTION_CREATE_DOCUMENT);
                intent.addCategory(Intent.CATEGORY_OPENABLE);
                intent.setType("text/markdown");
                String timestamp = new SimpleDateFormat("yyyyMMdd-HHmm", Locale.KOREA).format(new Date());
                intent.putExtra(Intent.EXTRA_TITLE, "모아-대화록-" + timestamp + ".md");
                intent.addFlags(Intent.FLAG_GRANT_WRITE_URI_PERMISSION);
                try { startActivityForResult(intent, SAVE_TRANSCRIPT); }
                catch (ActivityNotFoundException error) {
                    pendingMarkdown = null;
                    emitSaveResult(false, false, "파일을 저장할 수 있는 앱을 찾지 못했어요.");
                }
            });
        }

        @JavascriptInterface public String getCapabilities() {
            return "{\"microphone\":true,\"saveTranscript\":true,\"sampleRate\":16000,\"nativeSpeech\":true,"
                + "\"backgroundSpeech\":true,\"recommendationSpeech\":true,"
                + "\"gleoActions\":true,"
                + "\"streaming\":true,\"audioCommitAcknowledgment\":true,\"conversationGeneration\":true,\"seatDetection\":false,\"gleoSeatContext\":true,\"maxUtteranceSeconds\":12,"
                + "\"maxMeetingSeconds\":180,\"maxEnrollmentSeconds\":15,"
                + "\"meetingMaxSeconds\":180,\"enrollmentMaxSeconds\":15,"
                + "\"nativeNavigation\":{\"preview\":true,\"start\":true,\"arrival\":true,"
                + "\"previewMode\":\"native_search\",\"connected\":" + (navigation != null && navigation.available()) + "}}";
        }
    }

    private void emitSpeech(String json) {
        runOnUiThread(() -> {
            if (destroyed) return;
            if (recording.get() || (nativeSpeech != null && nativeSpeech.active()))
                getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
            else getWindow().clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
            if (!destroyed && pageReady && trustedPage()) {
                webView.evaluateJavascript("window.onNativeSpeech&&window.onNativeSpeech(" + json + ");", null);
            }
        });
    }

    private boolean microphoneBusy() {
        return preparingCapture || recording.get() || captureFinishing || pendingPurpose != null
            || (nativeSpeech != null && nativeSpeech.active());
    }

    private void emitSpeaker(String json) {
        runOnUiThread(() -> {
            if (!destroyed && pageReady && trustedPage()) {
                webView.evaluateJavascript("window.onNativeSpeaker&&window.onNativeSpeaker(" + json + ");", null);
            }
        });
    }

    private void emitNavigation(String json) {
        runOnUiThread(() -> {
            if (!destroyed && pageReady && trustedPage()) {
                webView.evaluateJavascript("window.onNativeNavigation&&window.onNativeNavigation(" + json + ");", null);
            }
        });
    }

    private String readGleoSeatContext() {
        // Gleo's selected voice-trigger/session seat is not a per-utterance
        // speaker label. Never attach this snapshot to captured audio.
        JSONObject result = new JSONObject();
        boolean available = false;
        String status = "Unavailable";
        String position = "Unknown";
        try {
            if (!destroyed) {
                Bundle response = getContentResolver().call(
                    Uri.parse("content://ai.umos.ambientai.provider/status"),
                    "getAgentStatus", null, null);
                String raw = response == null ? null : response.getString("result");
                if (raw != null && raw.length() <= 4096) {
                    JSONObject context = new JSONObject(raw);
                    String receivedStatus = context.optString("status", "");
                    if (!receivedStatus.isEmpty() && receivedStatus.length() <= 64) {
                        available = true;
                        status = receivedStatus;
                        String receivedPosition = context.optString("wwdPosition", "Unknown");
                        if (receivedPosition.matches("ROW_[123]_(LEFT|RIGHT)")) {
                            position = receivedPosition;
                        }
                    }
                }
            }
        } catch (RuntimeException | JSONException ignored) {
            // Missing providers, access denial and incompatible platform
            // versions leave the feature unavailable without changing access.
        }
        try {
            result.put("available", available);
            result.put("status", status);
            result.put("wwdPosition", position);
            result.put("observedAt", System.currentTimeMillis());
        } catch (JSONException impossible) {
            return "{\"available\":false,\"status\":\"Unavailable\",\"wwdPosition\":\"Unknown\",\"observedAt\":0}";
        }
        return result.toString();
    }

    private void beginRecording(String purpose, String conversationId) {
        if (!foreground || recording.get() || captureFinishing) return;
        if (nativeSpeech != null && (nativeSpeech.active() || nativeSpeech.hasPendingResults())) return;
        preparingCapture = true;
        try {
            if (nativeSpeaker != null) nativeSpeaker.stop();
            if (nativeSpeech != null && "meeting".equals(purpose)
                    && nativeSpeech.available() && nativeSpeech.start(conversationId)) return;
            recording.set(true);
        } finally { preparingCapture = false; }
        transferFailure = null;
        captureFinishing = true;
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        stopDeadline = this::requestStop;
        webView.postDelayed(stopDeadline, purpose.startsWith("enroll") ? 15000 : 180000);
        emitStatus(true, 0, null);
        new Thread(() -> capture(purpose, conversationId), "CabinAudioCapture").start();
    }

    private void capture(String purpose, String conversationId) {
        ByteArrayOutputStream pcm = new ByteArrayOutputStream();
        boolean streaming = "meeting".equals(purpose);
        String sessionId = UUID.randomUUID().toString();
        final String[] recordingStartedAt = {""};
        SpeechSegmenter segmenter = new SpeechSegmenter((audio, sequence, start, end) ->
            queueCapturedAudio(new CapturedAudio("stream", wav(audio), sequence, start, end, sessionId, conversationId, recordingStartedAt[0])));
        String failure = null;
        AudioRecord recorder = null;
        int limitSeconds = purpose.startsWith("enroll") ? 15 : 180;
        int limitBytes = limitSeconds * SAMPLE_RATE * 2;
        int totalBytes = 0;
        long lastMeter = 0;
        double maximumRms = 0;
        try {
            int minimum = AudioRecord.getMinBufferSize(SAMPLE_RATE, AudioFormat.CHANNEL_IN_MONO,
                AudioFormat.ENCODING_PCM_16BIT);
            if (minimum <= 0) throw new IOException("오디오 입력 형식을 사용할 수 없어요.");
            AudioRecord.Builder builder = new AudioRecord.Builder()
                .setAudioSource(MediaRecorder.AudioSource.VOICE_RECOGNITION)
                .setAudioFormat(new AudioFormat.Builder().setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                    .setSampleRate(SAMPLE_RATE).setChannelMask(AudioFormat.CHANNEL_IN_MONO).build())
                .setBufferSizeInBytes(Math.max(minimum * 2, 8192));
            if (Build.VERSION.SDK_INT >= 30) builder.setPrivacySensitive(true);
            recorder = builder.build();
            audioRecord = recorder;
            if (recorder.getState() != AudioRecord.STATE_INITIALIZED) {
                throw new IOException("마이크를 초기화하지 못했어요.");
            }
            if (recording.get()) {
                recordingStartedAt[0] = Instant.ofEpochMilli(System.currentTimeMillis()).toString();
                recorder.startRecording();
            }
            short[] samples = new short[320];
            byte[] bytes = new byte[samples.length * 2];
            while (recording.get() && totalBytes < limitBytes) {
                int count = recorder.read(samples, 0, samples.length, AudioRecord.READ_BLOCKING);
                if (count < 0) {
                    if (recording.get()) throw new IOException("마이크 입력이 중단됐어요.");
                    break;
                }
                int accepted = Math.min(count, (limitBytes - totalBytes) / 2);
                double squareSum = 0;
                for (int index = 0; index < accepted; index++) {
                    short sample = samples[index];
                    squareSum += (double)sample * sample;
                    bytes[index * 2] = (byte)(sample & 0xff);
                    bytes[index * 2 + 1] = (byte)((sample >>> 8) & 0xff);
                }
                double rms = accepted == 0 ? 0 : Math.sqrt(squareSum / accepted) / 32768.0;
                totalBytes += accepted * 2;
                if (streaming) segmenter.accept(bytes, accepted * 2, rms);
                else pcm.write(bytes, 0, accepted * 2);
                maximumRms = Math.max(maximumRms, rms);
                long now = SystemClock.elapsedRealtime();
                if (now - lastMeter >= 120) {
                    lastMeter = now;
                    emitStatus(true, Math.min(1, rms * 6), null);
                }
            }
        } catch (SecurityException error) {
            failure = "마이크 권한이 필요해요. 앱 설정에서 허용해 주세요.";
        } catch (Exception error) {
            failure = error instanceof IOException ? error.getMessage() : "마이크를 열지 못했어요. 잠시 후 다시 시도해 주세요.";
        } finally {
            recording.set(false);
            audioRecord = null;
            if (recorder != null) {
                try { recorder.stop(); } catch (IllegalStateException ignored) { }
                recorder.release();
            }
        }
        if (streaming) segmenter.finish();
        final String captureFailure;
        if (failure != null) captureFailure = failure;
        else if (transferFailure != null) captureFailure = transferFailure;
        else if (streaming && segmenter.emittedCount() > 0) captureFailure = null;
        else if (totalBytes < SAMPLE_RATE * 2) captureFailure = "녹음이 너무 짧아요. 1초 이상 말해 주세요.";
        else if (maximumRms < 0.00005) captureFailure = "마이크 입력이 없어요. 마이크 연결과 사용 권한을 확인해 주세요.";
        else if (streaming) captureFailure = "인식할 발언을 찾지 못했어요. 마이크 가까이에서 다시 말해 주세요.";
        else captureFailure = null;
        final byte[] capturedPcm = pcm.toByteArray();
        runOnUiThread(() -> {
            if (stopDeadline != null) {
                webView.removeCallbacks(stopDeadline);
                stopDeadline = null;
            }
            captureFinishing = false;
            getWindow().clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
            if (destroyed) return;
            emitStatus(false, 0, captureFailure);
            if (captureFailure == null && !streaming) queueCapturedAudio(new CapturedAudio(purpose, wav(capturedPcm), 0, 0, 0, sessionId, conversationId, recordingStartedAt[0]));
        });
    }

    private void queueCapturedAudio(CapturedAudio result) {
        if (!pendingAudio.offer(result)) {
            transferFailure = "녹음 전달 대기열이 가득 차 마지막 발화를 전달하지 못했어요. 처리가 끝난 뒤 다시 말해 주세요.";
            recording.set(false);
            return;
        }
        int waiting = pendingAudio.size();
        // Stop before accepting another utterance; queued audio remains available for retry.
        if (waiting == MAX_PENDING_AUDIO) {
            transferFailure = "분석을 기다리는 발화가 많아 녹음을 멈췄어요. 대기 중인 발화는 순서대로 전달하고 있어요.";
            recording.set(false);
        }
        runOnUiThread(() -> {
            MainActivity target = audioActivity.get();
            if (target != null && !target.destroyed) target.deliverPendingAudio();
        });
    }

    private void requestStop() {
        if (nativeSpeech != null && nativeSpeech.active()) nativeSpeech.stop();
        requestFallbackStop();
    }

    private void requestFallbackStop() {
        recording.set(false);
        if (stopDeadline != null) {
            webView.removeCallbacks(stopDeadline);
            stopDeadline = null;
        }
        AudioRecord recorder = audioRecord;
        if (recorder != null) {
            try { recorder.stop(); } catch (IllegalStateException ignored) { }
        }
    }

    private void emitStatus(boolean active, double level, String error) {
        runOnUiThread(() -> {
            if (!destroyed && pageReady && trustedPage()) {
                if (active && !recording.get()) return;
                JSONObject payload = new JSONObject();
                try {
                    int waiting = pendingAudio.size();
                    payload.put("recording", active);
                    payload.put("level", level);
                    payload.put("pendingChunks", waiting);
                    payload.put("pendingAudio", waiting > 0 || (!active && captureFinishing));
                    if (error != null) payload.put("error", error);
                } catch (JSONException impossible) { return; }
                webView.evaluateJavascript("window.onNativeStatus&&window.onNativeStatus(" + payload + ");", null);
            }
        });
    }

    private void emitSaveResult(boolean success, boolean cancelled, String error) {
        JSONObject payload = new JSONObject();
        try {
            payload.put("success", success);
            payload.put("cancelled", cancelled);
            if (success) payload.put("message", Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q
                ? "다운로드 / 모아에 대화록을 저장했어요." : "대화록을 저장했어요.");
            if (error != null) payload.put("error", error);
        } catch (JSONException impossible) { return; }
        runOnUiThread(() -> {
            if (destroyed) return;
            if (pageReady && trustedPage()) {
                webView.evaluateJavascript("window.onNativeSaveResult&&window.onNativeSaveResult(" + payload + ");", null);
            }
            if (!cancelled) {
                Toast.makeText(MainActivity.this, success ? payload.optString("message") : error, Toast.LENGTH_LONG).show();
            }
        });
    }

    private void saveToDownloads(String markdown) {
        // Pleos has no DocumentsUI picker. App-owned downloads work without
        // storage permissions, and become visible only after a complete write.
        ContentResolver resolver = getApplicationContext().getContentResolver();
        new Thread(() -> {
            Uri destination = null;
            boolean success = false;
            try {
                String timestamp = new SimpleDateFormat("yyyyMMdd-HHmmss", Locale.KOREA).format(new Date());
                ContentValues values = new ContentValues();
                values.put(MediaStore.Downloads.DISPLAY_NAME, "모아-대화록-" + timestamp + "-" + UUID.randomUUID().toString().substring(0, 6) + ".md");
                values.put(MediaStore.Downloads.MIME_TYPE, "text/markdown");
                values.put(MediaStore.Downloads.RELATIVE_PATH, Environment.DIRECTORY_DOWNLOADS + "/모아/");
                values.put(MediaStore.Downloads.IS_PENDING, 1);
                destination = resolver.insert(MediaStore.Downloads.EXTERNAL_CONTENT_URI, values);
                if (destination == null) throw new IOException("Could not create download");
                try (OutputStream output = resolver.openOutputStream(destination, "w")) {
                    if (output == null) throw new IOException("Could not open download");
                    output.write(markdown.getBytes(StandardCharsets.UTF_8));
                    output.flush();
                }
                values.clear();
                values.put(MediaStore.Downloads.IS_PENDING, 0);
                if (resolver.update(destination, values, null, null) != 1) throw new IOException("Could not publish download");
                success = true;
            } catch (Exception error) {
                if (destination != null) {
                    try { resolver.delete(destination, null, null); } catch (Exception ignored) { }
                }
            }
            final boolean saved = success;
            runOnUiThread(() -> {
                pendingMarkdown = null;
                emitSaveResult(saved, false, saved ? null : "대화록을 저장하지 못했어요. 저장 공간을 확인하고 다시 시도해 주세요.");
            });
        }, "transcript-download").start();
    }

    private void deliverPendingAudio() {
        if (destroyed || !pageReady || !trustedPage() || deliveringAudio || awaitingAudioCommit != null || pendingAudio.isEmpty()) return;
        deliveringAudio = true;
        int generation = documentGeneration;
        CapturedAudio result = pendingAudio.peek();
        String encoded = Base64.encodeToString(result.wav, Base64.NO_WRAP);
        webView.evaluateJavascript("window.__cabinAudioParts=[];", ignored -> sendAudioPart(result, encoded, 0, generation));
    }

    private void sendAudioPart(CapturedAudio result, String encoded, int offset, int generation) {
        if (generation != documentGeneration) return;
        if (destroyed || !pageReady || !trustedPage()) {
            deliveringAudio = false;
            return;
        }
        int end = Math.min(encoded.length(), offset + 48 * 1024);
        if (offset < end) {
            String part = JSONObject.quote(encoded.substring(offset, end));
            webView.evaluateJavascript("window.__cabinAudioParts.push(" + part + ");",
                ignored -> sendAudioPart(result, encoded, end, generation));
            return;
        }
        String dispatch = "(function(){if(typeof window.onNativeAudio!=='function')return false;"
            + "var data={base64:window.__cabinAudioParts.join(''),mime:'audio/wav',purpose:"
            + JSONObject.quote(result.purpose) + ",sequence:" + result.sequence
            + ",start:" + result.start + ",end:" + result.end + ",seat:'unknown',sessionId:"
            + JSONObject.quote(result.sessionId) + ",conversationId:" + JSONObject.quote(result.conversationId)
            + ",recordingStartedAt:" + JSONObject.quote(result.recordingStartedAt)
            + "};window.__cabinAudioParts=[];"
            + "return window.onNativeAudio(data)!==false;})()";
        webView.evaluateJavascript(dispatch, accepted -> {
            if (generation != documentGeneration) return;
            deliveringAudio = false;
            // A very fast commit may reach Java before this JS callback.
            if (pendingAudio.peek() != result) {
                deliverPendingAudio();
                return;
            }
            if ("true".equals(accepted)) {
                if ("stream".equals(result.purpose)) {
                    awaitingAudioCommit = result;
                } else {
                    pendingAudio.acknowledge(result);
                    if (!recording.get()) emitStatus(false, 0, null);
                    deliverPendingAudio();
                }
            } else {
                webView.postDelayed(this::deliverPendingAudio, 750);
            }
        });
    }

    private void releaseCommittedAudio(String sessionId, int sequence) {
        if (destroyed || !pageReady || !trustedPage()) return;
        CapturedAudio head = pendingAudio.peek();
        if (head == null || !"stream".equals(head.purpose)
                || !head.sessionId.equals(sessionId) || head.sequence != sequence) return;
        if (!pendingAudio.acknowledge(head)) return;
        if (awaitingAudioCommit == head) awaitingAudioCommit = null;
        emitStatus(recording.get(), 0, null);
        if (!deliveringAudio) deliverPendingAudio();
    }

    private static byte[] wav(byte[] pcm) {
        byte[] out = new byte[44 + pcm.length];
        putText(out, 0, "RIFF");
        putInt(out, 4, 36 + pcm.length);
        putText(out, 8, "WAVEfmt ");
        putInt(out, 16, 16);
        out[20] = 1;
        out[22] = 1;
        putInt(out, 24, SAMPLE_RATE);
        putInt(out, 28, SAMPLE_RATE * 2);
        out[32] = 2;
        out[34] = 16;
        putText(out, 36, "data");
        putInt(out, 40, pcm.length);
        System.arraycopy(pcm, 0, out, 44, pcm.length);
        return out;
    }

    private static void putInt(byte[] data, int offset, int value) {
        for (int index = 0; index < 4; index++) data[offset + index] = (byte)(value >>> (index * 8));
    }

    private static void putText(byte[] data, int offset, String value) {
        for (int index = 0; index < value.length(); index++) data[offset + index] = (byte)value.charAt(index);
    }

    @Override public void onRequestPermissionsResult(int request, String[] permissions, int[] results) {
        super.onRequestPermissionsResult(request, permissions, results);
        if (request == NativeLocation.PERMISSION_REQUEST) {
            if (vehicleLocation != null) vehicleLocation.permissionResult();
            return;
        }
        if (request != RECORD_PERMISSION) return;
        if (results.length == 0 || results[0] != PackageManager.PERMISSION_GRANTED) {
            pendingPurpose = null;
            pendingConversationId = null;
            emitStatus(false, 0, "마이크 권한을 허용해야 음성을 기록할 수 있어요.");
            return;
        }
        startPendingPermissionRecording();
    }

    private void startPendingPermissionRecording() {
        if (foreground && pendingPurpose != null
                && checkSelfPermission(Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED) {
            String purpose = pendingPurpose;
            String conversationId = pendingConversationId;
            pendingPurpose = null;
            pendingConversationId = null;
            beginRecording(purpose, conversationId);
        }
    }

    @Override protected void onActivityResult(int request, int result, Intent data) {
        super.onActivityResult(request, result, data);
        if (request == AUDIO_FILE && fileCallback != null) {
            Uri selected = result == RESULT_OK && data != null ? data.getData() : null;
            fileCallback.onReceiveValue(selected == null ? null : new Uri[]{selected});
            fileCallback = null;
        }
        if (request == SAVE_TRANSCRIPT) {
            String markdown = pendingMarkdown;
            pendingMarkdown = null;
            Uri selected = result == RESULT_OK && data != null ? data.getData() : null;
            if (selected == null || markdown == null) {
                emitSaveResult(false, true, null);
                return;
            }
            new Thread(() -> {
                try (OutputStream output = getContentResolver().openOutputStream(selected, "wt")) {
                    if (output == null) throw new IOException("No output stream");
                    output.write(markdown.getBytes(StandardCharsets.UTF_8));
                    output.flush();
                    emitSaveResult(true, false, null);
                } catch (IOException | SecurityException error) {
                    emitSaveResult(false, false, "대화록을 저장하지 못했어요. 저장할 위치를 다시 선택해 주세요.");
                }
            }, "CabinTranscriptSave").start();
        }
    }

    @Override protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        setIntent(intent);
        if (gleo != null) gleo.accept(intent);
    }

    @Override protected void onResume() {
        super.onResume();
        AppBranding.updateTask(this);
        foreground = true;
        audioActivity = new WeakReference<>(this);
        startPendingPermissionRecording();
        deliverPendingAudio();
        if (navigation != null) navigation.attach(navigationListener);
        if (vehicleLocation != null) vehicleLocation.start();
        if (nativeSpeech != null) nativeSpeech.publishState();
        if (nativeSpeaker != null) emitSpeaker(nativeSpeaker.state());
        if (gleo != null) gleo.setReady(pageReady && trustedPage());
    }

    @Override protected void onPause() {
        foreground = false;
        if (gleo != null) gleo.setReady(false);
        if (vehicleLocation != null) vehicleLocation.stop();
        if (navigation != null) navigation.detach(navigationListener);
        requestFallbackStop();
        super.onPause();
    }

    @Override protected void onDestroy() {
        destroyed = true;
        if (gleo != null) gleo.close();
        if (nativeSpeaker != null) nativeSpeaker.close();
        if (vehicleLocation != null) vehicleLocation.destroy();
        if (nativeSpeech != null) nativeSpeech.detach(speechListener);
        if (speechBound) { unbindService(speechConnection); speechBound = false; }
        if (audioActivity.get() == this) audioActivity.clear();
        requestFallbackStop();
        if (navigation != null) navigation.detach(navigationListener);
        if (fileCallback != null) fileCallback.onReceiveValue(null);
        webView.removeJavascriptInterface("CabinAndroid");
        webView.destroy();
        super.onDestroy();
    }
}
