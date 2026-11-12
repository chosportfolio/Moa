package local.pleos.cabin;

public final class NativeGleoTest {
    private static int passed;
    private static void check(boolean condition, String message) {
        if (!condition) throw new AssertionError(message);
        passed++;
    }
    public static void main(String[] args) {
        check("start_conversation".equals(NativeGleo.actionType("START_CABIN_CONVERSATION")), "Start mapping");
        check("recommend_places".equals(NativeGleo.actionType("RECOMMEND_CABIN_PLACES")), "Recommendation mapping");
        check("show_saved".equals(NativeGleo.actionType("SHOW_SAVED_CABIN_PLACES")), "Saved mapping");
        check(NativeGleo.actionType(null) == null, "Normal launch must not become a voice command");
        check(NativeGleo.actionType("START_RECORDING") == null, "Unknown command is rejected");
        check(NativeGleo.validStatus("success") && NativeGleo.validStatus("asking") && NativeGleo.validStatus("fail"), "Documented statuses");
        check(!NativeGleo.validStatus("error") && !NativeGleo.validStatus(null), "Unsupported callback status rejected");
        NativeGleo.Command command = new NativeGleo.Command("one", NativeGleo.START, 1000);
        check(command.take(1100), "Ready page takes pending action");
        check(!command.take(1200), "Resume or page-finished cannot deliver twice");
        check(command.fresh(6999), "Callback remains valid before deadline");
        check(!command.fresh(7000), "Late callback cannot be acknowledged as success");
        NativeGleo.Command expired = new NativeGleo.Command("two", NativeGleo.START, 1000);
        check(!expired.take(7001), "Slow page load cannot start stale capture");
        check(!expired.fresh(999), "Invalid monotonic time cannot replay action");
        System.out.println("Native Gleo action lifecycle: " + passed + " checks passed.");
    }
}
