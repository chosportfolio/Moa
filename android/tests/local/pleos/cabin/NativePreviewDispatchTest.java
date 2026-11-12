package local.pleos.cabin;

public final class NativePreviewDispatchTest {
    private static int passed;
    private static void check(boolean value, String label) {
        if (!value) throw new AssertionError(label);
        passed++;
    }
    public static void main(String[] args) {
        NativeNavigation.PreviewSearchGate gate = new NativeNavigation.PreviewSearchGate();
        long first = gate.begin(1000);
        gate.leftCaller();
        check(gate.take(first, 1600, true), "Expected Activity pause must allow the native search stage");
        check(!gate.take(first, 1700, true), "A callback runs only once");
        long old = gate.begin(2000);
        long next = gate.begin(2100);
        check(!gate.take(old, 2600, true), "A newer preview invalidates an old callback");
        check(gate.take(next, 2700, true), "The current preview still executes");
        long cancelled = gate.begin(3000);
        gate.cancel();
        check(!gate.take(cancelled, 3600, true), "Close or another navigation action cancels the stage");
        long returned = gate.begin(4000);
        gate.leftCaller();
        check(gate.returnedToCaller(), "Returning from navigation cancels an unfinished stage");
        check(!gate.take(returned, 4600, true), "A late callback cannot take the user back to navigation");
        long fresh = gate.begin(5000);
        check(!gate.returnedToCaller(), "Attaching before handoff is not a return from navigation");
        check(gate.take(fresh, 5600, true), "A normal first handoff remains available");
        long stale = gate.begin(6000);
        check(!gate.take(stale, 8000, true), "A blocked UI cannot dispatch a stale screen change later");
        long disconnected = gate.begin(9000);
        check(!gate.take(disconnected, 9600, false), "A different or disconnected helper cannot receive a queued request");
        System.out.println("Native preview dispatch: " + passed + " passed.");
    }
}
