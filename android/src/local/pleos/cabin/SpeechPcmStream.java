package local.pleos.cabin;

/** Main-thread gate for bounded public-SDK PCM streams. No audio is retained. */
final class SpeechPcmStream {
    private final int limitBytes;
    private long token;
    private int sentBytes;
    private boolean open;
    private boolean paused;

    SpeechPcmStream(int limitBytes) { this.limitBytes = limitBytes; }
    void begin(long nextToken) { token = nextToken; sentBytes = 0; open = true; }
    long token() { return token; }
    void pause(boolean value) { paused = value; }
    boolean accepts(long expected) { return open && !paused && token == expected; }
    boolean addBytes(long expected, int count) {
        if (!accepts(expected) || count <= 0) return false;
        sentBytes += count;
        return sentBytes >= limitBytes;
    }
    boolean end(long expected) {
        if (!open || token != expected) return false;
        open = false;
        return true;
    }
    void clear() { open = false; paused = false; token++; sentBytes = 0; }
}
