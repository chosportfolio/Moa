package local.pleos.cabin;

public final class SpeechPcmStreamTest {
    private static void check(boolean value) { if (!value) throw new AssertionError(); }
    public static void main(String[] args) {
        SpeechPcmStream stream = new SpeechPcmStream(6400);
        stream.begin(1);
        check(!stream.addBytes(1, 3200));
        stream.pause(true);
        check(!stream.accepts(1));
        check(!stream.addBytes(1, 3200));
        stream.pause(false);
        check(stream.addBytes(1, 3200)); // Paused input did not consume the stream.
        check(stream.end(1));
        check(!stream.end(1)); // completeAudioSend exactly once.
        check(!stream.accepts(1));
        stream.begin(2);
        check(!stream.accepts(1)); // Late callbacks cannot feed the next utterance.
        check(!stream.end(1));
        check(stream.accepts(2));
        stream.clear(); // Explicit stop, SDK failure, or service destruction.
        check(!stream.accepts(2));
        stream.begin(4);
        check(stream.accepts(4));
        check(!stream.addBytes(4, 3200));
        SpeechPcmStream live = new SpeechPcmStream(Integer.MAX_VALUE);
        live.begin(1);
        for (int frame = 0; frame < 1800; frame++) check(!live.addBytes(1, 3200));
        check(live.accepts(1)); // Three quiet minutes do not force a callback-dependent cut.
        check(live.end(1));
        System.out.println("PCM stream: mute/resume, bounded flush, late callbacks and restart passed.");
    }
}
