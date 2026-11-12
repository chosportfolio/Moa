package local.pleos.cabin;

/** One user request may dispatch only after a fresh provider response, within its deadline. */
final class NavigationPreparationGate {
    static final long TIMEOUT_MS = 30000;
    private long generation;
    private long started;
    private boolean pending;

    long begin(long now) {
        started = now;
        pending = true;
        return ++generation;
    }

    boolean pending(long token) { return pending && token == generation; }

    boolean take(long token, long now, boolean ready) {
        if (!pending(token) || !ready || now < started || now - started >= TIMEOUT_MS) return false;
        pending = false;
        return true;
    }

    boolean expire(long token, long now) {
        if (!pending(token) || now < started || now - started < TIMEOUT_MS) return false;
        pending = false;
        return true;
    }

    void cancel() { pending = false; generation++; }
}
