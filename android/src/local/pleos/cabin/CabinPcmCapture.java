package local.pleos.cabin;

import android.media.AudioFormat;
import android.media.AudioManager;
import android.media.AudioRecord;
import android.media.AudioRecordingConfiguration;
import android.media.MediaRecorder;
import android.os.Build;
import android.os.Handler;
import android.os.SystemClock;
import android.util.Log;
import java.util.Arrays;
import java.util.List;
import java.util.concurrent.atomic.AtomicInteger;

/** A microphone owned by the app's visible foreground service, never a file. */
final class CabinPcmCapture {
    interface Listener {
        void onPcm(byte[] pcm, double rms, long capturedAt);
        void onSilenced(boolean silenced);
        void onFailure(String code);
        void onStopped();
    }
    private static final int SAMPLE_RATE = 16000;
    private static final int CHUNK_BYTES = 3200; // 100 ms, mono signed PCM16 LE.
    private final Handler main;
    private final Listener listener;
    private final AtomicInteger queued = new AtomicInteger();
    private volatile AudioRecord recorder;
    private volatile boolean running;
    private volatile boolean silenced;
    private AudioManager.AudioRecordingCallback recordingCallback;

    CabinPcmCapture(Handler main, Listener listener) { this.main = main; this.listener = listener; }

    void start() {
        if (running) throw new IllegalStateException("Capture already started");
        int minimum = AudioRecord.getMinBufferSize(SAMPLE_RATE, AudioFormat.CHANNEL_IN_MONO,
            AudioFormat.ENCODING_PCM_16BIT);
        if (minimum <= 0) throw new IllegalStateException("PCM capture format unavailable");
        AudioRecord.Builder builder = new AudioRecord.Builder()
            .setAudioSource(MediaRecorder.AudioSource.VOICE_RECOGNITION)
            .setAudioFormat(new AudioFormat.Builder().setSampleRate(SAMPLE_RATE)
                .setChannelMask(AudioFormat.CHANNEL_IN_MONO).setEncoding(AudioFormat.ENCODING_PCM_16BIT).build())
            .setBufferSizeInBytes(Math.max(minimum * 2, CHUNK_BYTES * 4));
        // Do not preempt the vehicle assistant. Report Android's sharing decision.
        if (Build.VERSION.SDK_INT >= 30) builder.setPrivacySensitive(false);
        AudioRecord input = builder.build();
        recorder = input;
        try {
            if (input.getState() != AudioRecord.STATE_INITIALIZED)
                throw new IllegalStateException("Microphone unavailable");
            if (Build.VERSION.SDK_INT >= 29) {
                recordingCallback = new AudioManager.AudioRecordingCallback() {
                    @Override public void onRecordingConfigChanged(List<AudioRecordingConfiguration> configurations) {
                        AudioRecordingConfiguration config = input.getActiveRecordingConfiguration();
                        if (config != null) updateSilenced(config.isClientSilenced());
                    }
                };
                input.registerAudioRecordingCallback(Runnable::run, recordingCallback);
            }
            running = true;
            input.startRecording();
            if (input.getRecordingState() != AudioRecord.RECORDSTATE_RECORDING)
                throw new IllegalStateException("Microphone did not start");
            Log.i("CabinPcmCapture", "capture started session=" + input.getAudioSessionId());
            new Thread(() -> read(input), "cabin-pcm-capture").start();
        } catch (RuntimeException error) {
            running = false;
            release(input);
            throw error;
        }
    }

    private void read(AudioRecord input) {
        byte[] buffer = new byte[CHUNK_BYTES];
        try {
            while (running) {
                int size = input.read(buffer, 0, buffer.length, AudioRecord.READ_BLOCKING);
                // stop() can unblock read with a valid partial tail. Keep that
                // captured tail, but do not treat the expected stop error as failure.
                if (size <= 0) { if (running) fail("MICROPHONE_READ_ERROR"); break; }
                boolean frameSilenced = silenced;
                if (Build.VERSION.SDK_INT >= 29) {
                    AudioRecordingConfiguration config = input.getActiveRecordingConfiguration();
                    if (config != null) {
                        frameSilenced = config.isClientSilenced();
                        updateSilenced(frameSilenced);
                    }
                }
                if (frameSilenced) continue; // This also applies to the partial tail returned by stop().
                if (queued.incrementAndGet() > 4) {
                    queued.decrementAndGet();
                    fail("MICROPHONE_BACKPRESSURE");
                    break;
                }
                final byte[] pcm = Arrays.copyOf(buffer, size & ~1);
                final long capturedAt = SystemClock.elapsedRealtime() - pcm.length * 1000L / (SAMPLE_RATE * 2);
                double sum = 0;
                for (int i = 0; i + 1 < pcm.length; i += 2) {
                    short sample = (short)((pcm[i] & 255) | (pcm[i + 1] << 8));
                    sum += (double)sample * sample;
                }
                final double rms = Math.sqrt(sum / Math.max(1, pcm.length / 2));
                main.post(() -> {
                    queued.decrementAndGet();
                    // This frame was captured before stop and was not silenced.
                    // Deliver it before onStopped so the final syllable can drain.
                    listener.onPcm(pcm, rms, capturedAt);
                    // The public SDK queues this array for Binder delivery; do
                    // not overwrite it before that asynchronous send completes.
                });
            }
        } catch (RuntimeException error) {
            if (running) fail("MICROPHONE_READ_ERROR");
        } finally {
            Arrays.fill(buffer, (byte)0);
            release(input);
            main.post(listener::onStopped);
        }
    }

    private void updateSilenced(boolean next) {
        if (!running || next == silenced) return;
        silenced = next;
        Log.i("CabinPcmCapture", "microphone silenced=" + next);
        main.post(() -> { if (running && silenced == next) listener.onSilenced(next); });
    }

    private void fail(String code) {
        running = false;
        main.post(() -> listener.onFailure(code));
    }

    void stop() {
        running = false;
        AudioRecord input = recorder;
        if (input != null) {
            try { input.stop(); } catch (IllegalStateException ignored) { }
        }
    }

    private void release(AudioRecord input) {
        if (Build.VERSION.SDK_INT >= 29 && recordingCallback != null) {
            try { input.unregisterAudioRecordingCallback(recordingCallback); } catch (RuntimeException ignored) { }
        }
        try { input.stop(); } catch (IllegalStateException ignored) { }
        input.release();
        if (recorder == input) recorder = null;
        Log.i("CabinPcmCapture", "capture released");
    }
}
