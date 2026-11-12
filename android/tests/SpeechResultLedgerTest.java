package local.pleos.cabin;

public final class SpeechResultLedgerTest {
    private static void check(boolean value) { if (!value) throw new AssertionError(); }
    public static void main(String[] args) {
        SpeechResultLedger ledger = new SpeechResultLedger();
        long first = ledger.beginUtterance();
        check(ledger.acceptInterim(first, "칼국수") == 1);
        check(ledger.hasUnfinishedText());
        check(ledger.acceptFinal(first, " ") == 0);
        check(ledger.acceptFinal(first, "칼국수 먹자") == 1);
        check(!ledger.hasUnfinishedText());
        check(ledger.acceptInterim(first, "늦은 중간 결과") == 0);
        check(ledger.acceptFinal(first, "칼국수 먹자") == 0);
        check(ledger.acceptFinal(first, "늦은 수정") == 0);
        ledger.endUtterance();
        check(!ledger.current(first));
        check(ledger.acceptFinal(first, "늦은 결과") == 0);
        long second = ledger.beginUtterance();
        check(ledger.acceptFinal(first, "이전 발언") == 0);
        check(ledger.acceptInterim(second, "칼국수") == 2);
        check(ledger.markEnded(second));
        check(!ledger.markEnded(second));
        check(ledger.current(second));
        check(ledger.hasUnfinishedText());
        check(ledger.acceptFinal(second, "칼국수 먹자") == 2);
        check(!ledger.hasUnfinishedText());
        ledger.reset();
        check(ledger.acceptFinal(second, "새 대화에 섞일 발언") == 0);
        long third = ledger.beginUtterance();
        check(ledger.acceptFinal(third, "새 대화") == 1);
        check(ledger.markEnded(third));
        check(ledger.hasFinal(third));
        ledger.endUtterance();
        check(ledger.acceptFinal(third, "최종 대기 시간 후 결과") == 0);
        long quiet = ledger.beginUtterance();
        check(ledger.markEnded(quiet));
        check(!ledger.hasUnfinishedText());
        check(!ledger.hasFinal(quiet));
        ledger.endUtterance();
        long cancelled = ledger.beginUtterance();
        check(ledger.acceptInterim(cancelled, "아직 확정되지 않은 말") == 2);
        ledger.reset();
        check(!ledger.hasUnfinishedText());
        check(ledger.acceptFinal(cancelled, "취소 뒤 결과") == 0);
        System.out.println("Speech result ledger: end-before-final, final-before-end, silence, cancellation, duplicates and conversation isolation passed.");
    }
}
