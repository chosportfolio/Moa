package local.pleos.cabin;

public final class NavigationResponseRecoveryTest {
    private static int passed;
    private static void check(boolean condition, String message) {
        if (!condition) throw new AssertionError(message);
        passed++;
    }

    public static void main(String[] args) {
        NavigationResponseRecovery recovery = new NavigationResponseRecovery();
        check(recovery.timedOut() == NavigationResponseRecovery.Action.RECONNECT,
            "The first missing state response can repair a stale callback registration");
        check(recovery.timedOut() == NavigationResponseRecovery.Action.REPORT,
            "A live binding with missing callbacks must report uncertainty after one retry");
        for (int i = 0; i < 100; i++) {
            check(recovery.timedOut() == NavigationResponseRecovery.Action.WAIT,
                "Repeated missing callbacks cannot repeatedly release a working route");
        }
        check(recovery.receivedResponse(), "An actual state response clears the reported delay");
        check(!recovery.receivedResponse(), "Healthy responses do not repeatedly announce recovery");
        check(recovery.timedOut() == NavigationResponseRecovery.Action.RECONNECT,
            "A later independent outage can be repaired after real state responses resume");
        check(!recovery.receivedResponse(), "A response during the retry needs no stale error recovery");
        check(recovery.timedOut() == NavigationResponseRecovery.Action.RECONNECT,
            "One healthy response, rather than binder initialization, resets the retry budget");
        System.out.println("NavigationResponseRecovery: " + passed + " callback recovery checks passed.");
    }
}
