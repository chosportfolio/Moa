package local.pleos.cabin;

public final class VehicleLocationFixTest {
    private static void check(boolean condition, String message) {
        if (!condition) throw new AssertionError(message);
    }
    public static void main(String[] args) {
        long now = 1800000000000L;
        VehicleLocationFix fix = VehicleLocationFix.from(37.0, 127.0, 0.8f, now - 2000, now);
        check(fix != null && fix.timestampMillis == now - 2000, "Use original sample timestamp");
        check(fix.confidence == 0.8f, "Preserve SDK confidence without inventing metre accuracy");
        check(VehicleLocationFix.from(37, 127, 1, now - 30001, now) == null, "Reject old cache");
        check(VehicleLocationFix.from(37, 127, 1, now + 5001, now) == null, "Reject future sample");
        check(VehicleLocationFix.from(37, 127, 1, 0, now) == null, "Reject missing timestamp");
        check(VehicleLocationFix.from(Double.NaN, 127, 1, now, now) == null, "Reject NaN");
        check(VehicleLocationFix.from(37, Double.POSITIVE_INFINITY, 1, now, now) == null, "Reject infinity");
        check(VehicleLocationFix.from(91, 127, 1, now, now) == null, "Reject latitude range");
        check(VehicleLocationFix.from(37, -181, 1, now, now) == null, "Reject longitude range");
        check(VehicleLocationFix.from(0, 0, Float.NaN, now, now).confidence == null, "Coordinates remain valid when confidence is unavailable");
        check(VehicleLocationFix.from(-90, 180, 100, now, now) != null, "Accept real coordinate bounds and do not invent confidence units");
        System.out.println("Vehicle location: 11 fresh-sample, bounds, and confidence cases passed.");
    }
}
