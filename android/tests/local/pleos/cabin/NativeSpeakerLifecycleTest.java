package local.pleos.cabin;

public final class NativeSpeakerLifecycleTest {
    private static int passed;
    private static void check(boolean condition, String label) {
        if (!condition) throw new AssertionError(label);
        passed++;
    }
    public static void main(String[] args) {
        NativeSpeaker.RequestGate gate = new NativeSpeaker.RequestGate();
        long first = gate.begin();
        check(first > 0 && gate.active(), "A request reserves playback");
        check(gate.begin() == 0, "Concurrent playback is rejected");
        check(gate.preparing(first), "Initial preparation may be shown");
        check(gate.started(first), "Start belongs to the pending request");
        check(!gate.preparing(first), "Late ready cannot move speaking back to preparing");
        check(gate.finish(first), "Done completes the first request");
        check(!gate.active(), "Done releases the playback reservation");
        check(!gate.started(first), "Late onStart after done cannot revive speaking");
        check(!gate.finish(first), "Late onError/onStop after done cannot replace the result");

        long second = gate.begin();
        check(second > first, "A later utterance has a new identity");
        check(!gate.finish(first) && gate.current(second), "The previous completion cannot finish the next utterance");
        gate.cancel();
        check(!gate.current(second), "Stop invalidates queued start and callbacks");
        long third = gate.begin();
        check(third > second, "Stop followed by start gets an independent identity");
        check(!gate.started(second), "A cancelled start cannot claim the new utterance");
        check(gate.current(third), "The new utterance remains active after old callbacks");
        check(gate.finish(third), "Timeout consumes the request exactly once");
        check(!gate.finish(third), "A late SDK stop cannot overwrite timeout with stopped");

        long fourth = gate.begin();
        gate.close();
        check(!gate.current(fourth) && !gate.active(), "Close cancels playback");
        check(!gate.started(fourth) && !gate.finish(fourth), "Callbacks after close are ignored");
        check(gate.begin() == 0, "Closed speaker cannot start another utterance");
        System.out.println("Native speaker lifecycle: " + passed + " passed.");
    }
}
