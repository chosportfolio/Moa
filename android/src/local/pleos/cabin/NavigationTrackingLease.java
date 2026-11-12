package local.pleos.cabin;

import java.util.LinkedHashSet;

/** The current trip alone owns background callback tracking. */
final class NavigationTrackingLease {
    private String owner = "";
    private final LinkedHashSet<String> finished = new LinkedHashSet<>();
    void begin(String tripId) {
        if (tripId == null || tripId.isEmpty()) throw new IllegalArgumentException("Missing trip owner");
        if (isFinished(tripId)) throw new IllegalArgumentException("Trip is already complete");
        owner = tripId;
    }
    boolean required() { return !owner.isEmpty(); }
    String owner() { return owner; }
    boolean finish(String tripId) {
        if (!required() || !owner.equals(tripId)) return false;
        owner = "";
        return true;
    }
    void clear() { owner = ""; }
    void rememberFinished(String tripId) {
        if (tripId == null || tripId.isEmpty()) return;
        finished.add(tripId);
        while (finished.size() > 64) finished.remove(finished.iterator().next());
    }
    boolean isFinished(String tripId) { return finished.contains(tripId); }
}
