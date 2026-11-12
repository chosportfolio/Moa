package local.pleos.cabin;

import ai.pleos.playground.navi.constants.NaviMode;
import ai.pleos.playground.navi.constants.RouteOption;
import ai.pleos.playground.navi.data.RouteStateInfo;
import java.util.Collections;

public final class NavigationPreparationTest {
    private static int passed;
    private static void check(boolean value, String label) {
        if (!value) throw new AssertionError(label);
        passed++;
    }
    private static RouteStateInfo state(boolean terms, NaviMode mode, boolean routing) {
        return new RouteStateInfo(terms, mode, routing, RouteOption.RECOMMENDED, 0,
            Collections.emptyList(), null);
    }
    public static void main(String[] args) {
        NavigationPreparationGate gate = new NavigationPreparationGate();
        long first = gate.begin(1000);
        check(!gate.take(first, 2000, false), "Cold provider never receives a premature route");
        check(gate.take(first, 4000, true), "A fresh readiness response can send the pending route");
        for (int i = 0; i < 20; i++) check(!gate.take(first, 5000 + i, true), "Repeated callbacks never duplicate a route");
        long replaced = gate.begin(5000);
        long current = gate.begin(6000);
        check(!gate.take(replaced, 7000, true), "An old callback cannot dispatch the replaced request");
        check(!gate.expire(replaced, 50000), "An old timer cannot expire a new request");
        check(gate.take(current, 8000, true), "The current request remains dispatchable");
        long cancelled = gate.begin(9000);
        gate.cancel();
        check(!gate.take(cancelled, 10000, true), "Cancellation blocks a delayed readiness response");
        long expired = gate.begin(10000);
        check(!gate.take(expired, 40000, true), "A callback exactly at the 30-second deadline cannot start guidance");
        check(gate.expire(expired, 40000), "The timeout is reported once");
        check(!gate.expire(expired, 40001), "A repeated timer does not repeat the error");
        check(!gate.take(expired, 41000, true), "A provider that starts after timeout cannot navigate later");
        long closing = gate.begin(50000);
        gate.cancel();
        check(!gate.take(closing, 50001, true), "Closing cancels outstanding preparation");
        long fresh = gate.begin(60000);
        check(!gate.take(fresh, 59999, true), "Earlier observations are not fresh readiness evidence");
        check(!gate.expire(fresh, 60001), "A fresh request is not expired by an early timer");
        check(!NativeNavigation.providerReady(null), "Missing provider state is not readiness");
        check(!NativeNavigation.providerReady(state(false, NaviMode.SAFE_GUIDING, false)), "Terms must be accepted");
        check(!NativeNavigation.providerReady(state(true, NaviMode.NOT_WORKING, false)), "A stopped provider is not ready");
        check(!NativeNavigation.providerReady(state(true, NaviMode.SAFE_GUIDING, true)), "Wait while the provider is routing");
        for (NaviMode mode : new NaviMode[]{NaviMode.SAFE_GUIDING, NaviMode.ROUTE_GUIDING, NaviMode.SIMULATION_GUIDING})
            check(NativeNavigation.providerReady(state(true, mode, false)), "A working native provider can accept a new route: " + mode);
        System.out.println("NavigationPreparation: " + passed + " readiness, expiry, cancellation, and single-dispatch checks passed.");
    }
}
