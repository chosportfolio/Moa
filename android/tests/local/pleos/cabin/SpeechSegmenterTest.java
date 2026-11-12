package local.pleos.cabin;

import java.util.ArrayList;
import java.util.List;

/** Deterministic PCM boundary checks, with no Android microphone or user audio. */
public final class SpeechSegmenterTest {
    private static final class Event {
        final int bytes;
        final int sequence;
        final double start;
        final double end;
        Event(byte[] pcm, int sequence, double start, double end) {
            this.bytes = pcm.length; this.sequence = sequence; this.start = start; this.end = end;
        }
    }

    private static void feed(SpeechSegmenter splitter, int frames, boolean voiced) {
        byte[] pcm = new byte[640];
        if (voiced) {
            for (int index = 0; index < pcm.length; index += 2) {
                pcm[index] = (byte)0xd0;
                pcm[index + 1] = 7;
            }
        }
        for (int frame = 0; frame < frames; frame++) splitter.accept(pcm, pcm.length, voiced ? 0.06 : 0);
    }

    private static void check(boolean condition, String message) {
        if (!condition) throw new AssertionError(message);
    }

    public static void main(String[] args) {
        List<Event> events = new ArrayList<>();
        SpeechSegmenter splitter = new SpeechSegmenter((pcm, seq, start, end) -> events.add(new Event(pcm, seq, start, end)));
        feed(splitter, 100, false);
        check(events.isEmpty(), "Silence must not produce a chunk");
        feed(splitter, 50, true);
        feed(splitter, 39, false);
        check(events.isEmpty(), "Wait for 800 ms silence");
        feed(splitter, 1, false);
        check(events.size() == 1, "800 ms silence must end an utterance");
        check(Math.abs(events.get(0).start - 1.76) < 0.001, "Include 240 ms pre-roll");
        check(Math.abs(events.get(0).end - 3.8) < 0.001, "Use recording-relative time");
        feed(splitter, 25, true);
        splitter.finish();
        splitter.finish();
        check(events.size() == 2, "Stop emits final speech once");
        check(events.get(1).sequence == 2, "Sequences are ordered");
        check(events.get(1).start >= events.get(0).end, "No overlapping duplicate audio");

        events.clear();
        splitter = new SpeechSegmenter((pcm, seq, start, end) -> events.add(new Event(pcm, seq, start, end)));
        feed(splitter, 1250, true);
        splitter.finish();
        check(events.size() == 3, "25 seconds splits into 12,12,1");
        check(events.get(0).bytes == 16000 * 2 * 12, "First chunk is exactly 12 seconds");
        check(events.get(1).bytes == 16000 * 2 * 12, "Second chunk is exactly 12 seconds");
        check(events.get(2).bytes == 16000 * 2, "Final one-second chunk is preserved");
        check(events.get(2).start == 24 && events.get(2).end == 25, "Continuous timestamps preserved");

        events.clear();
        splitter = new SpeechSegmenter((pcm, seq, start, end) -> events.add(new Event(pcm, seq, start, end)));
        feed(splitter, 2, true);
        feed(splitter, 40, false);
        splitter.finish();
        check(events.isEmpty(), "A brief click is not a voiced utterance");
        System.out.println("SpeechSegmenter: silence, pre-roll, final flush, ordering, 12-second cap and click rejection passed.");
    }
}
