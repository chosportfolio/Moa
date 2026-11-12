package local.pleos.cabin;

/** Measures recognizer inactivity; silent but live microphone callbacks count. */
final class SpeechActivityWatchdog {
    private final long timeoutMillis;
    private long latestActivity;
    private boolean running;

    SpeechActivityWatchdog(long timeoutMillis) { this.timeoutMillis = timeoutMillis; }
    void start(long now) { latestActivity = now; running = true; }
    void touch(long now) { if (running) latestActivity = Math.max(latestActivity, now); }
    void stop() { running = false; }
    long remaining(long now) {
        if (!running) return -1;
        return Math.max(0, timeoutMillis - Math.max(0, now - latestActivity));
    }
}
