package local.pleos.cabin;

public final class SpeechPcmQueueTest {
    private static void check(boolean value) { if (!value) throw new AssertionError(); }
    public static void main(String[] args) {
        SpeechPcmQueue queue = new SpeechPcmQueue(6400);
        byte[] first = new byte[3200]; first[0] = 12;
        byte[] second = new byte[3200]; second[0] = 34;
        check(queue.add(first, 12, 1000));
        check(queue.add(second, 34, 1100));
        check(!queue.add(new byte[3200], 1, 1200)); // Do not silently overwrite speech.
        check(queue.firstCapturedAt() == 1000);
        SpeechPcmQueue.Frame sent = queue.remove();
        check(sent.pcm == first && sent.rms == 12 && sent.capturedAt == 1000);
        check(queue.firstCapturedAt() == 1100);
        queue.clear();
        check(queue.isEmpty());
        check(first[0] == 12); // A buffer owned by the async SDK is never overwritten.
        check(second[0] == 0); // Unsent recording is discarded on session termination.
        check(queue.add(new byte[3200], 0, 5000));
        check(queue.remove().capturedAt == 5000);
        System.out.println("PCM handoff: capture order/time, bounded overflow, SDK buffer ownership and reset passed.");
    }
}
