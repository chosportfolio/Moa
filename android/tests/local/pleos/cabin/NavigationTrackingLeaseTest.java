package local.pleos.cabin;

public final class NavigationTrackingLeaseTest {
    private static int passed;
    private static void check(boolean value, String label) {
        if (!value) throw new AssertionError(label);
        passed++;
    }
    public static void main(String[] args) {
        NavigationTrackingLease lease = new NavigationTrackingLease();
        check(!lease.required(), "Process startup never restarts an old tracking notification");
        lease.begin("A");
        check(lease.required() && lease.owner().equals("A"), "A user request owns tracking");
        check(!lease.finish("B") && lease.required(), "Another trip cannot stop tracking");
        lease.begin("B");
        check(!lease.finish("A") && lease.owner().equals("B"), "Delayed A completion cannot stop B");
        check(lease.finish("B") && !lease.required(), "Arrival or cancellation releases its own trip");
        check(!lease.finish("B"), "Repeated terminal responses are idempotent");
        check(!lease.required(), "A delayed service start after completion must stop itself");
        lease.begin("C");
        lease.clear();
        check(!lease.required(), "Timeout, failure and close release the service lease");
        check(!lease.finish("C"), "A late callback cannot revive an abandoned trip");
        lease.begin("D");
        check(lease.owner().equals("D"), "A new explicit request can start after a failure");
        lease.rememberFinished("A");
        check(lease.owner().equals("D") && lease.required(), "A delayed completion never interrupts current D tracking");
        check(lease.isFinished("A"), "Completion is remembered even without A's service lease or recovery candidate");
        boolean rejected = false;
        try { lease.begin("A"); } catch (IllegalArgumentException expected) { rejected = true; }
        check(rejected && lease.owner().equals("D"), "A stale recovery result cannot resurrect a completed trip");
        lease.clear();
        check(lease.isFinished("A"), "Service startup failure or cleanup does not forget terminal records");
        for (int i = 0; i < 70; i++) lease.rememberFinished("finished-" + i);
        check(!lease.isFinished("A") && lease.isFinished("finished-69"), "Completion memory is bounded to recent trips");
        System.out.println("NavigationTrackingLease: " + passed + " lifecycle and stale-owner checks passed.");
    }
}
