package local.pleos.cabin;

import ai.pleos.playground.navi.constants.NaviErrorCode;
import ai.pleos.playground.navi.constants.NaviMode;
import ai.pleos.playground.navi.constants.RouteOption;
import ai.pleos.playground.navi.data.RouteStateInfo;
import java.util.Collections;

/** Exercise actual SDK states against the production start-confirmation policy. */
public final class NavigationGuidanceTest {
    private static int passed;
    private static void check(boolean condition, String label) {
        if (!condition) throw new AssertionError(label);
        passed++;
    }
    private static RouteStateInfo state(boolean terms, NaviMode mode, boolean routing) {
        return new RouteStateInfo(terms, mode, routing, RouteOption.RECOMMENDED, 0,
            Collections.emptyList(), null);
    }
    private static boolean confirm(RouteStateInfo state, long routeAt, boolean destination, long destinationAt) {
        return NativeNavigation.guidanceConfirmed(true, true, NativeNavigation.isRouteGuiding(state),
            routeAt, destination, destinationAt, 12000);
    }
    public static void main(String[] args) {
        RouteStateInfo guiding = state(true, NaviMode.ROUTE_GUIDING, false);
        check(confirm(guiding, 11000, true, 11500), "Fresh actual guidance and matching destination confirm a start");
        check(!confirm(state(true, NaviMode.ROUTE_GUIDING, true), 11000, true, 11500),
            "Route-search start plus matching destination is not guidance while routing");
        for (NaviMode mode : new NaviMode[]{NaviMode.NOT_WORKING, NaviMode.SAFE_GUIDING, NaviMode.SIMULATION_GUIDING}) {
            check(!confirm(state(true, mode, false), 11000, true, 11500),
                "Non-guiding state cannot record actual navigation: " + mode);
        }
        check(!confirm(null, 11000, true, 11500), "Missing route-state response never assumes guidance");
        check(!confirm(state(false, NaviMode.ROUTE_GUIDING, false), 11000, true, 11500),
            "Unaccepted navigation terms cannot confirm guidance");
        check(!confirm(guiding, 0, true, 11500), "No route-state observation cannot confirm guidance");
        check(!confirm(guiding, 6000, true, 11500), "Old route-state observation cannot confirm a new destination");
        check(!confirm(guiding, 11000, true, 6000), "Old destination cannot confirm new guidance");
        check(!confirm(guiding, 11000, false, 11500), "Different destination cannot confirm our journey");
        check(!NativeNavigation.guidanceConfirmed(false, true, true, 11000, true, 11500, 12000),
            "Connection loss invalidates pending confirmation");
        check(!NativeNavigation.guidanceConfirmed(true, false, true, 11000, true, 11500, 12000),
            "An unrelated active route cannot confirm our uncorrelated request");
        for (NaviErrorCode code : NaviErrorCode.values()) {
            String message = NativeNavigation.routeErrorMessage(code);
            check(message != null && !message.isEmpty() && !message.contains("CAUSE_"),
                "Error is actionable product copy instead of an SDK enum: " + code);
        }
        System.out.println("NavigationGuidance: " + passed + " real SDK state and confirmation checks passed.");
    }
}
