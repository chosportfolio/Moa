package local.pleos.cabin;

import java.util.ArrayDeque;
import java.util.Arrays;

/** Bounded, process-only handoff while an SDK utterance waits for its late final. */
final class SpeechPcmQueue {
    static final class Frame {
        final byte[] pcm;
        final double rms;
        final long capturedAt;
        Frame(byte[] pcm, double rms, long capturedAt) {
            this.pcm = pcm; this.rms = rms; this.capturedAt = capturedAt;
        }
    }
    private final int capacity;
    private final ArrayDeque<Frame> frames = new ArrayDeque<>();
    private int bytes;
    SpeechPcmQueue(int capacity) { this.capacity = capacity; }
    boolean add(byte[] pcm, double rms, long capturedAt) {
        if (pcm == null || pcm.length == 0 || pcm.length > capacity - bytes) return false;
        frames.add(new Frame(pcm, rms, capturedAt));
        bytes += pcm.length;
        return true;
    }
    boolean isEmpty() { return frames.isEmpty(); }
    long firstCapturedAt() { return frames.getFirst().capturedAt; }
    Frame remove() { Frame frame = frames.removeFirst(); bytes -= frame.pcm.length; return frame; }
    void clear() {
        // These frames were never passed to the SDK, so they can be erased now.
        for (Frame frame : frames) Arrays.fill(frame.pcm, (byte)0);
        frames.clear(); bytes = 0;
    }
}
