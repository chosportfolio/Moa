package local.pleos.cabin;

/** Separates utterance callbacks from conversation generations without guessing seats. */
final class SpeechResultLedger {
    private long generation;
    private int sequence;
    private boolean open;
    private boolean finalAccepted;
    private boolean recognitionEnded;
    private boolean interimSeen;

    long beginUtterance() {
        open = true;
        finalAccepted = false;
        recognitionEnded = false;
        interimSeen = false;
        return ++generation;
    }

    boolean current(long token) { return open && token == generation; }
    boolean hasFinal(long token) { return current(token) && finalAccepted; }
    boolean ended(long token) { return current(token) && recognitionEnded; }
    boolean hasUnfinishedText() { return open && interimSeen && !finalAccepted; }

    int acceptInterim(long token, String text) {
        if (!current(token) || finalAccepted || text == null || text.trim().isEmpty()) return 0;
        interimSeen = true;
        return sequence + 1;
    }

    // The native service can deliver onEnded before completed=true. Keep this
    // generation open until the caller's bounded final-result grace ends.
    boolean markEnded(long token) {
        if (!current(token) || recognitionEnded) return false;
        recognitionEnded = true;
        return true;
    }

    int acceptFinal(long token, String text) {
        if (!current(token) || finalAccepted || text == null || text.trim().isEmpty()) return 0;
        finalAccepted = true;
        return ++sequence;
    }

    void endUtterance() { open = false; }

    void reset() {
        generation++;
        open = false;
        finalAccepted = false;
        recognitionEnded = false;
        interimSeen = false;
        sequence = 0;
    }
}
