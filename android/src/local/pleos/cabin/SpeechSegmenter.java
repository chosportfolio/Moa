package local.pleos.cabin;

import java.io.ByteArrayOutputStream;

/** Energy-based utterance boundaries; does not identify a speaker or a seat. */
final class SpeechSegmenter {
    interface Listener {
        void onUtterance(byte[] pcm, int sequence, double start, double end);
    }

    private static final int RATE = 16000;
    private static final int SILENCE_SAMPLES = RATE * 800 / 1000;
    private static final int MAX_SAMPLES = RATE * 12;
    private static final int MIN_VOICED_SAMPLES = RATE * 160 / 1000;
    private final Listener listener;
    private final byte[] preRoll = new byte[RATE * 240 / 1000 * 2];
    private final ByteArrayOutputStream utterance = new ByteArrayOutputStream();
    private int preRollSize;
    private int preRollCursor;
    private int silenceSamples;
    private int voicedSamples;
    private int sequence;
    private long totalSamples;
    private long startSample;
    private double noiseFloor = 0.0007;

    SpeechSegmenter(Listener listener) { this.listener = listener; }

    void accept(byte[] pcm, int byteCount, double rms) {
        int samples = byteCount / 2;
        long frameStart = totalSamples;
        totalSamples += samples;
        boolean voiced = rms >= Math.max(0.0025, Math.min(0.02, noiseFloor * 3));
        if (!voiced) noiseFloor = noiseFloor * 0.98 + rms * 0.02;
        if (utterance.size() == 0) {
            if (!voiced) {
                remember(pcm, byteCount);
                return;
            }
            startSample = frameStart - preRollSize / 2;
            int first = (preRollCursor - preRollSize + preRoll.length) % preRoll.length;
            for (int index = 0; index < preRollSize; index++) {
                utterance.write(preRoll[(first + index) % preRoll.length]);
            }
            preRollSize = 0;
            preRollCursor = 0;
        }
        utterance.write(pcm, 0, byteCount);
        if (voiced) {
            voicedSamples += samples;
            silenceSamples = 0;
        } else {
            silenceSamples += samples;
        }
        if (silenceSamples >= SILENCE_SAMPLES || utterance.size() / 2 >= MAX_SAMPLES) flush();
    }

    private void remember(byte[] pcm, int count) {
        for (int index = 0; index < count; index++) {
            preRoll[preRollCursor] = pcm[index];
            preRollCursor = (preRollCursor + 1) % preRoll.length;
        }
        preRollSize = Math.min(preRoll.length, preRollSize + count);
    }

    private void flush() {
        if (voicedSamples >= MIN_VOICED_SAMPLES) {
            listener.onUtterance(utterance.toByteArray(), ++sequence,
                startSample / (double)RATE, totalSamples / (double)RATE);
        }
        utterance.reset();
        silenceSamples = 0;
        voicedSamples = 0;
    }

    void finish() { flush(); }
    int emittedCount() { return sequence; }
}
