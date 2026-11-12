package local.pleos.cabin;

/** Audio plus the conversation/session identifiers captured when recording began. */
final class CapturedAudio {
    final String purpose;
    final byte[] wav;
    final int sequence;
    final double start;
    final double end;
    final String sessionId;
    final String conversationId;
    final String recordingStartedAt;

    CapturedAudio(String purpose, byte[] wav, int sequence, double start, double end,
            String sessionId, String conversationId) {
        this(purpose, wav, sequence, start, end, sessionId, conversationId, "");
    }

    CapturedAudio(String purpose, byte[] wav, int sequence, double start, double end,
            String sessionId, String conversationId, String recordingStartedAt) {
        this.purpose = purpose;
        this.wav = wav;
        this.sequence = sequence;
        this.start = start;
        this.end = end;
        this.sessionId = sessionId;
        this.conversationId = conversationId == null ? "" : conversationId;
        this.recordingStartedAt = recordingStartedAt == null ? "" : recordingStartedAt;
    }

    static boolean validConversationId(String value) {
        return value != null && value.matches("[A-Za-z0-9_-]{1,80}");
    }
}
