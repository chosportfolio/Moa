package local.pleos.cabin;

/** No microphone input: verifies commit retention, stale acknowledgments and bounds. */
public final class PendingAudioQueueTest {
    private static void check(boolean condition, String message) {
        if (!condition) throw new AssertionError(message);
    }

    public static void main(String[] args) {
        PendingAudioQueue<Object> queue = new PendingAudioQueue<>(2);
        Object first = new Object();
        Object second = new Object();
        Object overflow = new Object();
        check(queue.offer(first) && queue.offer(second), "Store accepted audio");
        check(!queue.offer(overflow) && queue.size() == 2, "Bound memory without replacing earlier audio");
        check(queue.peek() == first && queue.peek() == first, "Sending/reloading does not remove audio");
        check(!queue.acknowledge(second), "Cannot acknowledge a later utterance out of order");
        check(!queue.acknowledge(new Object()), "Reject a stale or unrelated acknowledgment");
        check(queue.acknowledge(first) && queue.peek() == second, "Commit releases exactly the first utterance");
        check(!queue.acknowledge(first), "Duplicate commit is harmless");
        check(queue.offer(overflow), "Released space can accept new audio");
        check(queue.acknowledge(second) && queue.acknowledge(overflow) && queue.isEmpty(), "Preserve FIFO order");
        check(!queue.acknowledge(null), "An empty queue cannot be acknowledged");
        PendingAudioQueue<CapturedAudio> recordings = new PendingAudioQueue<>(2);
        String conversation = "conversation-before-reset";
        CapturedAudio old = new CapturedAudio("stream", new byte[44], 1, 0, 1, "recording-1", conversation);
        check(recordings.offer(old), "Store audio with its original conversation");
        conversation = "conversation-after-reset";
        CapturedAudio next = new CapturedAudio("stream", new byte[44], 1, 0, 1, "recording-2", conversation);
        check(recordings.offer(next), "New capture has a separate conversation");
        check(recordings.peek() == old && "conversation-before-reset".equals(recordings.peek().conversationId),
            "Reloading/retrying old audio cannot relabel it with the current conversation");
        check(recordings.acknowledge(old) && "conversation-after-reset".equals(recordings.peek().conversationId),
            "Each queued capture retains its own conversation");
        check(CapturedAudio.validConversationId("e9f9d463-2fb4-444a-a8eb-0cce54a560a2"), "Accept UUID conversation IDs");
        check(!CapturedAudio.validConversationId(null) && !CapturedAudio.validConversationId("")
            && !CapturedAudio.validConversationId("current\r\ninjected"), "Reject absent or malformed new conversation IDs");
        CapturedAudio legacy = new CapturedAudio("stream", new byte[44], 1, 0, 1, "old-app", null);
        check(legacy.conversationId.isEmpty(), "Legacy capture remains unidentified instead of guessing its conversation");
        System.out.println("PendingAudioQueue: retention, bounds, FIFO, commit acknowledgment and conversation isolation passed.");
    }
}
