package local.pleos.cabin;

import java.util.ArrayDeque;

/** Bounded process-memory audio retention, released only after commit or user discard. */
final class PendingAudioQueue<T> {
    private final int capacity;
    private final ArrayDeque<T> entries = new ArrayDeque<>();

    PendingAudioQueue(int capacity) {
        if (capacity < 1) throw new IllegalArgumentException("capacity");
        this.capacity = capacity;
    }

    synchronized boolean offer(T entry) {
        if (entries.size() >= capacity) return false;
        entries.addLast(entry);
        return true;
    }

    synchronized T peek() { return entries.peekFirst(); }
    synchronized int size() { return entries.size(); }
    synchronized boolean isEmpty() { return entries.isEmpty(); }

    synchronized boolean acknowledge(T expected) {
        if (expected == null || entries.peekFirst() != expected) return false;
        entries.removeFirst();
        return true;
    }
}
