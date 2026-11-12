package local.pleos.cabin.speechprobe;

import android.app.Activity;
import android.app.Instrumentation;
import android.os.Bundle;
import android.os.SystemClock;
import ai.pleos.playground.stt.SpeechToText;
import ai.pleos.playground.stt.constant.ErrorType;
import ai.pleos.playground.stt.constant.Mode;
import ai.pleos.playground.stt.listener.OnInitializeListener;
import ai.pleos.playground.stt.listener.ResultListener;
import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.util.Arrays;
import java.util.ArrayList;
import java.util.Locale;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;

/** Feeds an existing synthetic Korean fixture; never requests microphone access. */
public final class SpeechSdkProbe extends Instrumentation {
    private final CountDownLatch initialized = new CountDownLatch(1);
    private final CountDownLatch finished = new CountDownLatch(1);
    private volatile SpeechToText helper;
    private volatile String failure = "";
    private volatile String transcript = "";
    private volatile String interim = "";
    private volatile boolean ready;
    private volatile boolean started;
    private volatile boolean ended;
    private volatile int finalCount;
    private final ArrayList<String> events = new ArrayList<>();
    private long began;
    private boolean quietProbe;
    private void event(String type) {
        synchronized (events) { events.add((SystemClock.elapsedRealtime() - began) + ":" + type); }
    }

    @Override public void onCreate(Bundle arguments) {
        super.onCreate(arguments);
        quietProbe = arguments != null && "true".equals(arguments.getString("quiet"));
        start();
    }

    @Override public void onStart() {
        Bundle report = new Bundle();
        began = SystemClock.elapsedRealtime();
        ResultListener listener = new ResultListener() {
            @Override public void onReady() { event("ready"); }
            @Override public void onStartedRecognition() { started = true; event("started"); }
            @Override public void onUpdated(String text, boolean completed) {
                event(completed ? "final" : "interim");
                if (completed) { transcript = text; finalCount++; }
                else interim = text;
            }
            @Override public void onUpdatedEpdData(long on, long off) { }
            @Override public void onUpdatedRms(double rms) { }
            @Override public void onEndedRecognition() { ended = true; event("ended"); finished.countDown(); }
            @Override public void onError(ErrorType error) {
                failure = error.name(); initialized.countDown(); finished.countDown();
            }
        };
        try {
            byte[] pcm;
            try (InputStream input = getContext().getAssets().open("test.pcm");
                 ByteArrayOutputStream output = new ByteArrayOutputStream()) {
                byte[] buffer = new byte[8192];
                int size;
                while ((size = input.read(buffer)) >= 0) output.write(buffer, 0, size);
                pcm = output.toByteArray();
            }
            if (quietProbe) pcm = new byte[64000]; // Two seconds of explicit synthetic silence.
            report.putInt("pcm_bytes", pcm.length);
            runOnMainSync(() -> {
                try {
                    helper = new SpeechToText(getContext(), Mode.ON_DEVICE);
                    helper.addListener(listener);
                    helper.initialize(new OnInitializeListener() {
                        @Override public void onSuccess() {
                            try { helper.setLocale(Locale.KOREA); ready = true; }
                            catch (Throwable error) { failure = error.toString(); }
                            initialized.countDown();
                        }
                        @Override public void onFailure(ErrorType error) {
                            failure = error.name(); initialized.countDown();
                        }
                    });
                } catch (Throwable error) { failure = error.toString(); initialized.countDown(); }
            });
            if (!initialized.await(15, TimeUnit.SECONDS)) failure = "INITIALIZE_TIMEOUT";
            if (ready && failure.isEmpty()) {
                // Official API accepts 16 kHz, signed 16-bit mono PCM. Streaming
                // at capture speed avoids exceeding a Binder transaction size.
                for (int offset = 0; offset < pcm.length && failure.isEmpty() && !ended; offset += 3200) {
                    final byte[] chunk = Arrays.copyOfRange(pcm, offset, Math.min(pcm.length, offset + 3200));
                    runOnMainSync(() -> helper.sendAudio(chunk));
                    SystemClock.sleep(100);
                }
                runOnMainSync(() -> helper.completeAudioSend());
                if (quietProbe) {
                    report.putBoolean("quiet_complete_ended", finished.await(3, TimeUnit.SECONDS));
                    event("explicit_stop");
                    runOnMainSync(() -> helper.stop());
                    report.putBoolean("quiet_stop_ended", finished.await(5, TimeUnit.SECONDS));
                    if (!ended) failure = "QUIET_END_TIMEOUT";
                } else if (!finished.await(35, TimeUnit.SECONDS)) failure = "RESULT_TIMEOUT";
                // Observe whether this SDK sends its final callback after end.
                SystemClock.sleep(3000);
            }
        } catch (Throwable error) { failure = error.toString(); }
        finally {
            runOnMainSync(() -> {
                if (helper != null) {
                    try { helper.removeListener(listener); helper.stop(); helper.release(); }
                    catch (Throwable ignored) { }
                }
            });
        }
        report.putBoolean("initialized", ready);
        report.putBoolean("started", started);
        report.putBoolean("ended", ended);
        report.putInt("final_count", finalCount);
        report.putString("synthetic_final", transcript);
        report.putString("synthetic_interim", interim);
        report.putString("error", failure);
        report.putLong("elapsed_ms", SystemClock.elapsedRealtime() - began);
        synchronized (events) { report.putString("event_order", events.toString()); }
        finish(failure.isEmpty() && (quietProbe ? ended : !transcript.isEmpty()) ? Activity.RESULT_OK : Activity.RESULT_CANCELED, report);
    }
}
