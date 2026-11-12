package local.pleos.cabin;

public final class NavigationRecoveryTest {
    private static int passed;
    private static void check(boolean result, String message) {
        if (!result) throw new AssertionError(message);
        passed++;
    }

    public static void main(String[] args) {
        check(NavigationRecovery.recoverableTrip("navigating", "pleos"), "Confirmed Pleos journey can be a candidate");
        for (String status : new String[]{"selected", "arrived", "saved", "dismissed", "cancelled", "", null}) {
            check(!NavigationRecovery.recoverableTrip(status, "pleos"), "Never recover non-guiding state: " + status);
        }
        check(!NavigationRecovery.recoverableTrip("navigating", "other"), "Do not adopt another provider's route");
        check(!NavigationRecovery.recoverableTrip("navigating", null), "Missing provider is not confirmed ownership");
        check(NavigationRecovery.candidateStillCurrent(3, 3, true, false, false), "Original untouched candidate accepted");
        check(!NavigationRecovery.candidateStillCurrent(3, 4, true, false, false), "Late server reply cannot replace newer selection");
        check(!NavigationRecovery.candidateStillCurrent(3, 3, false, false, false), "Explicit action invalidates recovery");
        check(!NavigationRecovery.candidateStillCurrent(3, 3, true, true, false), "Confirmed/current ownership cannot be replaced");
        check(!NavigationRecovery.candidateStillCurrent(3, 3, true, false, true), "Queued explicit request takes precedence");
        check(NavigationRecovery.freshEvidence(true, true, 10000, true, 11000, 12000), "Fresh matching observations restore tracking");
        check(!NavigationRecovery.freshEvidence(false, true, 10000, true, 11000, 12000), "Disconnected evidence cannot restore");
        check(!NavigationRecovery.freshEvidence(true, false, 10000, true, 11000, 12000), "Startup SAFE or routing-in-progress cannot restore");
        check(!NavigationRecovery.freshEvidence(true, true, 10000, false, 11000, 12000), "Different or absent destination cannot restore");
        check(!NavigationRecovery.freshEvidence(true, true, 0, true, 11000, 12000), "Destination-only evidence cannot restore");
        check(!NavigationRecovery.freshEvidence(true, true, 10000, true, 0, 12000), "Route-only evidence cannot restore");
        check(!NavigationRecovery.freshEvidence(true, true, 6000, true, 11000, 12000), "Old route status cannot pair with fresh destination");
        check(!NavigationRecovery.freshEvidence(true, true, 10000, true, 6000, 12000), "Old destination cannot pair with fresh route");
        check(!NavigationRecovery.freshEvidence(true, true, 13000, true, 11000, 12000), "Future timestamps rejected");
        System.out.println("NavigationRecovery: " + passed + " eligibility, request-race and fresh-evidence checks passed.");
    }
}
