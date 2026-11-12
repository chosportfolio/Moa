package local.pleos.cabin;

/** A connected service's missing state callback is not proof of a dead binding. */
final class NavigationResponseRecovery {
    enum Action { RECONNECT, REPORT, WAIT }
    private boolean retried;
    private boolean reported;

    Action timedOut() {
        if (!retried) {
            retried = true;
            return Action.RECONNECT;
        }
        if (!reported) {
            reported = true;
            return Action.REPORT;
        }
        return Action.WAIT;
    }

    boolean receivedResponse() {
        boolean wasReported = reported;
        retried = false;
        reported = false;
        return wasReported;
    }
}
