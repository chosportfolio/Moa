package local.pleos.cabin;

public final class SpeechActivityWatchdogTest {
    private static void check(boolean condition) { if (!condition) throw new AssertionError(); }
    public static void main(String[] args) {
        SpeechActivityWatchdog watchdog = new SpeechActivityWatchdog(45000);
        check(watchdog.remaining(0) == -1);
        watchdog.start(1000);
        // An entire three-minute quiet capture remains alive while RMS or
        // recognizer callbacks continue; no words need to be recognized.
        for (long time = 2000; time <= 181000; time += 1000) {
            watchdog.touch(time);
            check(watchdog.remaining(time) == 45000);
        }
        check(watchdog.remaining(225999) == 1);
        check(watchdog.remaining(226000) == 0);
        watchdog.stop();
        watchdog.touch(250000);
        check(watchdog.remaining(300000) == -1);
        watchdog.start(300000);
        watchdog.touch(299000);
        check(watchdog.remaining(300000) == 45000);
        check(watchdog.remaining(345000) == 0);
        System.out.println("Speech inactivity watchdog: live silence, response timeout, stop and restart passed.");
    }
}
