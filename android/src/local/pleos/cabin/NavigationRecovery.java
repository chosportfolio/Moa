package local.pleos.cabin;

/** Conservative cold-start ownership policy. Never produces an arrival or cancellation. */
final class NavigationRecovery {
    private NavigationRecovery() { }

    static boolean recoverableTrip(String status, String provider) {
        return "navigating".equals(status) && "pleos".equals(provider);
    }

    static boolean candidateStillCurrent(long fetchedGeneration, long currentGeneration,
            boolean eligible, boolean hasActive, boolean hasPending) {
        return eligible && fetchedGeneration == currentGeneration && !hasActive && !hasPending;
    }

    static boolean freshEvidence(boolean connected, boolean routeGuiding, long routeAt,
            boolean destinationMatched, long destinationAt, long now) {
        return connected && routeGuiding && destinationMatched
            && routeAt > 0 && destinationAt > 0
            && routeAt <= now && destinationAt <= now
            && now - routeAt <= 5000 && now - destinationAt <= 5000;
    }
}
